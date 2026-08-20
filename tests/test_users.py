"""Testy mapowania użytkowników: user_mapping.json, zajęci, odbiorcy."""

from __future__ import annotations

import logging

import pytest

from db import Order
from users import MappingError, busy_zello_users, load_mapping, recipients


def write_mapping(path, data):
    import json

    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def order(number, status, modified_by="", order_id=None):
    return Order(order_id, number, status, modified_by)


# --- load_mapping ----------------------------------------------------------------


def test_load_mapping_valid(tmp_path):
    path = write_mapping(tmp_path / "m.json", {"jan.kowalski": "jan", "anna.nowak": "anna"})
    assert load_mapping(str(path)) == {"jan.kowalski": "jan", "anna.nowak": "anna"}


def test_load_mapping_trims_whitespace(tmp_path):
    path = write_mapping(tmp_path / "m.json", {" jan.kowalski ": " jan "})
    assert load_mapping(str(path)) == {"jan.kowalski": "jan"}


def test_load_mapping_missing_file_raises(tmp_path):
    with pytest.raises(MappingError, match="Brak pliku mapowania"):
        load_mapping(str(tmp_path / "brak.json"))


def test_load_mapping_bad_json_raises(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MappingError, match="błędny JSON"):
        load_mapping(str(path))


def test_load_mapping_non_object_raises(tmp_path):
    path = write_mapping(tmp_path / "m.json", ["jan", "anna"])
    with pytest.raises(MappingError, match="obiektem JSON"):
        load_mapping(str(path))


def test_load_mapping_skips_invalid_values(tmp_path, caplog):
    path = write_mapping(tmp_path / "m.json", {"login": "ok", "zly": 42, "pusty": ""})
    with caplog.at_level(logging.WARNING):
        mapping = load_mapping(str(path))
    assert mapping == {"login": "ok"}
    assert "Pomijam" in caplog.text


# --- busy_zello_users ------------------------------------------------------------


def test_busy_contains_only_mapped_in_progress_handlers():
    mapping = {"jan.kowalski": "jan", "anna.nowak": "anna"}
    orders = [
        order("Z1", "new", "jan.kowalski"),        # nowe — nie zajmuje nikogo
        order("Z2", "in_progress", "jan.kowalski"),  # zajęty: jan
        order("Z3", "in_progress", "anna.nowak"),    # zajęta: anna
    ]
    assert busy_zello_users(orders, mapping) == {"jan", "anna"}


def test_busy_empty_when_no_in_progress():
    orders = [order("Z1", "new", "jan.kowalski")]
    assert busy_zello_users(orders, mapping={"jan.kowalski": "jan"}) == set()


def test_busy_skips_unmapped_handler_with_warning(caplog):
    orders = [order("Z1", "in_progress", "nieznany.login")]
    with caplog.at_level(logging.WARNING):
        busy = busy_zello_users(orders, mapping={"jan.kowalski": "jan"})
    assert busy == set()  # nie może wykluczyć — nie zna nazwy Zello
    assert "nieznany.login" in caplog.text


def test_busy_skips_in_progress_without_modified_by():
    orders = [order("Z1", "in_progress", "")]
    assert busy_zello_users(orders, mapping={}) == set()


# --- recipients ------------------------------------------------------------------


def test_recipients_all_minus_busy():
    mapping = {"jan.kowalski": "jan", "anna.nowak": "anna", "piotr.w": "piotr"}
    orders = [order("Z1", "new"), order("Z2", "in_progress", "anna.nowak")]
    assert recipients(mapping, orders) == ["jan", "piotr"]


def test_recipients_all_when_nobody_busy():
    mapping = {"jan.kowalski": "jan", "anna.nowak": "anna"}
    orders = [order("Z1", "new", "jan.kowalski")]
    assert recipients(mapping, orders) == ["jan", "anna"]


def test_recipients_nobody_when_everyone_busy():
    mapping = {"jan.kowalski": "jan", "anna.nowak": "anna"}
    orders = [
        order("Z1", "in_progress", "jan.kowalski"),
        order("Z2", "in_progress", "anna.nowak"),
    ]
    assert recipients(mapping, orders) == []


def test_recipients_empty_mapping():
    assert recipients({}, [order("Z1", "new")]) == []


def test_recipients_dedups_multiple_logins_to_one_zello_user():
    mapping = {"jan.kowalski": "jan", "jan.kowalski2": "jan"}  # 2 loginy → 1 osoba
    orders = [order("Z1", "new")]
    assert recipients(mapping, orders) == ["jan"]
