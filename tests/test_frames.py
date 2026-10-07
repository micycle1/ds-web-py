import datetime as dt

import pytest
from conftest import fixture_text

pd = pytest.importorskip("pandas")

from ds_web import Series, to_frame  # noqa: E402
from ds_web import _parsers as parse  # noqa: E402


def test_series_frame_spreads_extra_and_parses_dates():
    frame = to_frame([
        Series("1", "A", "AAA", base_date=dt.date(2000, 1, 3), extra={"note": "x"}),
        Series("2", "B", "BBB"),
    ])
    assert list(frame.columns[:3]) == ["series_id", "name", "symbol"]
    assert frame.loc[0, "note"] == "x"
    assert str(frame["base_date"].dtype).startswith("datetime64")


def test_snapshot_frame_uses_labels():
    frame = to_frame(parse.parse_snapshot(fixture_text("snapshot.html")))
    assert {"series_id", "name", "mnemonic", "Price index"} <= set(frame.columns)
    assert len(frame) == 5


def test_details_frame_flattens():
    details = parse.parse_details(fixture_text("details_future.html"))
    frame = to_frame(details.values())
    assert frame.loc[0, "underlying_series_symbol"] == "WSUGDLY"


def test_search_page_frame():
    from ds_web import Query

    page = parse.parse_search_page(fixture_text("search_futures.html"), Query("sugar"), 1)
    frame = to_frame(page)
    assert len(frame) == 15 and "exchange" in frame.columns


def test_empty():
    assert to_frame([]).empty
