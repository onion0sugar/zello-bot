"""Zello Bot — prosty notifier: MSSQL → wykrycie nowego rekordu → wiadomość na kanał Zello.

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
# ZMIEŃ TU ZAPYTANIE O ZAMÓWIENIA — jedyne miejsce dostosowania do Twojej ERP.
# Parametr (?) to ostatnie obsłużone ID. Zapytanie musi zwracać max 1 wiersz.
# ============================================================================
GET_NEXT_ORDER_SQL = """
SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > ?
ORDER BY id ASC;
"""
# ============================================================================

BOT_NAME = "orders"          # nazwa wiersza w dbo.zello_bot_state
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
        zello_network=env("ZELLO_NETWORK", required=True),
        zello_username=env("ZELLO_USERNAME", required=True),
        zello_password=env("ZELLO_PASSWORD", required=True),
        zello_channel=env("ZELLO_CHANNEL", required=True),
        poll_interval=max(1, int(env("POLL_INTERVAL", "3"))),
        send_text=flag("SEND_TEXT", "true"),
        send_voice=flag("SEND_VOICE", "true"),
        voice_file=env("VOICE_FILE", "audio/new_order.wav"),
    )


# --- MSSQL --------------------------------------------------------------------


def connect_db(cfg: SimpleNamespace):
    """Połączenie pyodbc (autocommit — każda instrukcja to własna transakcja)."""
    if pyodbc is None:
        raise RuntimeError("pyodbc nie jest zainstalowane — pip install -r requirements.txt")
    dsn = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={cfg.mssql_server},{cfg.mssql_port};"
        f"DATABASE={cfg.mssql_database};"
        f"UID={cfg.mssql_username};"
        f"PWD={cfg.mssql_password};"
        "Encrypt=yes;TrustServerCertificate=yes"
    )
    cnxn = pyodbc.connect(dsn, timeout=10, autocommit=True)
    cnxn.timeout = 15
    logger.info("Connected to MSSQL")
    return cnxn


def get_last_id(cursor) -> int | None:
    cursor.execute("SELECT last_id FROM dbo.zello_bot_state WHERE bot_name = ?", (BOT_NAME,))
    row = cursor.fetchone()
    return int(row[0]) if row else None


def set_last_id(cursor, value: int) -> None:
    cursor.execute("UPDATE dbo.zello_bot_state SET last_id = ? WHERE bot_name = ?", (value, BOT_NAME))
    if cursor.rowcount == 0:
        cursor.execute(
            "INSERT INTO dbo.zello_bot_state (bot_name, last_id) VALUES (?, ?)", (BOT_NAME, value)
        )


def init_state(cursor) -> None:
    """Pierwsze uruchomienie: punkt startowy = aktualne MAX(id) — bez historii."""
    if get_last_id(cursor) is not None:
        return
    cursor.execute("SELECT ISNULL(MAX(id), 0) FROM dbo.orders")
    start = int(cursor.fetchone()[0])
    set_last_id(cursor, start)
    logger.info("First run: last_id = MAX(id) = %d (historical orders skipped)", start)


def get_next_order(cursor, last_id: int) -> tuple[int, str] | None:
    cursor.execute(GET_NEXT_ORDER_SQL, (last_id,))
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
    with db.cursor() as cursor:
        init_state(cursor)

    voice_packets = load_voice_packets(cfg) if cfg.send_voice else []  # fail-fast przy braku pliku

    z = Zello(cfg.zello_network, cfg.zello_username, cfg.zello_password, cfg.zello_channel)
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
                last_id = get_last_id(cursor)
                if last_id is None:
                    init_state(cursor)
                    last_id = get_last_id(cursor)
                order = get_next_order(cursor, last_id)
            if order:
                order_id, order_number = order
                logger.info("New order id=%s", order_id)
                await notify(z, cfg, order_number, voice_packets)
                with db.cursor() as cursor:
                    set_last_id(cursor, order_id)
                logger.info("Updated last_id=%s", order_id)
                continue  # od razu sprawdź następny rekord
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
    z = Zello(cfg.zello_network, cfg.zello_username, cfg.zello_password, cfg.zello_channel)
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
    z = Zello(cfg.zello_network, cfg.zello_username, cfg.zello_password, cfg.zello_channel)
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
