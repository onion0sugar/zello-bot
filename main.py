"""Zello Bot — MSSQL → Zello (tekst + głos).

Zapytanie o zamówienia edytujesz w pliku query.sql (NIE w kodzie!).
Bot NIE zapamiętuje zamówień: każde zwrócenie wiersza przez zapytanie
= powiadomienie (nawet dla tego samego wiersza co w poprzednim pollingu).

Warianty:
    python main.py                  # serwis: polling co POLL_INTERVAL sekund
    python main.py --test-db        # SELECT 1 + walidacja query.sql
    python main.py --test-text      # test tekstu na kanał Zello
    python main.py --test-voice     # test głosu (VOICE_FILE) na kanał
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from types import SimpleNamespace

from audio import VoiceFileError, codec_header, encode_opus, wav_to_pcm
from config import ConfigError, load_config
from db import DbError, connect_db, get_next_order, load_query
from zello import Zello, ZelloError

logger = logging.getLogger("bot")

RECONNECT_DELAY = 5          # sekundy przerwy po błędzie
DEFAULT_TEXT = "🔔 Nowe zamówienie: {}"


def _build_zello(cfg: SimpleNamespace) -> Zello:
    """Tworzy klienta Zello (Work lub F&F wg konfiguracji)."""
    return Zello(
        network=cfg.zello_network or "",
        username=cfg.zello_username,
        password=cfg.zello_password,
        channel=cfg.zello_channel,
        auth_token=cfg.zello_auth_token,
        wait_online=cfg.zello_wait_online,
    )


def load_voice_packets(cfg: SimpleNamespace) -> list[bytes]:
    """WAV → PCM (FFmpeg) → ramki Opus. Raz na start — brak pliku .opus na dysku."""
    pcm = wav_to_pcm(cfg.voice_file)
    packets = encode_opus(pcm)
    logger.info("Voice ready: %d packets (%.1f s)", len(packets), len(packets) * 0.02)
    return packets


async def notify(
    z: Zello, cfg: SimpleNamespace, order_number: str, voice_packets: list[bytes]
) -> None:
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
    query = load_query()  # fail-fast: zły query.sql widać od razu przy starcie
    db = connect_db(cfg)

    voice_packets = load_voice_packets(cfg) if cfg.send_voice else []

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
                order = get_next_order(cursor, query)
            if order:
                order_id, order_number = order
                logger.info("New order id=%s", order_id)
                await notify(z, cfg, order_number, voice_packets)
            # stały rytm pollingu — także gdy zapytanie ciągle zwraca ten sam wiersz
            await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval)
        except asyncio.TimeoutError:
            pass  # przerwa pollingu — oczekiwane zachowanie, nie błąd
        except asyncio.CancelledError:
            raise
        except DbError as exc:
            logger.error("MSSQL error: %s", exc)
            try:
                db.close()
            except Exception:
                pass
            try:
                db = connect_db(cfg)
            except DbError:
                logger.error("MSSQL reconnect failed — next try in %d s", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception:
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
        query = load_query()  # walidacja pliku zapytania
    except Exception as exc:
        logger.error("DB test FAILED: %s", exc)
        return 1
    logger.info("DB test OK — SELECT 1 returned 1; query.sql loaded (%d chars)", len(query))
    return 0


async def test_text(cfg: SimpleNamespace) -> int:
    z = _build_zello(cfg)
    await z.start()
    try:
        await z.wait_logged_in()  # tylko logowanie; online kanału decyduje ZELLO_WAIT_ONLINE
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
        await z.wait_logged_in()  # tylko logowanie; online kanału decyduje ZELLO_WAIT_ONLINE
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
    parser.add_argument("--test-db", action="store_true", help="sprawdź MSSQL (SELECT 1) + query.sql")
    parser.add_argument("--test-text", action="store_true", help="wyślij test tekstowy na kanał Zello")
    parser.add_argument("--test-voice", action="store_true", help="wyślij VOICE_FILE na kanał Zello")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
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
