"""Testy logiki głównej: zapytanie, pętla serwisu (bez zapamiętywania zamówień).

Zachowanie: bot NIE pamięta last_id — każde zapytanie zwracające wiersz
powoduje powiadomienie, nawet jeśli to ten sam wiersz co wcześniej.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import main
from main import DEFAULT_TEXT, get_next_order, run_service

# --- podróbki bazy -------------------------------------------------------------


class FakeCursor:
    """Cursor odpowiadający na zapytania wg mapy: substring SQL → wiersz."""

    def __init__(self, responses: dict[str, tuple | None]):
        self.responses = responses
        self.calls: list[tuple[str, tuple | None]] = []
        self._result = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._result = None
        for key, row in self.responses.items():
            if key in sql:
                self._result = row
                return

    def fetchone(self):
        return self._result

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
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
        mssql_server="srv", mssql_port=1433, mssql_database="db",
        mssql_username="u", mssql_password="p",
        zello_network="net", zello_username="bot", zello_password="pw",
        zello_channel="Magazyn", poll_interval=3,
        send_text=True, send_voice=False, voice_file="audio/new_order.wav",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- testy zapytania -------------------------------------------------------------


def test_get_next_order_returns_row_without_parameters():
    cursor = FakeCursor({"SELECT TOP 1": (101, "ZAM/2026/1234")})
    order = get_next_order(cursor)
    assert order == (101, "ZAM/2026/1234")
    sql, params = cursor.calls[0]
    assert "SELECT TOP 1" in sql
    assert params is None  # bez parametrów — bot nie pamięta last_id


def test_get_next_order_none_when_empty():
    cursor = FakeCursor({"SELECT TOP 1": None})
    assert get_next_order(cursor) is None


def test_no_order_memory_left():
    """Mechanizm zapamiętywania (last_id / tabela stanu) został usunięty."""
    assert not hasattr(main, "get_last_id")
    assert not hasattr(main, "set_last_id")
    assert not hasattr(main, "init_state")
    assert "zello_bot_state" not in main.GET_NEXT_ORDER_SQL


# --- pętla serwisu ---------------------------------------------------------------


class ServiceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_notifies_even_when_query_keeps_returning_same_row(self):
        # zapytanie ZAWSZE zwraca ten sam wiersz — bot ma wysyłać powiadomienie
        cursor = FakeCursor({"SELECT TOP 1": (101, "ZAM/2026/1234")})
        db = FakeDB(cursor)
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello

        def fake_get_next_order(c):
            stop.set()
            return (101, "ZAM/2026/1234")

        main.get_next_order = fake_get_next_order

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert result == 0
        assert zello.texts == [("Magazyn", DEFAULT_TEXT.format("ZAM/2026/1234"))]
        assert zello.closed and db.closed

    async def test_loop_does_nothing_when_query_returns_nothing(self):
        cursor = FakeCursor({"SELECT TOP 1": None})
        db = FakeDB(cursor)
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello

        async def stop_later():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(stop_later())

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)
        assert result == 0
        assert zello.texts == []  # nic nie wysłano — zapytanie nic nie zwróciło
        assert db.closed


if __name__ == "__main__":
    unittest.main()
