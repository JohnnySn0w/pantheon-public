"""Tests for tools/appserver_seats.py (T-6118 PR-2): the seat-name lookup shared by the installer and the export."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import appserver_seats as seats

REPO = Path(__file__).resolve().parent.parent


def _write(root: Path, doc) -> Path:
    path = root / seats.APPSERVER_SEATS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return root


def test_the_real_file_maps_the_codex_account_to_consultant():
    assert seats.appserver_seat_for_account(REPO, "codex") == "consultant"
    assert seats.appserver_seat_for_account(REPO, "codex-fallback") == "watchman"


def test_zero_matches_is_a_count_error_with_no_matches(tmp_path):
    _write(tmp_path, {"seats": {"a": {"account": "x"}}})
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "nobody")
    assert err.value.kind == "count" and err.value.matches == ()
    assert str(err.value) == f"{seats.APPSERVER_SEATS}: expected exactly one seat for account 'nobody', found []"


def test_two_matches_is_ambiguous_and_lists_them_sorted(tmp_path):
    _write(tmp_path, {"seats": {"b": {"account": "x"}, "a": {"account": "x"}}})
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "x")
    assert err.value.kind == "count" and err.value.matches == ("a", "b")


def test_unreadable_and_malformed_files_have_their_own_kinds(tmp_path):
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "x")
    assert err.value.kind == "unreadable" and str(err.value).startswith(f"cannot read {seats.APPSERVER_SEATS}: ")
    _write(tmp_path, "{not json")
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "x")
    assert err.value.kind == "unreadable"
    _write(tmp_path, {"seats": ["a"]})
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "x")
    assert err.value.kind == "malformed"
    assert str(err.value) == f"{seats.APPSERVER_SEATS}: seats must be a mapping, got list"


def test_an_invalid_seat_name_is_refused(tmp_path):
    _write(tmp_path, {"seats": {"Bad Name": {"account": "x"}}})
    with pytest.raises(seats.AppserverSeatsError) as err:
        seats.appserver_seat_for_account(tmp_path, "x")
    assert err.value.kind == "count"


def test_seat_name_re_shape():
    assert seats.SEAT_NAME_RE.match("consultant") and seats.SEAT_NAME_RE.match("a_b-9")
    assert not seats.SEAT_NAME_RE.match("../x") and not seats.SEAT_NAME_RE.match("")
    assert not seats.SEAT_NAME_RE.match("a" * 65)


def test_the_installer_delegates_to_this_module_with_one_definition():
    from tools import persona_installer as pim

    assert pim._SEAT_TIMER_INSTANCE is seats.SEAT_NAME_RE
    assert pim._APPSERVER_SEATS == seats.APPSERVER_SEATS
