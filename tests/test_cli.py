import json
from importlib.metadata import version
from pathlib import Path

import pytest

import lanpilot
from lanpilot.cli import main
from lanpilot.db import migrate


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]) -> dict[str, str]:
    for name, value in base_env.items():
        monkeypatch.setenv(name, value)
    return base_env


def test_version_flag_prints_version_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"lanpilot {lanpilot.__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: lanpilot" in capsys.readouterr().out


def test_unknown_argument_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-option"])

    assert exc.value.code == 2


def test_package_metadata_matches_module_version() -> None:
    assert version("lanpilot") == lanpilot.__version__


def test_missing_configuration_is_reported_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "show"]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("lanpilot: configuration error:")
    assert "Traceback" not in captured.err


def test_config_show_masks_the_secret_key(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANPILOT_SECRET_KEY", "super-secret-value")

    assert main(["config", "show"]) == 0

    output = capsys.readouterr().out
    assert "super-secret-value" not in output
    shown = json.loads(output)
    assert shown["server"]["public_url"] == "https://lanpilot.test"
    assert shown["secret_key"] == "**********"


def test_config_option_selects_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "lanpilot.toml"
    config.write_text('[server]\npublic_url = "https://from-file.example"\n')

    assert main(["--config", str(config), "config", "show"]) == 0

    assert json.loads(capsys.readouterr().out)["server"]["public_url"] == (
        "https://from-file.example"
    )


def test_db_upgrade_then_current(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    database = Path(env["LANPILOT_DATA_DIR"]) / "lanpilot.db"

    assert main(["db", "current"]) == 1
    assert "current:  none" in capsys.readouterr().out

    assert main(["db", "upgrade"]) == 0
    assert f"upgraded {database}" in capsys.readouterr().out

    assert main(["db", "upgrade"]) == 0
    assert "already at revision" in capsys.readouterr().out

    assert main(["db", "current"]) == 0
    assert f"current:  {migrate.head_revision()}" in capsys.readouterr().out


def test_serve_refuses_outdated_schema_when_auto_migrate_is_off(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANPILOT_AUTO_MIGRATE", "false")

    assert main(["serve"]) == 1

    assert "run 'lanpilot db upgrade'" in capsys.readouterr().err


def feed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_user_create_list_and_set_password(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Works on a fresh installation: the database is created on the way.
    feed(monkeypatch, "correct horse battery\n")
    assert main(["user", "create", "Admin", "--password-stdin"]) == 0
    assert "created user 'admin' with role admin" in capsys.readouterr().out

    feed(monkeypatch, "another long passphrase\n")
    assert main(["user", "create", "viewer1", "--role", "viewer", "--password-stdin"]) == 0
    capsys.readouterr()

    assert main(["user", "list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert [line.split() for line in listing] == [
        ["admin", "admin", "active"],
        ["viewer1", "viewer", "active"],
    ]

    feed(monkeypatch, "a third long passphrase\n")
    assert main(["user", "set-password", "admin", "--password-stdin"]) == 0
    assert "password of 'admin' set" in capsys.readouterr().out


def test_user_create_prompts_twice_and_compares(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["correct horse battery", "correct horse bttery"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    assert main(["user", "create", "admin"]) == 1

    assert "passwords do not match" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "password", "message"),
    [
        (["user", "create", "x"], "correct horse battery", "3 to 64 characters"),
        (["user", "create", "admin"], "short", "at least 10 characters"),
        (["user", "set-password", "ghost"], "correct horse battery", "no user named 'ghost'"),
    ],
)
def test_user_command_errors_are_reported_without_traceback(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    password: str,
    message: str,
) -> None:
    feed(monkeypatch, password + "\n")

    assert main([*arguments, "--password-stdin"]) == 1

    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert password not in captured.err + captured.out


def test_duplicate_user_is_rejected(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(monkeypatch, "correct horse battery\n")
    assert main(["user", "create", "admin", "--password-stdin"]) == 0
    feed(monkeypatch, "correct horse battery\n")

    assert main(["user", "create", "ADMIN", "--password-stdin"]) == 1

    assert "already exists" in capsys.readouterr().err
