"""Warstwa MSSQL: połączenie (pyodbc) + zapytanie z osobnego pliku query.sql.

Baza jest traktowana jako TYLKO DO ODCZYTU — wykonujemy wyłącznie SELECT.
Connection string zawiera ApplicationIntent=ReadOnly (przy Always On
Availability Groups połączenie trafia do repliki do odczytu).
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore[assignment]

logger = logging.getLogger("db")

QUERY_FILE = "query.sql"


class DbError(Exception):
    """Błąd połączenia, zapytania lub pliku query.sql."""


# --- zapytanie w osobnym pliku ------------------------------------------------


def load_query(path: str = QUERY_FILE) -> str:
    """Wczytaj zapytanie z pliku. Fail-fast: brak pliku / pusty / nie-SELECT.

    Komentarze (-- ...) na początku pliku są pomijane przy walidacji i zwrocie.
    """
    if not os.path.isfile(path):
        raise DbError(
            f"Brak pliku zapytania: {path}. Utwórz go wg wzoru z repozytorium."
        )
    with open(path, encoding="utf-8") as f:
        sql = f.read().strip()
    if not sql:
        raise DbError(f"Plik zapytania {path} jest pusty.")
    # odrzuć wiodące linie komentarzy, żeby znaleźć właściwą instrukcję
    lines = sql.splitlines()
    while lines and lines[0].lstrip().startswith("--"):
        lines.pop(0)
    statement = "\n".join(lines).strip()
    if not statement:
        raise DbError(f"Plik zapytania {path} zawiera tylko komentarze.")
    if not statement.lstrip().upper().startswith("SELECT"):
        raise DbError(f"Plik zapytania {path} musi zaczynać się od SELECT.")
    return statement


# --- połączenie ----------------------------------------------------------------


def build_dsn(cfg: SimpleNamespace) -> str:
    """Connection string ODBC Driver 18.

    Instancja nazwana (host\\instancja) NIE dostaje portu w adresie — numer
    portu odczytuje SQL Browser (UDP 1434). Port dopisujemy tylko zwykłemu
    adresowi.
    """
    server = cfg.mssql_server
    if "\\" not in server:
        server = f"{server},{cfg.mssql_port}"
    return ";".join(
        [
            "DRIVER={ODBC Driver 18 for SQL Server}",
            f"SERVER={server}",
            f"DATABASE={cfg.mssql_database}",
            f"UID={cfg.mssql_username}",
            f"PWD={cfg.mssql_password}",
            f"Encrypt={cfg.mssql_encrypt}",
            f"TrustServerCertificate={cfg.mssql_trust_server_certificate}",
            "ApplicationIntent=ReadOnly",
        ]
    )


def connect_db(cfg: SimpleNamespace):
    """Połączenie pyodbc (autocommit — każda instrukcja to własna transakcja)."""
    if pyodbc is None:
        raise DbError("pyodbc nie jest zainstalowane — pip install -r requirements.txt")
    try:
        cnxn = pyodbc.connect(build_dsn(cfg), timeout=10, autocommit=True)
    except Exception as exc:
        raise DbError(f"MSSQL connection failed: {exc}") from exc
    cnxn.timeout = 15
    logger.info("Connected to MSSQL (%s)", cfg.mssql_server)
    return cnxn


# --- zapytanie ------------------------------------------------------------------


def get_next_order(cursor, query: str) -> tuple[int | None, str] | None:
    """Wykonaj zapytanie z query.sql. Wiersz → powiadomienie (bez pamiętania).

    Obsługuje zapytania zwracające:
    * 1 kolumnę — sam numer zamówienia (np. OriginalNumber),
    * 2 kolumny — id (liczba, tylko do logów) + numer zamówienia.

    Zwraca (order_id, order_number); order_id = None, gdy brak kolumny id
    lub nie jest liczbą.
    """
    try:
        cursor.execute(query)
        row = cursor.fetchone()
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc
    if row is None:
        return None
    if len(row) >= 2:
        order_number = "" if row[1] is None else str(row[1])
        try:
            order_id: int | None = int(row[0])
        except (TypeError, ValueError):
            order_id = None  # nie-liczbowe id — niepotrzebne do wysyłki
        return order_id, order_number
    # jedna kolumna — sam numer zamówienia
    order_number = "" if row[0] is None else str(row[0])
    return None, order_number
