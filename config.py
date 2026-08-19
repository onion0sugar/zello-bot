"""Konfiguracja aplikacji ładowana z .env / zmiennych środowiskowych."""

from __future__ import annotations

import os
from types import SimpleNamespace

from dotenv import load_dotenv


class ConfigError(Exception):
    """Brakująca lub błędna zmienna konfiguracyjna."""


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not (value and value.strip()):
        raise ConfigError(
            f"Brak wymaganej zmiennej środowiskowej: {name} "
            f"(skopiuj .env.example do .env)"
        )
    return value.strip() if value else value


def _flag(name: str, default: str = "true") -> bool:
    return (_env(name, default) or "").lower() in {"1", "true", "yes", "on"}


def load_config(env_file: str | None = None) -> SimpleNamespace:
    """Wczytaj konfigurację (nie nadpisuje istniejących zmiennych środowiska)."""
    load_dotenv(env_file)
    return SimpleNamespace(
        # MSSQL
        mssql_server=_env("MSSQL_SERVER", required=True),
        mssql_port=int(_env("MSSQL_PORT", "1433") or "1433"),
        mssql_database=_env("MSSQL_DATABASE", required=True),
        mssql_username=_env("MSSQL_USERNAME", required=True),
        mssql_password=_env("MSSQL_PASSWORD", required=True),
        mssql_encrypt=_env("MSSQL_ENCRYPT", "yes"),
        mssql_trust_server_certificate=_env("MSSQL_TRUST_SERVER_CERTIFICATE", "yes"),
        # Zello: Work (ZELLO_NETWORK) LUB Friends & Family (ZELLO_AUTH_TOKEN)
        zello_network=_env("ZELLO_NETWORK"),
        zello_username=_env("ZELLO_USERNAME", required=True),
        zello_password=_env("ZELLO_PASSWORD", required=True),
        zello_channel=_env("ZELLO_CHANNEL", required=True),
        zello_auth_token=_env("ZELLO_AUTH_TOKEN", ""),
        zello_wait_online=_flag("ZELLO_WAIT_ONLINE", "true"),
        # Zachowanie
        poll_interval=max(1, int(_env("POLL_INTERVAL", "3") or "3")),
        send_text=_flag("SEND_TEXT", "true"),
        send_voice=_flag("SEND_VOICE", "true"),
        voice_file=_env("VOICE_FILE", "audio/new_order.wav"),
    )
