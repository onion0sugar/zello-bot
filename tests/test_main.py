"""Testy pętli serwisu (baza i Zello zamockowane).

Nowe zachowanie: powiadomienie PER UŻYTKOWNIK (atrybut "for") — odbiorcy =
wszyscy z user_mapping.json MINUS zajęci (zamówienie 'in_progress' wg
kolumny ModifiedBy z query.sql).
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from db import DbError
from main import DEFAULT_TEXT, run_service
from users import MappingError

# --- podróbki ------------------------------------------------------------------

DESC = [("id",), ("OriginalNumber",), ("DocumentStatusText",), ("ModifiedBy",)]


def rows(*entries):
    """entries: (order_id|None, number, status, modified_by) → wiersze SQL."""
    return [list(e) for e in entries]


class FakeCursor:
    def __init__(self, data, description):
        self.data = data
        self.description = description

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, data, description=DESC):
        self._cursor = FakeCursor(data, description)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class FakeZello:
    def __init__(self):
        self.texts: list[tuple[str, str, str | None]] = []  # (channel, text, for_user)
        self.voices: list[tuple[str, str | None]] = []      # (channel, for_user)
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def send_text_message(self, channel, text, for_user=None):
        self.texts.append((channel, text, for_user))

    async def send_voice(self, channel, packets, codec_header, for_user=None):
        self.voices.append((channel, for_user))


def make_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        mssql_server="192.168.24.22\\SERWISKOPB2B",
        mssql_port=1433,
        mssql_database="SerwisKop_Magazyn",
        mssql_username="serwiskop-ro",
        mssql_password="haslo",
        mssql_encrypt="yes",
        mssql_trust_server_certificate="yes",
        zello_network="net",
        zello_username="bot",
        zello_password="pw",
        zello_channel="Magazyn",
        zello_auth_token="",
        zello_wait_online=True,
        poll_interval=3,
        send_text=True,
        send_voice=False,
        voice_file="audio/new_order.wav",
        user_mapping_file="user_mapping.json",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


MAPPING = {"jan.kowalski": "jan", "anna.nowak": "anna"}


def _install(db: FakeDB, zello: FakeZello, mapping: dict | None = None) -> None:
    """Podstaw podróbki w module main (tak jak robiły to pierwotne testy)."""
    main.connect_db = lambda cfg: db
    main.Zello = lambda *a, **kw: zello
    main.load_mapping = lambda path: mapping if mapping is not None else MAPPING


class ServiceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_notifies_all_except_busy(self):
        db = FakeDB(rows(
            (None, "ZAM/1", "new", ""),
            (None, "ZAM/2", "in_progress", "anna.nowak"),
        ))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()  # jedna iteracja wystarczy
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert result == 0
        # anna zajęta (ZAM/2) → tylko jan dostaje wiadomość, z atrybutem for
        assert zello.texts == [("Magazyn", DEFAULT_TEXT.format("ZAM/1"), "jan")]
        assert zello.closed and db.closed

    async def test_loop_sends_to_all_when_nobody_busy(self):
        db = FakeDB(rows((None, "ZAM/1", "new", "jan.kowalski")))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert zello.texts == [
            ("Magazyn", DEFAULT_TEXT.format("ZAM/1"), "jan"),
            ("Magazyn", DEFAULT_TEXT.format("ZAM/1"), "anna"),
        ]

    async def test_loop_no_message_when_only_in_progress(self):
        db = FakeDB(rows((None, "ZAM/2", "in_progress", "anna.nowak")))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert zello.texts == []  # brak 'new' → brak powiadomienia

    async def test_loop_no_message_when_everyone_busy(self):
        db = FakeDB(rows(
            (None, "ZAM/1", "new", ""),
            (None, "ZAM/2", "in_progress", "jan.kowalski"),
            (None, "ZAM/3", "in_progress", "anna.nowak"),
        ))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert zello.texts == []  # nowe zamówienie jest, ale wszyscy zajęci

    async def test_loop_multiple_new_orders_plural_text(self):
        db = FakeDB(rows(
            (None, "ZAM/1", "new", ""),
            (None, "ZAM/2", "new", ""),
        ))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        await run_service(make_cfg(poll_interval=0.02), stop=stop)
        expected = ("Magazyn", "🔔 Nowe zamówienia: ZAM/1, ZAM/2", "jan")
        assert expected in zello.texts

    async def test_loop_sends_voice_per_recipient(self):
        db = FakeDB(rows((None, "ZAM/1", "new", "")))
        zello = FakeZello()
        stop = asyncio.Event()
        _install(db, zello)
        main.load_voice_packets = lambda cfg: [b"p1"]
        main.codec_header = lambda: "hdr"
        real_fetch = main.fetch_orders

        def fetch_once(cursor, query):
            stop.set()
            return real_fetch(cursor, query)

        main.fetch_orders = fetch_once

        await run_service(make_cfg(poll_interval=0.02, send_voice=True), stop=stop)
        assert zello.voices == [("Magazyn", "jan"), ("Magazyn", "anna")]

    async def test_loop_recovers_when_db_unavailable_at_start(self):
        """Baza chwilowo nieosiągalna → serwis nie pada, tylko ponawia."""
        attempts = {"n": 0}
        stop = asyncio.Event()

        def flaky_connect(cfg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise DbError("MSSQL connection failed: timeout")
            stop.set()  # druga próba udana → kończymy pętlę
            return FakeDB(rows())

        with patch.object(main, "RECONNECT_DELAY", 0.02):
            main.connect_db = flaky_connect
            main.Zello = lambda *a, **kw: FakeZello()
            main.load_mapping = lambda path: MAPPING
            result = await run_service(make_cfg(poll_interval=0.02), stop=stop)

        assert result == 0
        assert attempts["n"] == 2  # pierwsza padła, druga się udała
        assert stop.is_set()

    async def test_missing_query_file_fails_fast(self):
        with patch("main.load_query", side_effect=DbError("Brak pliku zapytania: query.sql")):
            with pytest.raises(DbError, match="query.sql"):
                await run_service(make_cfg())

    async def test_missing_mapping_file_fails_fast(self):
        with patch(
            "main.load_mapping",
            side_effect=MappingError("Brak pliku mapowania: user_mapping.json"),
        ):
            with pytest.raises(MappingError, match="user_mapping.json"):
                await run_service(make_cfg())

    async def test_no_order_memory_left(self):
        """Mechanizm zapamiętywania (last_id / tabela stanu) nie istnieje."""
        assert not hasattr(main, "get_last_id")
        assert not hasattr(main, "set_last_id")
        with open("query.sql", encoding="utf-8") as f:
            assert "zello_bot_state" not in f.read()


if __name__ == "__main__":
    unittest.main()
