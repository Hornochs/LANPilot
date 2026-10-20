import logging
import stat
from pathlib import Path

import pytest

from lanpilot.cli import main
from lanpilot.config import ConfigError, load_settings
from lanpilot.core import secrets
from lanpilot.core.redaction import PLACEHOLDER, RedactingFormatter, SecretRedactor
from tests.conftest import PUBLIC_URL, SECRET_KEY

CONTEXT = secrets.server_context("cs16", "10.0.0.5", 27015)


@pytest.fixture
def box() -> secrets.SecretBox:
    return secrets.SecretBox(secrets._decode_key(SECRET_KEY, "test"))


def test_round_trip(box: secrets.SecretBox) -> None:
    token = box.encrypt({"rcon_password": "hunter2", "admin": "ü-ñ-密码"}, context=CONTEXT)

    assert token.startswith("v1:")
    assert "hunter2" not in token
    assert box.decrypt(token, context=CONTEXT) == {"rcon_password": "hunter2", "admin": "ü-ñ-密码"}


def test_same_values_encrypt_differently_each_time(box: secrets.SecretBox) -> None:
    first = box.encrypt({"p": "hunter2"}, context=CONTEXT)
    second = box.encrypt({"p": "hunter2"}, context=CONTEXT)

    assert first != second


@pytest.mark.parametrize(
    "other",
    [
        secrets.server_context("cs16", "10.0.0.66", 27015),  # pointed at another host
        secrets.server_context("cs16", "10.0.0.5", 27016),
        secrets.server_context("demo", "10.0.0.5", 27015),
    ],
)
def test_credentials_are_bound_to_adapter_and_address(box: secrets.SecretBox, other: str) -> None:
    token = box.encrypt({"p": "hunter2"}, context=CONTEXT)

    with pytest.raises(secrets.SecretError):
        box.decrypt(token, context=other)


def test_host_case_does_not_matter_for_the_binding(box: secrets.SecretBox) -> None:
    token = box.encrypt({"p": "x"}, context=secrets.server_context("cs16", "Game.LAN", 1))

    assert box.decrypt(token, context=secrets.server_context("cs16", "game.lan", 1)) == {"p": "x"}


def test_wrong_key_and_tampering_are_detected(box: secrets.SecretBox) -> None:
    token = box.encrypt({"p": "hunter2"}, context=CONTEXT)
    other_box = secrets.SecretBox(b"\x01" * 32)
    flipped = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")

    for candidate, used_box in ((token, other_box), (flipped, box), ("v1:!!!", box), ("v9:x", box)):
        with pytest.raises(secrets.SecretError) as exc:
            used_box.decrypt(candidate, context=CONTEXT)
        assert "hunter2" not in str(exc.value)


def test_generated_keys_are_valid_and_unique() -> None:
    first, second = secrets.generate_key(), secrets.generate_key()

    assert first != second
    assert len(secrets._decode_key(first, "generated")) == 32


def settings_with(**env: str) -> object:
    return load_settings(environ={"LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL, **env})


def test_key_from_environment() -> None:
    settings = load_settings(
        environ={"LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL, "LANPILOT_SECRET_KEY": SECRET_KEY}
    )

    assert len(secrets.load_key(settings)) == 32


def test_key_from_file(tmp_path: Path) -> None:
    key_file = tmp_path / "secret.key"
    secrets.write_key_file(key_file)
    settings = load_settings(
        environ={
            "LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL,
            "LANPILOT_SECRET_KEY_FILE": str(key_file),
        }
    )

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert len(secrets.load_key(settings)) == 32
    with pytest.raises(FileExistsError):
        secrets.write_key_file(key_file)  # never silently replace a key


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "no secret_key or secret_key_file"),
        ({"LANPILOT_SECRET_KEY": "too-short"}, "not a valid key"),
        ({"LANPILOT_SECRET_KEY": "not base64 !!"}, "not a valid key"),
        ({"LANPILOT_SECRET_KEY_FILE": "/nonexistent/secret.key"}, "cannot read"),
        (
            {"LANPILOT_SECRET_KEY": SECRET_KEY, "LANPILOT_SECRET_KEY_FILE": "/etc/x"},
            "not both",
        ),
    ],
)
def test_key_configuration_errors(env: dict[str, str], message: str) -> None:
    settings = load_settings(environ={"LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL, **env})

    with pytest.raises(ConfigError, match=message) as exc:
        secrets.load_key(settings)
    assert "too-short" not in str(exc.value)


def test_key_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    key_file = tmp_path / "secret.key"
    key_file.write_text(SECRET_KEY)
    key_file.chmod(0o644)
    settings = load_settings(
        environ={
            "LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL,
            "LANPILOT_SECRET_KEY_FILE": str(key_file),
        }
    )

    with pytest.raises(ConfigError, match="chmod 600"):
        secrets.load_key(settings)


def test_cli_generates_a_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["secret", "generate-key"]) == 0
    printed = capsys.readouterr().out.strip()
    assert len(secrets._decode_key(printed, "printed")) == 32

    target = tmp_path / "secret.key"
    assert main(["secret", "generate-key", "--output", str(target)]) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.read_text()

    assert main(["secret", "generate-key", "--output", str(target)]) == 1
    assert target.read_text() == before
    assert "already exists" in capsys.readouterr().err


def test_serve_refuses_to_start_without_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANPILOT_SERVER__PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("LANPILOT_DATA_DIR", str(tmp_path / "data"))

    assert main(["serve"]) == 2

    assert "no secret_key or secret_key_file" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()  # nothing was created before the check


def test_redactor_replaces_known_secrets() -> None:
    redactor = SecretRedactor()
    redactor.add("hunter2-rcon")
    redactor.add("hunter2-rcon-extended")
    redactor.add("abc")  # too short to register

    text = redactor.redact("rcon hunter2-rcon-extended failed; retry with hunter2-rcon (abc)")

    assert text == f"rcon {PLACEHOLDER} failed; retry with {PLACEHOLDER} (abc)"


def test_redacting_formatter_covers_messages_arguments_and_tracebacks() -> None:
    redactor = SecretRedactor()
    redactor.add("hunter2-rcon")
    formatter = RedactingFormatter(redactor, "%(levelname)s %(message)s")
    try:
        raise ConnectionError("udp://10.0.0.5:27015 rcon hunter2-rcon refused")
    except ConnectionError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "command %s failed",
            ("say hunter2-rcon",),
            sys.exc_info(),
        )

    output = formatter.format(record)

    assert "hunter2-rcon" not in output
    assert output.count(PLACEHOLDER) >= 2
    assert "ConnectionError" in output


def test_key_file_is_written_completely_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "secret.key"

    def broken_fsync(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("lanpilot.core.secrets.os.fsync", broken_fsync)
    with pytest.raises(OSError):
        secrets.write_key_file(key_file)
    assert list(tmp_path.iterdir()) == []  # neither a partial key nor a temporary file

    monkeypatch.undo()
    secrets.write_key_file(key_file)
    assert [path.name for path in tmp_path.iterdir()] == ["secret.key"]
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
