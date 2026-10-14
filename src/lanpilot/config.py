"""Configuration loading.

Precedence, lowest to highest: built-in defaults, the TOML file, environment variables.

Environment variables are named ``LANPILOT_<SECTION>__<KEY>`` (``LANPILOT_<KEY>`` for
top-level settings). Empty environment values are ignored, so an unset line in an
``EnvironmentFile`` never overrides the TOML file.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

ENV_PREFIX = "LANPILOT_"
CONFIG_FILE_ENV = "LANPILOT_CONFIG"
DEFAULT_CONFIG_FILE = Path("/etc/lanpilot/lanpilot.toml")
DATABASE_FILENAME = "lanpilot.db"


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid."""


def _split_commas(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


CommaList = Annotated[list[str], BeforeValidator(_split_commas)]


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bind: str = "127.0.0.1"
    port: int = Field(default=9030, ge=1, le=65535)
    public_url: str
    allowed_hosts: CommaList = Field(default_factory=list)
    trusted_proxies: CommaList = Field(default_factory=lambda: ["127.0.0.1"])

    @field_validator("public_url")
    @classmethod
    def _normalise_public_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https"):
            raise ValueError("must start with http:// or https://")
        if not parts.hostname:
            raise ValueError("must contain a host name")
        if parts.username or parts.password:
            raise ValueError("must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query string or fragment")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        host = parts.hostname.lower()
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        netloc = host if port is None else f"{host}:{port}"
        return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"

    @field_validator("allowed_hosts")
    @classmethod
    def _check_allowed_hosts(cls, value: list[str]) -> list[str]:
        hosts = []
        for host in value:
            if "*" in host:
                raise ValueError("wildcards are not allowed; list every host name explicitly")
            if "/" in host or any(ch.isspace() for ch in host):
                raise ValueError(f"{host!r} is not a host name")
            hosts.append(host.lower())
        return hosts

    @field_validator("trusted_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: list[str]) -> list[str]:
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(f"{entry!r} is not an IP address or network") from exc
        return value

    @property
    def is_https(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def public_origin(self) -> str:
        """Scheme and authority, as a browser sends it in the ``Origin`` header."""
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def public_host(self) -> str:
        """Host name without port, as used for the ``Host`` header check."""
        return urlsplit(self.public_url).hostname or ""

    @property
    def root_path(self) -> str:
        """Sub-path LANPilot is served from; empty when it lives at ``/``."""
        return urlsplit(self.public_url).path


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A session ends after this much inactivity ...
    session_idle_minutes: int = Field(default=720, ge=5)
    # ... and in any case this long after the login.
    session_lifetime_hours: int = Field(default=168, ge=1)


class PollingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # How often every game server is asked for its status ...
    interval_seconds: float = Field(default=10, ge=2, le=3600)
    # ... and how long LANPilot waits for an answer.
    timeout_seconds: float = Field(default=8, ge=1, le=60)


# Online sources of map images, in the order they are asked (lanpilot.core.image_sources).
IMAGE_SOURCES = ("steam_workshop", "gamebanana", "gametracker", "tmx", "unreal_archive")


class ImageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Look for map images on the internet at all. Off for LAN parties without internet.
    online: bool = True
    # Which sources to ask, in this order.
    sources: CommaList = Field(default_factory=lambda: list(IMAGE_SOURCES))
    # After no source had an image, wait this long before asking again.
    retry_days: int = Field(default=7, ge=1, le=365)

    @field_validator("sources")
    @classmethod
    def _check_sources(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in IMAGE_SOURCES]
        if unknown:
            raise ValueError(
                f"unknown source(s) {', '.join(unknown)}; known: {', '.join(IMAGE_SOURCES)}"
            )
        return list(dict.fromkeys(value))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server: ServerSettings
    auth: AuthSettings = Field(default_factory=AuthSettings)
    polling: PollingSettings = Field(default_factory=PollingSettings)
    images: ImageSettings = Field(default_factory=ImageSettings)
    data_dir: Path = Path("/var/lib/lanpilot")
    secret_key: SecretStr | None = None
    secret_key_file: Path | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    auto_migrate: bool = True
    # Offers a simulated game server for trying LANPilot out; not meant for production.
    enable_demo_adapter: bool = False
    # Lets administrators send raw console commands to game servers.
    enable_console: bool = False

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_FILENAME


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file {path} does not exist") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file {path} is not valid TOML: {exc}") from exc


def _apply_environment(data: dict[str, Any], environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX) or name == CONFIG_FILE_ENV or value == "":
            continue
        *sections, key = name[len(ENV_PREFIX) :].lower().split("__")
        target = data
        for section in sections:
            existing = target.setdefault(section, {})
            if not isinstance(existing, dict):
                raise ConfigError(f"{name} addresses {section!r}, which is not a section")
            target = existing
        target[key] = value


def _describe(error: ValidationError) -> str:
    # Deliberately omits the offending input: it may be a secret.
    lines = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"{location}: {item['msg']}")
    return "; ".join(lines)


def load_settings(
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load and validate the settings.

    ``config_file`` (or ``LANPILOT_CONFIG``) must exist when given. Without either, the
    default file is used if present; otherwise the environment alone has to be sufficient.
    """
    env = os.environ if environ is None else environ

    path = config_file
    if path is None and env.get(CONFIG_FILE_ENV):
        path = Path(env[CONFIG_FILE_ENV])
    if path is None and DEFAULT_CONFIG_FILE.is_file():
        path = DEFAULT_CONFIG_FILE

    data = _read_toml(path) if path is not None else {}
    _apply_environment(data, env)

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from exc
