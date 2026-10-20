from pathlib import Path

import pytest

from lanpilot.config import ConfigError, load_settings


def load(**env: str) -> object:
    return load_settings(environ=env)


def test_defaults_with_only_public_url() -> None:
    settings = load_settings(environ={"LANPILOT_SERVER__PUBLIC_URL": "https://lan.example"})

    assert settings.server.bind == "127.0.0.1"
    assert settings.server.port == 9030
    assert settings.server.allowed_hosts == []
    assert settings.server.trusted_proxies == ["127.0.0.1"]
    assert settings.data_dir == Path("/var/lib/lanpilot")
    assert settings.database_path == Path("/var/lib/lanpilot/lanpilot.db")
    assert settings.secret_key is None
    assert settings.auto_migrate is True


def test_public_url_is_required() -> None:
    with pytest.raises(ConfigError, match="server"):
        load_settings(environ={})


@pytest.mark.parametrize(
    ("url", "origin", "host", "root_path", "https"),
    [
        ("https://LAN.Example/", "https://lan.example", "lan.example", "", True),
        ("http://10.0.0.5:8080", "http://10.0.0.5:8080", "10.0.0.5", "", False),
        ("https://lan.example/pilot/", "https://lan.example", "lan.example", "/pilot", True),
        ("http://[fd00::1]:9030", "http://[fd00::1]:9030", "fd00::1", "", False),
    ],
)
def test_public_url_derivations(
    url: str, origin: str, host: str, root_path: str, https: bool
) -> None:
    server = load_settings(environ={"LANPILOT_SERVER__PUBLIC_URL": url}).server

    assert server.public_origin == origin
    assert server.public_host == host
    assert server.root_path == root_path
    assert server.is_https is https


@pytest.mark.parametrize(
    "url",
    [
        "lan.example",
        "ftp://lan.example",
        "https://",
        "https://user:pw@lan.example",
        "https://lan.example/?x=1",
        "https://lan.example/#top",
        "https://lan.example:notaport",
    ],
)
def test_invalid_public_url_is_rejected(url: str) -> None:
    with pytest.raises(ConfigError, match=r"server\.public_url"):
        load_settings(environ={"LANPILOT_SERVER__PUBLIC_URL": url})


def test_comma_separated_lists_from_environment() -> None:
    settings = load_settings(
        environ={
            "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
            "LANPILOT_SERVER__ALLOWED_HOSTS": "10.0.0.5, Pilot.LAN ,",
            "LANPILOT_SERVER__TRUSTED_PROXIES": "127.0.0.1,10.0.0.0/24",
        }
    )

    assert settings.server.allowed_hosts == ["10.0.0.5", "pilot.lan"]
    assert settings.server.trusted_proxies == ["127.0.0.1", "10.0.0.0/24"]


@pytest.mark.parametrize("hosts", ["*", "*.lan.example", "lan.example/path", "two words"])
def test_invalid_allowed_hosts_are_rejected(hosts: str) -> None:
    with pytest.raises(ConfigError, match=r"server\.allowed_hosts"):
        load_settings(
            environ={
                "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
                "LANPILOT_SERVER__ALLOWED_HOSTS": hosts,
            }
        )


def test_invalid_trusted_proxy_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"server\.trusted_proxies"):
        load_settings(
            environ={
                "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
                "LANPILOT_SERVER__TRUSTED_PROXIES": "proxy.lan",
            }
        )


def test_toml_file_is_read_and_environment_wins(tmp_path: Path) -> None:
    config = tmp_path / "lanpilot.toml"
    config.write_text(
        'data_dir = "/srv/lanpilot"\n'
        "[server]\n"
        'public_url = "https://from-file.example"\n'
        "port = 9100\n"
        'allowed_hosts = ["10.0.0.5"]\n'
    )

    settings = load_settings(config_file=config, environ={"LANPILOT_SERVER__PORT": "9200"})

    assert settings.server.public_url == "https://from-file.example"
    assert settings.server.allowed_hosts == ["10.0.0.5"]
    assert settings.server.port == 9200
    assert settings.data_dir == Path("/srv/lanpilot")


def test_config_file_from_environment_variable(tmp_path: Path) -> None:
    config = tmp_path / "custom.toml"
    config.write_text('[server]\npublic_url = "https://via-env.example"\n')

    settings = load_settings(environ={"LANPILOT_CONFIG": str(config)})

    assert settings.server.public_url == "https://via-env.example"


def test_empty_environment_value_does_not_override_file(tmp_path: Path) -> None:
    config = tmp_path / "lanpilot.toml"
    config.write_text('[server]\npublic_url = "https://lan.example"\nallowed_hosts = ["a.lan"]\n')

    settings = load_settings(
        config_file=config,
        environ={"LANPILOT_SERVER__ALLOWED_HOSTS": "", "LANPILOT_SECRET_KEY": ""},
    )

    assert settings.server.allowed_hosts == ["a.lan"]
    assert settings.secret_key is None


def test_missing_explicit_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(config_file=tmp_path / "missing.toml", environ={})


def test_broken_toml_is_an_error(tmp_path: Path) -> None:
    config = tmp_path / "lanpilot.toml"
    config.write_text("[server\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(config_file=config, environ={})


def test_unknown_setting_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"server\.pubilc_url"):
        load_settings(
            environ={
                "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
                "LANPILOT_SERVER__PUBILC_URL": "https://typo.example",
            }
        )


def test_error_message_never_contains_the_offending_value() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(
            environ={
                "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
                "LANPILOT_SERVER__PORT": "hunter2-not-a-port",
            }
        )

    assert "server.port" in str(exc.value)
    assert "hunter2" not in str(exc.value)


def test_secret_key_is_masked_when_dumped() -> None:
    settings = load_settings(
        environ={
            "LANPILOT_SERVER__PUBLIC_URL": "https://lan.example",
            "LANPILOT_SECRET_KEY": "super-secret-value",
        }
    )

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.model_dump(mode="json"))
    assert settings.secret_key is not None
    assert settings.secret_key.get_secret_value() == "super-secret-value"
