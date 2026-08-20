"""Testy warstwy bazy: DSN (instancja nazwana), query.sql, zapytania, połączenie.

pyodbc jest zamockowane — bez prawdziwego MSSQL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import db
from config import load_config
from db import DbError, Order, build_dsn, fetch_orders, load_query


class FakeCursor:
    """Podróbka kursora: opis kolumn + wiersze (jak prawdziwy pyodbc)."""

    def __init__(self, rows, description):
        self.rows = rows
        self.description = description
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor = cursor
        self.timeout = None
        self.closed = False

    def cursor(self):
        return self.cursor

    def close(self):
        self.closed = True


class FakePyodbc:
    def __init__(self, fail=False):
        self.fail = fail
        self.last_dsn = None

    def connect(self, dsn, timeout=None, autocommit=False):
        self.last_dsn = dsn
        if self.fail:
            raise Exception("08001 can't connect")  # symulacja pyodbc.Error
        return FakeConnection(FakeCursor([], []))


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
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- DSN -----------------------------------------------------------------------


def test_dsn_named_instance_without_port():
    dsn = build_dsn(make_cfg(mssql_server="192.168.24.22\\SERWISKOPB2B"))
    assert "SERVER=192.168.24.22\\SERWISKOPB2B;" in dsn
    assert ",1433" not in dsn  # instancja nazwana: port ustala SQL Browser
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in dsn
    assert "DATABASE=SerwisKop_Magazyn" in dsn
    assert "UID=serwiskop-ro" in dsn
    assert "ApplicationIntent=ReadOnly" in dsn


def test_dsn_plain_server_gets_port():
    dsn = build_dsn(make_cfg(mssql_server="10.0.0.1"))
    assert "SERVER=10.0.0.1,1433;" in dsn


def test_dsn_custom_port():
    dsn = build_dsn(make_cfg(mssql_server="10.0.0.1", mssql_port=52341))
    assert "SERVER=10.0.0.1,52341;" in dsn


# --- query.sql ------------------------------------------------------------------


def test_query_file_ships_valid_select():
    """Wzorcowy query.sql z repozytorium ładuje się pod nowy kontrakt."""
    sql = load_query()  # realny plik w projekcie
    assert sql.lstrip().upper().startswith("SELECT")
    assert "OriginalNumber" in sql
    assert "ModifiedBy" in sql
    assert "DocumentStatusText" in sql


def test_load_query_missing_file_raises(tmp_path):
    with pytest.raises(DbError, match="Brak pliku zapytania"):
        load_query(str(tmp_path / "brak.sql"))


def test_load_query_empty_file_raises(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(DbError, match="pusty"):
        load_query(str(path))


def test_load_query_non_select_raises(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("DELETE FROM dbo.orders", encoding="utf-8")
    with pytest.raises(DbError, match="SELECT"):
        load_query(str(path))


def test_load_query_reads_file(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("SELECT 1", encoding="utf-8")
    assert load_query(str(path)) == "SELECT 1"


# --- fetch_orders -----------------------------------------------------------------

DESC_FULL = [
    ("id",), ("OriginalNumber",), ("DocumentStatusText",), ("ModifiedBy",),
]


def test_fetch_orders_parses_by_column_name():
    cursor = FakeCursor(
        [(1, "ZAM/2026/1234", "new", "jan.kowalski")], DESC_FULL,
    )
    orders = fetch_orders(cursor, "SELECT ...")
    assert orders == [Order(1, "ZAM/2026/1234", "new", "jan.kowalski")]
    assert cursor.executed[0][0] == "SELECT ..."


def test_fetch_orders_column_order_is_irrelevant():
    desc = [("ModifiedBy",), ("OriginalNumber",), ("DocumentStatusText",)]
    cursor = FakeCursor([("anna.nowak", "ZAM/2", "in progress")], desc)
    orders = fetch_orders(cursor, "SELECT 1")
    assert orders == [Order(None, "ZAM/2", "in_progress", "anna.nowak")]


def test_fetch_orders_normalizes_status_spelling():
    desc = [("OriginalNumber",), ("Status",), ("ModifiedBy",)]
    cursor = FakeCursor(
        [("Z1", "in_progress", "a"), ("Z2", "In Progress", "b"), ("Z3", "NEW", "c")],
        desc,
    )
    orders = fetch_orders(cursor, "SELECT 1")
    assert [o.status for o in orders] == ["in_progress", "in_progress", "new"]


def test_fetch_orders_accepts_status_alias():
    desc = [("OriginalNumber",), ("Status",), ("ModifiedBy",)]
    cursor = FakeCursor([("Z1", "new", "a")], desc)
    assert fetch_orders(cursor, "SELECT 1") == [Order(None, "Z1", "new", "a")]


def test_fetch_orders_missing_required_column_raises():
    desc = [("OriginalNumber",), ("DocumentStatusText",)]  # brak ModifiedBy
    cursor = FakeCursor([("Z1", "new")], desc)
    with pytest.raises(DbError, match="ModifiedBy"):
        fetch_orders(cursor, "SELECT 1")


def test_fetch_orders_no_rows_returns_empty_list():
    cursor = FakeCursor([], DESC_FULL)
    assert fetch_orders(cursor, "SELECT 1") == []


def test_fetch_orders_null_values_become_empty_strings():
    cursor = FakeCursor([(None, None, None, None)], DESC_FULL)
    orders = fetch_orders(cursor, "SELECT 1")
    assert orders == [Order(None, "", "", "")]


def test_fetch_orders_optional_id_non_numeric_tolerated():
    desc = [("id",), ("OriginalNumber",), ("DocumentStatusText",), ("ModifiedBy",)]
    cursor = FakeCursor([("ABC", "Z1", "new", "a")], desc)  # id nie jest liczbą
    assert fetch_orders(cursor, "SELECT 1") == [Order(None, "Z1", "new", "a")]


def test_fetch_orders_wraps_db_errors():
    class BoomCursor:
        def execute(self, sql, params=None):
            raise RuntimeError("table not found")

    with pytest.raises(DbError, match="Query failed"):
        fetch_orders(BoomCursor(), "SELECT 1")


# --- connect_db -------------------------------------------------------------------


def test_connect_db_uses_dsn_and_autocommit(monkeypatch):
    fake = FakePyodbc()
    monkeypatch.setattr(db, "pyodbc", fake)
    cnxn = db.connect_db(make_cfg())
    assert fake.last_dsn and "SERVER=192.168.24.22\\SERWISKOPB2B;" in fake.last_dsn
    assert cnxn.timeout == 15


def test_connect_db_failure_raises_db_error(monkeypatch):
    fake = FakePyodbc(fail=True)
    monkeypatch.setattr(db, "pyodbc", fake)
    with pytest.raises(DbError, match="connection failed"):
        db.connect_db(make_cfg())
