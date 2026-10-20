"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from lanpilot import __version__
from lanpilot.config import ConfigError, Settings, load_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("lanpilot")


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(config_file=args.config)


def _configure_logging(settings: Settings) -> None:
    from lanpilot.core.redaction import RedactingFormatter, redactor

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(redactor, "%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=settings.log_level.upper(), handlers=[handler], force=True)


def _migrate(settings: Settings) -> None:
    from lanpilot.db import migrate

    result = migrate.upgrade(settings.database_path)
    if result.backup is not None:
        logger.info("database backed up to %s", result.backup)
    if result.changed:
        logger.info("database schema upgraded: %s -> %s", result.previous, result.current)


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from lanpilot.app import create_app
    from lanpilot.db import migrate

    settings = _settings(args)
    _configure_logging(settings)
    app = create_app(settings)  # fails here, before anything is touched, if no key is set

    if settings.auto_migrate:
        _migrate(settings)
    elif migrate.current_revision(settings.database_path) != migrate.head_revision():
        print(
            "lanpilot: the database schema is not current; run 'lanpilot db upgrade'",
            file=sys.stderr,
        )
        return 1

    if not settings.server.is_https:
        logger.warning(
            "public_url is not https: session cookies are sent without the Secure flag "
            "and passwords travel unencrypted"
        )

    proxies = settings.server.trusted_proxies
    uvicorn.run(
        app,
        host=settings.server.bind,
        port=settings.server.port,
        proxy_headers=bool(proxies),
        forwarded_allow_ips=proxies,
        server_header=False,
        log_level=settings.log_level,
        log_config=None,  # keep the handler configured above, so Uvicorn's lines are redacted too
    )
    return 0


def _cmd_db_upgrade(args: argparse.Namespace) -> int:
    from lanpilot.db import migrate

    settings = _settings(args)
    result = migrate.upgrade(settings.database_path)
    if result.backup is not None:
        print(f"backup written to {result.backup}")
    if result.changed:
        print(f"upgraded {settings.database_path}: {result.previous} -> {result.current}")
    else:
        print(f"{settings.database_path} is already at revision {result.current}")
    return 0


def _cmd_db_current(args: argparse.Namespace) -> int:
    from lanpilot.db import migrate

    settings = _settings(args)
    current = migrate.current_revision(settings.database_path)
    head = migrate.head_revision()
    print(f"database: {settings.database_path}")
    print(f"current:  {current or 'none'}")
    print(f"newest:   {head}")
    return 0 if current == head else 1


class CommandError(Exception):
    """A command cannot be carried out; the message is printed without a traceback."""


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise CommandError("no password on standard input")
        return line.rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Password again: ") != password:
        raise CommandError("the passwords do not match")
    return password


def _prepare_database(settings: Settings) -> None:
    """Bring the schema up to date, or refuse if that is switched off and it is not."""
    from lanpilot.db import migrate

    if settings.auto_migrate:
        migrate.upgrade(settings.database_path)
    elif migrate.current_revision(settings.database_path) != migrate.head_revision():
        raise CommandError("the database schema is not current; run 'lanpilot db upgrade'")


def _with_database(args: argparse.Namespace, work: Callable[[AsyncSession], Awaitable[int]]) -> int:
    """Run ``work`` in a database session, with the schema brought up to date first."""
    from lanpilot.db.engine import create_engine, create_sessionmaker

    settings = _settings(args)
    _prepare_database(settings)

    async def run() -> int:
        engine = create_engine(settings.database_path)
        try:
            async with create_sessionmaker(engine)() as db:
                return await work(db)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _cmd_user_create(args: argparse.Namespace) -> int:
    from lanpilot import audit
    from lanpilot.auth import users
    from lanpilot.auth.roles import Role

    async def work(db: AsyncSession) -> int:
        try:
            users.normalise_username(args.username)  # fail before asking for a password
            user = await users.create_user(db, args.username, _read_password(args), Role(args.role))
        except users.UserError as exc:
            raise CommandError(str(exc)) from exc
        audit.record(
            db,
            action="user.create",
            actor_name=audit.CLI_ACTOR,
            target_type="user",
            target_id=user.id,
            detail={"username": user.username, "role": user.role},
        )
        await db.commit()
        print(f"created user {user.username!r} with role {user.role}")
        return 0

    return _with_database(args, work)


def _cmd_user_set_password(args: argparse.Namespace) -> int:
    from lanpilot import audit
    from lanpilot.auth import users

    async def work(db: AsyncSession) -> int:
        try:
            user = await users.get_by_username(db, users.normalise_username(args.username))
            if user is None:
                raise CommandError(f"no user named {args.username!r}")
            await users.set_password(db, user, _read_password(args))
        except users.UserError as exc:
            raise CommandError(str(exc)) from exc
        audit.record(
            db,
            action="user.password_reset",
            actor_name=audit.CLI_ACTOR,
            target_type="user",
            target_id=user.id,
            detail={"username": user.username},
        )
        await db.commit()
        print(f"password of {user.username!r} set; the user's sessions were signed out")
        return 0

    return _with_database(args, work)


def _cmd_user_list(args: argparse.Namespace) -> int:
    from lanpilot.auth import users

    async def work(db: AsyncSession) -> int:
        for user in await users.list_users(db):
            state = "active" if user.is_active else "disabled"
            print(f"{user.username:<24} {user.role:<10} {state}")
        return 0

    return _with_database(args, work)


def _cmd_secret_generate_key(args: argparse.Namespace) -> int:
    from lanpilot.core import secrets

    if args.output is None:
        print(secrets.generate_key())
        return 0
    try:
        secrets.write_key_file(args.output)
    except FileExistsError:
        raise CommandError(
            f"{args.output} already exists; a new key would make stored passwords unreadable"
        ) from None
    except OSError as exc:
        raise CommandError(f"cannot write {args.output}: {exc.strerror}") from exc
    print(f"key written to {args.output} (mode 600)")
    return 0


def _cmd_images_prefetch(args: argparse.Namespace) -> int:
    """Look online for the images of every stored map that has none (before a LAN party)."""
    from collections import Counter

    from lanpilot.core import image_lookup, maps, servers
    from lanpilot.core.adapters.registry import load_registry
    from lanpilot.core.map_images import MapImageIndex
    from lanpilot.db.engine import create_engine, create_sessionmaker

    settings = _settings(args)
    _configure_logging(settings)
    if not settings.images.online:
        raise CommandError("looking for images online is switched off (images.online)")
    _prepare_database(settings)

    async def run() -> int:
        engine = create_engine(settings.database_path)
        sessionmaker = create_sessionmaker(engine)
        registry = load_registry()
        index = MapImageIndex()
        lookup = image_lookup.ImageLookup(
            settings.images,
            index=index,
            sessionmaker=sessionmaker,
            data_dir=settings.data_dir,
            registry=registry,
        )
        outcomes: Counter[image_lookup.Outcome] = Counter()
        try:
            async with sessionmaker() as db:
                await index.load(db)
                work = []
                for server in await servers.list_servers(db):
                    adapter = registry.get(server.adapter_id)
                    if adapter is None or (args.server is not None and server.id != args.server):
                        continue
                    for row in await maps.list_maps(db, server.id):
                        ref = maps.to_ref(row)
                        work.append(
                            image_lookup.Wanted(
                                adapter.id,
                                adapter.image_lookup,
                                ref,
                                image_lookup.Reach.MISSING,
                            )
                        )
            async with lookup.open_fetcher() as fetcher:
                for number, wanted in enumerate(work, start=1):
                    outcome = await lookup.lookup(fetcher, wanted)
                    outcomes[outcome] += 1
                    if outcome is not image_lookup.Outcome.SKIPPED:
                        print(f"[{number}/{len(work)}] {wanted.ref.key}: {outcome.value}")
                        await asyncio.sleep(image_lookup.PAUSE_SECONDS)
        finally:
            await engine.dispose()
        print(
            f"{outcomes[image_lookup.Outcome.FOUND]} found, "
            f"{outcomes[image_lookup.Outcome.NOT_FOUND]} not found, "
            f"{outcomes[image_lookup.Outcome.FAILED]} could not be checked, "
            f"{outcomes[image_lookup.Outcome.SKIPPED]} skipped (image present or removed)"
        )
        return 0

    return asyncio.run(run())


def _cmd_scan(args: argparse.Namespace) -> int:
    """Look for game servers in the scan networks once and print what answered."""
    from pydantic import ValidationError

    from lanpilot.config import ScanSettings
    from lanpilot.core.adapters.registry import load_registry
    from lanpilot.core.scan import Scanner

    settings = _settings(args)
    _configure_logging(settings)
    scan = settings.scan
    if args.network:
        try:
            scan = ScanSettings(networks=args.network, timeout_seconds=scan.timeout_seconds)
        except ValidationError as exc:
            raise CommandError(exc.errors(include_input=False)[0]["msg"]) from exc
    if not scan.networks:
        raise CommandError("no networks to scan: set scan.networks or pass --network")

    result = asyncio.run(Scanner(load_registry(), scan).run())
    for item in result.found:
        server = item.server
        address = server.host if server.port is None else f"{server.host}:{server.port}"
        extra = "".join(f" {key}={value}" for key, value in server.values.items())
        game_type = f" ({server.game_type})" if server.game_type else ""
        print(f"{address:<21} {item.game}{game_type}: {server.name or '-'}{extra}")
    if result.failed:
        print(f"did not finish: {', '.join(result.failed)}")
    print(f"{len(result.found)} found in {', '.join(result.networks)} ({result.seconds:.1f} s)")
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    # mode="json" renders SecretStr values as asterisks.
    print(json.dumps(_settings(args).model_dump(mode="json"), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lanpilot",
        description="Monitor and remote-control game servers at LAN parties.",
    )
    parser.add_argument("--version", action="version", version=f"lanpilot {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="configuration file (default: $LANPILOT_CONFIG or /etc/lanpilot/lanpilot.toml)",
    )
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    serve = commands.add_parser("serve", help="run the web application")
    serve.set_defaults(handler=_cmd_serve)

    db = commands.add_parser("db", help="manage the database schema")
    db_commands = db.add_subparsers(title="db commands", metavar="COMMAND", required=True)
    db_upgrade = db_commands.add_parser("upgrade", help="create or upgrade the database")
    db_upgrade.set_defaults(handler=_cmd_db_upgrade)
    db_current = db_commands.add_parser("current", help="show the schema revision")
    db_current.set_defaults(handler=_cmd_db_current)

    user = commands.add_parser("user", help="manage user accounts")
    user_commands = user.add_subparsers(title="user commands", metavar="COMMAND", required=True)
    stdin_help = "read the password from standard input instead of prompting"
    user_create = user_commands.add_parser("create", help="create a user")
    user_create.add_argument("username")
    user_create.add_argument(
        "--role", choices=["viewer", "operator", "admin"], default="admin", help="default: admin"
    )
    user_create.add_argument("--password-stdin", action="store_true", help=stdin_help)
    user_create.set_defaults(handler=_cmd_user_create)
    user_password = user_commands.add_parser("set-password", help="set a user's password")
    user_password.add_argument("username")
    user_password.add_argument("--password-stdin", action="store_true", help=stdin_help)
    user_password.set_defaults(handler=_cmd_user_set_password)
    user_list = user_commands.add_parser("list", help="list users")
    user_list.set_defaults(handler=_cmd_user_list)

    secret = commands.add_parser("secret", help="manage the credential encryption key")
    secret_commands = secret.add_subparsers(
        title="secret commands", metavar="COMMAND", required=True
    )
    secret_generate = secret_commands.add_parser(
        "generate-key", help="create a new key for encrypting server credentials"
    )
    secret_generate.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="write the key to FILE (mode 600) instead of printing it",
    )
    secret_generate.set_defaults(handler=_cmd_secret_generate_key)

    images = commands.add_parser("images", help="manage map images")
    images_commands = images.add_subparsers(
        title="images commands", metavar="COMMAND", required=True
    )
    images_prefetch = images_commands.add_parser(
        "prefetch", help="look online for the images of all known maps that have none"
    )
    images_prefetch.add_argument(
        "--server", type=int, metavar="ID", help="only the maps of the server with this id"
    )
    images_prefetch.set_defaults(handler=_cmd_images_prefetch)

    scan = commands.add_parser("scan", help="look for game servers on the LAN once")
    scan.add_argument(
        "--network",
        action="append",
        metavar="CIDR",
        help="network to scan instead of scan.networks (repeatable)",
    )
    scan.set_defaults(handler=_cmd_scan)

    config = commands.add_parser("config", help="inspect the configuration")
    config_commands = config.add_subparsers(
        title="config commands", metavar="COMMAND", required=True
    )
    config_show = config_commands.add_parser(
        "show", help="print the effective configuration with secrets masked"
    )
    config_show.set_defaults(handler=_cmd_config_show)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Whatever the commands create (database, backups, key, images) is for the owner only.
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return int(handler(args))
    except ConfigError as exc:
        print(f"lanpilot: configuration error: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"lanpilot: {exc}", file=sys.stderr)
        return 1
