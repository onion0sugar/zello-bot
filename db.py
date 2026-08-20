"""Warstwa MSSQL: połączenie (pyodbc) + zapytanie z osobnego pliku query.sql.

Baza jest traktowana jako TYLKO DO ODCZYTU — wykonujemy wyłącznie SELECT.
Connection string zawiera ApplicationIntent=ReadOnly (przy Always On
Availability Groups połączenie trafia do repliki do odczytu).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Order:
    """Jeden wiersz z query.sql: numer zamówienia + status + kto obsługuje."""

    order_id: int | None      # opcjonalne (kolumna Id) — tylko do logów
    number: str               # numer zamówienia (OriginalNumber)
    status: str               # znormalizowany: 'new' | 'in_progress' | inne
    modified_by: str          # login MSSQL osoby obsługującej (ModifiedBy)


def normalize_status(value) -> str:
    """Normalizuje status: 'In Progress'/'in progress' → 'in_progress'.

    Baza może zwracać obie pisownie — sprowadzamy do jednej kanonicznej.
    """
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "_")


def fetch_orders(cursor, query: str) -> list[Order]:
    """Wykonaj zapytanie z query.sql; zwróć listę zamówień.

    Kolumny rozpoznawane PO NAZWACH (kolejność w SELECT nie ma znaczenia):
      * OriginalNumber (lub OrderNumber) — numer zamówienia,
      * ModifiedBy — kto obsługuje zamówienie,
      * DocumentStatusText (lub Status) — status ('new' / 'in_progress'),
      * Id (opcjonalnie, liczba) — tylko do logów.
    Brak wymaganej kolumny = fail-fast z listą znalezionych kolumn.
    """
    try:
        cursor.execute(query)
        description = cursor.description or []
        columns = [str(col[0]).strip().lower() if col[0] else "" for col in description]
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc

    def find(*names: str) -> int | None:
        for name in names:
            for i, col in enumerate(columns):
                if col == name:
                    return i
        return None

    idx_number = find("originalnumber", "ordernumber")
    idx_status = find("documentstatustext", "status")
    idx_modified = find("modifiedby")
    idx_id = find("id")
    missing = [
        name
        for name, idx in (
            ("OriginalNumber/OrderNumber", idx_number),
            ("DocumentStatusText/Status", idx_status),
            ("ModifiedBy", idx_modified),
        )
        if idx is None
    ]
    if missing:
        raise DbError(
            f"query.sql musi zwracać kolumny: {', '.join(missing)}. "
            f"Znaleziono: {', '.join(columns) or '(brak)'}"
        )

    rows: list[Order] = []
    try:
        for row in cursor.fetchall():
            number = "" if row[idx_number] is None else str(row[idx_number]).strip()
            modified_by = (
                "" if row[idx_modified] is None else str(row[idx_modified]).strip()
            )
            order_id: int | None = None
            if idx_id is not None and row[idx_id] is not None:
                try:
                    order_id = int(row[idx_id])
                except (TypeError, ValueError):
                    order_id = None  # nie-liczbowe id — niepotrzebne do logów
            rows.append(Order(order_id, number, normalize_status(row[idx_status]), modified_by))
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc
    return rows
