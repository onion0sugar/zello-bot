"""Testy konfiguracji (.env → load_config)."""

import pytest

from config import ConfigError, load_config

REQUIRED = {
    "MSSQL_SERVER": "192.168.24.22\\SERWISKOPB2B",
    "MSSQL_DATABASE": "SerwisKop_Magazyn",
    "MSSQL_USERNAME": "serwiskop-ro",
    "MSSQL_PASSWORD": "haslo",
    "ZELLO_USERNAME": "sql_bot",
    "ZELLO_PASSWORD": "haslo",
    "ZELLO_CHANNEL": "Magazyn",
}


def _set_env(monkeypatch, values: dict[str, str]) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    for key in values:
        monkeypatch.setenv(key, values[key])


def test_load_config_reads_required_vars(monkeypatch):
    _set_env(monkeypatch, REQUIRED)
    cfg = load_config()
    assert cfg.mssql_server == "192.168.24.22\\SERWISKOPB2B"
    assert cfg.mssql_database == "SerwisKop_Magazyn"
    assert cfg.mssql_username == "serwiskop-ro"
    assert cfg.zello_channel == "Magazyn"


def test_missing_required_var_raises(monkeypatch):
    values = dict(REQUIRED)
    del values["MSSQL_SERVER"]
    _set_env(monkeypatch, values)
    with pytest.raises(ConfigError, match="MSSQL_SERVER"):
        load_config()


def test_defaults(monkeypatch):
    _set_env(monkeypatch, REQUIRED)
    cfg = load_config()
    assert cfg.mssql_port == 1433
    assert cfg.mssql_encrypt == "yes"
    assert cfg.mssql_trust_server_certificate == "yes"
    assert cfg.poll_interval == 3
    assert cfg.announce_interval == 30
    assert cfg.send_text is True
    assert cfg.send_voice is True
    assert cfg.zello_auth_token == ""
    assert cfg.zello_wait_online is True


def test_flags_parsed(monkeypatch):
    values = dict(REQUIRED)
    values.update(
        {
            "SEND_TEXT": "false",
            "SEND_VOICE": "no",
            "POLL_INTERVAL": "10",
            "ANNOUNCE_INTERVAL": "45",
        }
    )
    _set_env(monkeypatch, values)
    cfg = load_config()
    assert cfg.send_text is False
    assert cfg.send_voice is False
    assert cfg.poll_interval == 10
    assert cfg.announce_interval == 45


def test_fnf_auth_token_and_network_both_loaded(monkeypatch):
    values = dict(REQUIRED)
    values["ZELLO_AUTH_TOKEN"] = "dev-token"
    values["ZELLO_NETWORK"] = "moja_siec"
    _set_env(monkeypatch, values)
    cfg = load_config()
    assert cfg.zello_auth_token == "dev-token"
    assert cfg.zello_network == "moja_siec"
