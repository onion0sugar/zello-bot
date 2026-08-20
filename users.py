"""Mapowanie loginów MSSQL → nazw Zello i wyliczanie odbiorców powiadomień.

Plik konfiguracyjny: user_mapping.json (JSON: {"login_mssql": "zello_user"}).

Wartości mapowania = pełna lista użytkowników, którzy mogą dostawać
powiadomienia. Odbiorcy = wszyscy zmapowani MINUS zajęci (mają zamówienie
ze statusem 'in_progress' wg kolumny ModifiedBy z query.sql).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("users")

MAPPING_FILE = "user_mapping.json"


class MappingError(Exception):
    """Brak pliku mapowania lub błędny JSON."""


def load_mapping(path: str = MAPPING_FILE) -> dict[str, str]:
    """Wczytaj mapowanie login MSSQL → nazwa Zello. Fail-fast przy braku pliku."""
    if not os.path.isfile(path):
        raise MappingError(
            f"Brak pliku mapowania: {path}. Utwórz go wg wzoru user_mapping.example.json"
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise MappingError(f"Plik mapowania {path} ma błędny JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError(f"Plik mapowania {path} musi być obiektem JSON ({{...}}).")
    mapping: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            logger.warning("Pomijam wpis %r — wartość musi być nazwą Zello", key)
            continue
        mapping[str(key).strip()] = value.strip()
    return mapping


def busy_zello_users(orders, mapping: dict[str, str]) -> set[str]:
    """Nazwy Zello użytkowników, którzy mają zamówienie ze statusem 'in_progress'.

    Niezmapowany ModifiedBy → ostrzeżenie; takiego użytkownika NIE wykluczamy
    (ostrożnie: może dostać powiadomienie mimo bycia zajętym).
    """
    busy: set[str] = set()
    for order in orders:
        if order.status != "in_progress":
            continue
        if not order.modified_by:
            continue  # zamówienie bez opiekuna — nikogo nie wyklucza
        zello_user = mapping.get(order.modified_by)
        if zello_user is None:
            logger.warning(
                "ModifiedBy=%r nie ma wpisu w user_mapping.json — nie mogę go wykluczyć",
                order.modified_by,
            )
        else:
            busy.add(zello_user)
    return busy


def recipients(mapping: dict[str, str], orders) -> list[str]:
    """Odbiorcy powiadomienia: wszyscy zmapowani MINUS zajęci.

    Kolejność = kolejność wpisów w user_mapping.json (deterministyczna);
    duplikaty (kilka loginów MSSQL → ta sama nazwa Zello) zwijane do jednej osoby.
    """
    busy = busy_zello_users(orders, mapping)
    return list(dict.fromkeys(user for user in mapping.values() if user not in busy))
