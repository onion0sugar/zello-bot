"""Testy pętli serwisu (baza i Zello zamockowane).

Zachowanie: bot NIE pamięta zamówień — każde zwrócenie wiersza przez
zapytanie z query.sql powoduje powiadomienie, nawet dla tego samego wiersza.
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

# --- podróbki ------------------------------------------------------------------


class FakeCursor:
    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    def __init__(self):
        self._cursor = FakeCursor()
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class FakeZello:
    def __init__(self):
        self.texts: list[tuple[str, str]] = []
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def send_text_message(self, channel, text):
        self.texts.append((channel, text))

    async def send_voice(self, channel, packets, codec_header):
        pass


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
        announce_interval=30,
        send_text=True,
        send_voice=False,
        voice_file="audio/new_order.wav",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch(monkeypatch, db: FakeDB, zello: FakeZello) -> None:
    monkeypatch.setattr(main, "connect_db", lambda cfg: db)
    monkeypatch.setattr(main, "Zello", lambda *a, **kw: zello)


# --- pętla ---------------------------------------------------------------------


class ServiceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_notifies_even_when_query_keeps_returning_same_row(self):
        db = FakeDB()
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello

        def fake_get_next_order(cursor, query):
            stop.set()  # jedna iteracja wystarczy
            return (101, "ZAM/2026/1234")

        main.get_next_order = fake_get_next_order

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert result == 0
        assert zello.texts == [("Magazyn", DEFAULT_TEXT.format("ZAM/2026/1234"))]
        assert zello.closed and db.closed

    async def test_loop_does_nothing_when_query_returns_nothing(self):
        db = FakeDB()
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello
        main.get_next_order = lambda cursor, query: None

        async def stop_later():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(stop_later())

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert result == 0
        assert zello.texts == []  # nic nie wysłano — zapytanie nic nie zwróciło
        assert db.closed

    async def test_loop_recovers_when_db_unavailable_at_start(self):
        """Baza chwilowo nieosiągalna → serwis nie pada, tylko ponawia."""
        attempts = {"n": 0}
        stop = asyncio.Event()

        def flaky_connect(cfg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise DbError("MSSQL connection failed: timeout")
            stop.set()  # druga próba udana → kończymy pętlę
            return FakeDB()

        with patch.object(main, "RECONNECT_DELAY", 0.02):
            main.connect_db = flaky_connect
            main.Zello = lambda *a, **kw: FakeZello()
            main.get_next_order = lambda cursor, query: None
            result = await run_service(make_cfg(poll_interval=0.02), stop=stop)

        assert result == 0
        assert attempts["n"] == 2  # pierwsza padła, druga się udała
        assert stop.is_set()

    async def test_loop_repeats_announcement_until_no_order(self):
        """Powiadomienie powtarza się co announce_interval, dopóki jest wiersz."""
        db = FakeDB()
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello
        calls = {"n": 0}

        def fake_get_next_order(cursor, query):
            calls["n"] += 1
            if calls["n"] >= 3:
                stop.set()
                return None  # kolejne zapytanie: brak zamówienia → koniec powtórek
            return (101, "ZAM/2026/1234")

        main.get_next_order = fake_get_next_order

        result = await run_service(
            make_cfg(poll_interval=0.02, announce_interval=0.01), stop=stop
        )
        assert result == 0
        assert len(zello.texts) == 2  # 2 wiersze → 2 powiadomienia, 3. zapytanie puste

    async def test_loop_respects_announce_interval(self):
        """Interwał 60 s >> czas testu → tylko 1 powiadomienie mimo ciągłych wierszy."""
        db = FakeDB()
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello
        calls = {"n": 0}

        def fake_get_next_order(cursor, query):
            calls["n"] += 1
            if calls["n"] >= 4:
                stop.set()
            return (101, "ZAM/2026/1234")

        main.get_next_order = fake_get_next_order

        result = await run_service(
            make_cfg(poll_interval=0.02, announce_interval=60), stop=stop
        )
        assert result == 0
        assert len(zello.texts) == 1  # pierwsze powiadomienie; reszta wstrzymana interwałem

    async def test_loop_resets_cycle_after_no_order(self):
        """Brak wiersza resetuje cykl — następne zamówienie anonsowane od razu."""
        db = FakeDB()
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello
        sequence = [(101, "ZAM/1"), None, (102, "ZAM/2")]
        idx = {"i": 0}

        def fake_get_next_order(cursor, query):
            if idx["i"] >= len(sequence):
                stop.set()
                return None
            value = sequence[idx["i"]]
            idx["i"] += 1
            return value

        main.get_next_order = fake_get_next_order

        result = await run_service(
            make_cfg(poll_interval=0.02, announce_interval=60), stop=stop
        )
        assert result == 0
        # 1. poll: ZAM/1 → powiadomienie; 2. poll: brak → reset; 3. poll: ZAM/2 → od razu
        assert len(zello.texts) == 2

    async def test_missing_query_file_fails_fast(self):
        with patch("main.load_query", side_effect=DbError("Brak pliku zapytania: query.sql")):
            with pytest.raises(DbError, match="query.sql"):
                await run_service(make_cfg())

    async def test_no_order_memory_left(self):
        """Mechanizm zapamiętywania (last_id / tabela stanu) nie istnieje."""
        assert not hasattr(main, "get_last_id")
        assert not hasattr(main, "set_last_id")
        with open("query.sql", encoding="utf-8") as f:
            assert "zello_bot_state" not in f.read()


if __name__ == "__main__":
    unittest.main()
