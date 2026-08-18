"""Testy logiki głównej: zapytania, checkpoint last_id, pętla serwisu.

pyodbc / Zello / audio są zamockowane — żadnego prawdziwego MSSQL ani Zello.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import main
from main import (
    DEFAULT_TEXT,
    get_last_id,
    get_next_order,
    init_state,
    run_service,
    set_last_id,
)

# --- podróbki bazy -------------------------------------------------------------


class FakeCursor:
    """Cursor odpowiadający na zapytania wg mapy: substring SQL → wiersz."""

    def __init__(self, responses: dict[str, tuple | None], rowcount: int = 1):
        self.responses = responses
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple | None]] = []
        self._result = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._result = None
        for key, row in self.responses.items():
            if key in sql:
                self._result = row
                return
        self._result = None

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
        self.voices: list[tuple[str, int]] = []
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def send_text_message(self, channel, text):
        self.texts.append((channel, text))

    async def send_voice(self, channel, packets, codec_header):
        self.voices.append((channel, len(packets)))


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


# --- testy zapytań --------------------------------------------------------------


def test_get_next_order_uses_parameterized_query():
    cursor = FakeCursor({"SELECT TOP 1": (101, "ZAM/2026/1234")})
    order = get_next_order(cursor, 100)
    assert order == (101, "ZAM/2026/1234")
    sql, params = cursor.calls[0]
    assert "WHERE id > ?" in sql
    assert params == (100,)


def test_get_next_order_none_when_empty():
    cursor = FakeCursor({"SELECT TOP 1": None})
    assert get_next_order(cursor, 100) is None


def test_set_last_id_updates_existing_row():
    cursor = FakeCursor({"UPDATE dbo.zello_bot_state": None}, rowcount=1)
    set_last_id(cursor, 101)
    assert len(cursor.calls) == 1
    assert cursor.calls[0][0].startswith("UPDATE")


def test_set_last_id_inserts_when_missing_row():
    cursor = FakeCursor({"UPDATE dbo.zello_bot_state": None}, rowcount=0)
    set_last_id(cursor, 101)
    assert len(cursor.calls) == 2
    assert cursor.calls[1][0].startswith("INSERT")
    assert cursor.calls[1][1] == ("orders", 101)


def test_init_state_skips_when_row_exists():
    cursor = FakeCursor({"SELECT last_id": (50,)})
    init_state(cursor)
    assert len(cursor.calls) == 1  # bez MAX(id), bez INSERT/UPDATE


def test_init_state_first_run_uses_max_id():
    # UPDATE bez dopasowania → rowcount=0 → następuje INSERT
    cursor = FakeCursor({"SELECT last_id": None, "ISNULL(MAX(id)": (42,)}, rowcount=0)
    init_state(cursor)
    sqls = [sql for sql, _ in cursor.calls]
    assert any("ISNULL(MAX(id), 0)" in sql for sql in sqls)
    assert any(sql.startswith("INSERT") for sql in sqls)


# --- pętla serwisu ---------------------------------------------------------------


class ServiceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_sends_notification_and_updates_last_id(self):
        cursor = FakeCursor(
            {
                "SELECT last_id": (100,),
                "SELECT TOP 1": (101, "ZAM/2026/1234"),
                "UPDATE dbo.zello_bot_state": None,
            }
        )
        db = FakeDB(cursor)
        zello = FakeZello()
        stop = asyncio.Event()
        main.connect_db = lambda cfg: db
        main.Zello = lambda *a, **kw: zello
        updated = []

        def fake_set_last_id(c, value):
            updated.append(value)
            stop.set()

        main.set_last_id = fake_set_last_id

        result = await run_service(make_cfg(poll_interval=0.02), stop=stop)

        assert result == 0
        assert zello.texts == [("Magazyn", DEFAULT_TEXT.format("ZAM/2026/1234"))]
        assert updated == [101]
        assert zello.closed and db.closed

    async def test_loop_sleeps_when_no_order_then_exits_on_stop(self):
        cursor = FakeCursor({"SELECT last_id": (100,), "SELECT TOP 1": None})
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
        assert zello.texts == []  # nic nie wysłano — brak nowych rekordów
        assert db.closed


if __name__ == "__main__":
    unittest.main()
