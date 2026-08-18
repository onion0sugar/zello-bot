"""Zello Bot — prosty notifier: MSSQL → wynik zapytania → wiadomość na kanał Zello.

Bot NIE zapamiętuje obsłużonych zamówień: za każdym razem, gdy zapytanie
zwróci wiersz, wysyła powiadomienie — nawet jeśli to ten sam wiersz, co
w poprzednim pollingu.

Warianty:
    python main.py                  # serwis: polling co POLL_INTERVAL sekund
    python main.py --test-db        # sprawdź połączenie MSSQL (SELECT 1)
    python main.py --test-text      # wyślij test tekstowy na kanał Zello
    python main.py --test-voice     # wyślij VOICE_FILE na kanał Zello
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from types import SimpleNamespace

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore[assignment]

from dotenv import load_dotenv

from audio import VoiceFileError, codec_header, encode_opus, wav_to_pcm
from zello import Zello, ZelloError

logger = logging.getLogger("bot")

# ============================================================================
# ZMIEŃ TU ZAPYTANIE — jedyne miejsce dostosowania do Twojej ERP.
# Bot NIE zapamiętuje zamówień: każde zwrócenie wiersza = powiadomienie
# (nawet dla tego samego zamówienia co w poprzednim pollingu).
# Własny warunek wpisz w WHERE, np. status = 'oczekuje'. Max 1 wiersz.
# ============================================================================
GET_NEXT_ORDER_SQL = """
SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > 0
ORDER BY id ASC;
"""
# ============================================================================

RECONNECT_DELAY = 5          # sekundy przerwy po błędzie
DEFAULT_TEXT = "🔔 Nowe zamówienie: {}"


# --- konfiguracja (.env) -----------------------------------------------------


def load_config() -> SimpleNamespace:
    load_dotenv()

    def env(name: str, default: str | None = None, required: bool = False) -> str | None:
        value = os.environ.get(name, default)
        if required and not (value and value.strip()):
            raise SystemExit(
                f"Brak wymaganej zmiennej środowiskowej: {name} "
                f"(skopiuj .env.example do .env)"
            )
        return value.strip() if value else value

    def flag(name: str, default: str = "true") -> bool:
        return (env(name, default) or "").lower() in {"1", "true", "yes", "on"}

    return SimpleNamespace(
        mssql_server=env("MSSQL_SERVER", required=True),
        mssql_port=int(env("MSSQL_PORT", "1433")),
        mssql_database=env("MSSQL_DATABASE", required=True),
        mssql_username=env("MSSQL_USERNAME", required=True),
        mssql_password=env("MSSQL_PASSWORD", required=True),
        mssql_encrypt=env("MSSQL_ENCRYPT", "yes"),
        mssql_trust_server_certificate=env("MSSQL_TRUST_SERVER_CERTIFICATE", "yes"),
        # Zello: Work (ZELLO_NETWORK) LUB Friends & Family (ZELLO_AUTH_TOKEN)
        zello_network=env("ZELLO_NETWORK"),
        zello_username=env("ZELLO_USERNAME", required=True),
        zello_password=env("ZELLO_PASSWORD", required=True),
        zello_channel=env("ZELLO_CHANNEL", required=True),
        zello_auth_token=env("ZELLO_AUTH_TOKEN", ""),
        poll_interval=max(1, int(env("POLL_INTERVAL", "3"))),
        send_text=flag("SEND_TEXT", "true"),
        send_voice=flag("SEND_VOICE", "true"),
        voice_file=env("VOICE_FILE", "audio/new_order.wav"),
    )


def _build_zello(cfg: SimpleNamespace) -> Zello:
    """Tworzy klienta Zello (Work lub F&F wg konfiguracji)."""
    return Zello(
        network=cfg.zello_network or "",
        username=cfg.zello_username,
        password=cfg.zello_password,
        channel=cfg.zello_channel,
        auth_token=cfg.zello_auth_token,
    )


# --- MSSQL --------------------------------------------------------------------


def connect_db(cfg: SimpleNamespace):
    """Połączenie pyodbc. Bot wykonuje WYŁĄCZNIE SELECT — baza tylko do odczytu.

    ApplicationIntent=ReadOnly to jawna deklaracja intencji tylko-do-odczytu;
    przy Always On Availability Groups połączenie trafia do repliki
    przeznaczonej do odczytu. Na zwykłym serwerze parametr jest ignorowany.
    """
    if pyodbc is None:
        raise RuntimeError("pyodbc nie jest zainstalowane — pip install -r requirements.txt")
    # Instancja nazwana (np. 192.168.24.22\SERWISKOPB2B) NIE ma portu w adresie
    # — numer portu odczytuje SQL Browser (UDP 1434). Port dopisujemy tylko
    # do zwykłego adresu.
    server = cfg.mssql_server
    if "\\" not in server:
        server = f"{server},{cfg.mssql_port}"
    dsn = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={cfg.mssql_database};"
        f"UID={cfg.mssql_username};"
        f"PWD={cfg.mssql_password};"
        f"Encrypt={cfg.mssql_encrypt};TrustServerCertificate={cfg.mssql_trust_server_certificate};"
        "ApplicationIntent=ReadOnly"
    )
    cnxn = pyodbc.connect(dsn, timeout=10, autocommit=True)
    cnxn.timeout = 15
    logger.info("Connected to MSSQL")
    return cnxn


def get_next_order(cursor) -> tuple[int, str] | None:
    """Wykonaj zapytanie. Wiersz zwrócony → powiadomienie (bez zapamiętywania)."""
    cursor.execute(GET_NEXT_ORDER_SQL)
    row = cursor.fetchone()
    return (int(row[0]), str(row[1])) if row else None


# --- głos ---------------------------------------------------------------------


def load_voice_packets(cfg: SimpleNamespace) -> list[bytes]:
    """WAV → PCM (FFmpeg) → ramki Opus. Raz na start — brak pośredniego .opus na dysku."""
    pcm = wav_to_pcm(cfg.voice_file)
    packets = encode_opus(pcm)
    logger.info("Voice ready: %d packets (%.1f s)", len(packets), len(packets) * 0.02)
    return packets


async def notify(z: Zello, cfg: SimpleNamespace, order_number: str, voice_packets: list[bytes]) -> None:
    if cfg.send_text:
        await z.send_text_message(cfg.zello_channel, DEFAULT_TEXT.format(order_number))
        logger.info("Text sent")
    if cfg.send_voice:
        logger.info("Sending voice")
        await z.send_voice(cfg.zello_channel, voice_packets, codec_header())
        logger.info("Voice sent")


# --- pętla serwisu ------------------------------------------------------------


async def run_service(cfg: SimpleNamespace, stop: asyncio.Event | None = None) -> int:
    stop = stop or asyncio.Event()
    db = connect_db(cfg)

    voice_packets = load_voice_packets(cfg) if cfg.send_voice else []  # fail-fast przy braku pliku

    z = _build_zello(cfg)
    await z.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    while not stop.is_set():
        try:
            with db.cursor() as cursor:
                order = get_next_order(cursor)
            if order:
                order_id, order_number = order
                logger.info("New order id=%s", order_id)
                await notify(z, cfg, order_number, voice_packets)
            # stały rytm pollingu — także wtedy, gdy zapytanie ciągle zwraca ten sam wiersz
            await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval)
        except asyncio.TimeoutError:
            pass  # przerwa pollingu — oczekiwane zachowanie, nie błąd
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if pyodbc is not None and isinstance(exc, pyodbc.Error):
                logger.error("MSSQL error: %s", exc)
                try:
                    db.close()
                except Exception:
                    pass
                try:
                    db = connect_db(cfg)
                except Exception:
                    logger.error("MSSQL reconnect failed — next try in %d s", RECONNECT_DELAY)
            else:
                logger.exception("Error in main loop")
            await asyncio.sleep(RECONNECT_DELAY)

    await z.close()
    db.close()
    logger.info("Stopped")
    return 0


# --- komendy testowe ----------------------------------------------------------


def test_db(cfg: SimpleNamespace) -> int:
    try:
        db = connect_db(cfg)
        with db.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            assert row and row[0] == 1
        db.close()
    except Exception as exc:
        logger.error("DB test FAILED: %s", exc)
        return 1
    logger.info("DB test OK")
    return 0


async def test_text(cfg: SimpleNamespace) -> int:
    z = _build_zello(cfg)
    await z.start()
    try:
        await z.wait_ready()
        await z.send_text_message(cfg.zello_channel, "Test wiadomości z bota MSSQL")
    except Exception as exc:
        logger.error("Text test FAILED: %s", exc)
        return 1
    finally:
        await z.close()
    logger.info("Text test OK")
    return 0


async def test_voice(cfg: SimpleNamespace) -> int:
    try:
        packets = load_voice_packets(cfg)
    except VoiceFileError as exc:
        logger.error("Voice test FAILED: %s", exc)
        return 1
    z = _build_zello(cfg)
    await z.start()
    try:
        await z.wait_ready()
        await z.send_voice(cfg.zello_channel, packets, codec_header())
    except Exception as exc:
        logger.error("Voice test FAILED: %s", exc)
        return 1
    finally:
        await z.close()
    logger.info("Voice test OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Zello Bot: MSSQL -> Zello")
    parser.add_argument("--test-db", action="store_true", help="sprawdź połączenie MSSQL (SELECT 1)")
    parser.add_argument("--test-text", action="store_true", help="wyślij test tekstowy na kanał Zello")
    parser.add_argument("--test-voice", action="store_true", help="wyślij VOICE_FILE na kanał Zello")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.test_db:
        return test_db(cfg)
    if args.test_text:
        return asyncio.run(test_text(cfg))
    if args.test_voice:
        return asyncio.run(test_voice(cfg))
    try:
        return asyncio.run(run_service(cfg))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
