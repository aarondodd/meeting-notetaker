"""Session-list sort spec encoder/decoder + persistence wiring.

These tests cover the pure-Python translation between the persisted
spec strings (`date_desc` / `date_asc` / `title_asc` / `title_desc`)
and the Qt (column index, sort order) pair MainWindow drives, plus the
config validator's accept/reject behavior for the new
`ui.session_list_sort` field.

The actual header-click round-trip needs a real MainWindow + populated
session list, which pulls in the entire app graph. The translation
helpers verified here are the load-bearing piece for both the
persistence write side (header click -> spec -> config.save) and the
restore-on-launch read side (config.load -> spec -> sortByColumn).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402

from meeting_notetaker.ui.main_window import (  # noqa: E402
    _COL_DATE,
    _COL_TITLE,
    _COL_AUDIO,
    _COL_SLIDES,
    _COL_STATE,
    _DEFAULT_SORT_SPEC,
    _column_order_to_sort_spec,
    _sort_spec_to_column_order,
)
from meeting_notetaker.utils.config import (  # noqa: E402
    Config,
    VALID_SESSION_LIST_SORTS,
)


def test_default_sort_spec_is_date_desc():
    """Newest-first remains the default; bumping this is a UX choice that
    deserves an explicit test edit."""
    assert _DEFAULT_SORT_SPEC == "date_desc"


def test_column_order_matches_visible_layout():
    """Issue #25 specifies Date | Title | Audio | Slides | State. A change
    here would regress the visual layout the user requested."""
    assert _COL_DATE == 0
    assert _COL_TITLE == 1
    assert _COL_AUDIO == 2
    assert _COL_SLIDES == 3
    assert _COL_STATE == 4


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("date_desc",  (_COL_DATE,  Qt.SortOrder.DescendingOrder)),
        ("date_asc",   (_COL_DATE,  Qt.SortOrder.AscendingOrder)),
        ("title_asc",  (_COL_TITLE, Qt.SortOrder.AscendingOrder)),
        ("title_desc", (_COL_TITLE, Qt.SortOrder.DescendingOrder)),
    ],
)
def test_sort_spec_to_column_order_known_specs(spec, expected):
    assert _sort_spec_to_column_order(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["", "bogus", "audio_asc", "date_descx"],
)
def test_sort_spec_to_column_order_unknown_falls_back_to_default(spec):
    """A stale config.toml from a prior version (or a hand-edited mistake)
    must not crash the app -- it falls back to date_desc."""
    assert _sort_spec_to_column_order(spec) == _sort_spec_to_column_order(
        _DEFAULT_SORT_SPEC,
    )


@pytest.mark.parametrize(
    "column, order, expected",
    [
        (_COL_DATE,  Qt.SortOrder.DescendingOrder, "date_desc"),
        (_COL_DATE,  Qt.SortOrder.AscendingOrder,  "date_asc"),
        (_COL_TITLE, Qt.SortOrder.AscendingOrder,  "title_asc"),
        (_COL_TITLE, Qt.SortOrder.DescendingOrder, "title_desc"),
    ],
)
def test_column_order_to_sort_spec_known_columns(column, order, expected):
    assert _column_order_to_sort_spec(column, order) == expected


@pytest.mark.parametrize("indicator_column", [_COL_AUDIO, _COL_SLIDES, _COL_STATE])
def test_column_order_to_sort_spec_indicator_columns_return_default(
    indicator_column,
):
    """Indicator columns aren't sortable -- the encoder hands back the
    default so a snap-back path has a sensible value to drive."""
    assert _column_order_to_sort_spec(
        indicator_column, Qt.SortOrder.AscendingOrder,
    ) == _DEFAULT_SORT_SPEC
    assert _column_order_to_sort_spec(
        indicator_column, Qt.SortOrder.DescendingOrder,
    ) == _DEFAULT_SORT_SPEC


def test_round_trip_all_valid_specs():
    """Every spec the config validator accepts must round-trip cleanly."""
    for spec in VALID_SESSION_LIST_SORTS:
        col, order = _sort_spec_to_column_order(spec)
        assert _column_order_to_sort_spec(col, order) == spec


def test_config_default_session_list_sort_is_date_desc():
    cfg = Config()
    assert cfg.ui.session_list_sort == "date_desc"
    assert cfg.validate() == []


@pytest.mark.parametrize("spec", VALID_SESSION_LIST_SORTS)
def test_config_accepts_each_valid_sort_spec(spec):
    cfg = Config()
    cfg.ui.session_list_sort = spec
    assert cfg.validate() == []


@pytest.mark.parametrize("bad_spec", ["", "bogus", "DATE_DESC", "audio_asc"])
def test_config_rejects_invalid_sort_spec(bad_spec):
    cfg = Config()
    cfg.ui.session_list_sort = bad_spec
    errors = cfg.validate()
    assert any("session_list_sort" in err for err in errors), errors


def test_config_round_trips_session_list_sort_through_toml(tmp_path):
    """Persistence: a user clicking Title-ascending must survive a restart."""
    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.ui.session_list_sort = "title_asc"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.ui.session_list_sort == "title_asc"
