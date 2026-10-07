"""Against the real site. Run with:  DS_WEB_USERNAME=... DS_WEB_PASSWORD=... pytest -m live

These check the site still behaves as the library assumes — the caps, the parameter
conventions, the response shapes — so a failure here usually means the site changed.
"""
import os
import warnings

import pytest

from ds_web import ALL, BULK_CAP, SHOW_ALL_CAP, DatastreamWebClient, Exclude, Query, TruncatedResultsWarning

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("DS_WEB_USERNAME"), reason="needs DS_WEB_USERNAME/DS_WEB_PASSWORD"),
]


@pytest.fixture(scope="module")
def ds():
    with DatastreamWebClient() as client:
        yield client


def test_search_and_filters(ds):
    page = ds.search("sugar", category="Futures")
    assert page.total_hits > 100 and len(page.hits) == 15
    assert any(f.filter_name == "exchange" for f in page.filters)
    assert ds.count("sugar", category="Futures", exchange=Exclude("ICE Futures U.S.")) < page.total_hits


def test_show_all_cap(ds):
    with pytest.warns(TruncatedResultsWarning):
        page = ds.search("gold", category="Equities", page=ALL)
    assert len(page.hits) == SHOW_ALL_CAP < page.total_hits


def test_search_all_is_complete_and_distinct(ds):
    query = Query("gold", category="Equities")
    found = ds.search_all(query)
    assert len(found) == ds.count(query) > SHOW_ALL_CAP
    assert len({s.series_id for s in found}) == len(found)


def test_bulk_cap(ds):
    assert len(ds.search_all("sugar", limit=BULK_CAP)) == BULK_CAP


def test_resolve(ds):
    found = ds.resolve(["VOD", "GB00BH4HKS39", "VOD.L", "NOTREAL", "TRUK10T"])
    assert found["VOD"].series_id == found["GB00BH4HKS39"].series_id == found["VOD.L"].series_id
    assert found["NOTREAL"] is None and found["TRUK10T"].ric == "GB10YT=RR"


def test_details_and_extras(ds):
    d = ds.details(ds.lookup("FTSE100").series_id)
    assert d.full_name == "FTSE 100" and d.links["contains"][0].count >= 100
    assert any(t.mnemonic == "DY" for t in ds.datatypes(d.series_id))
    assert ds.chart(d)[:4] == b"\x89PNG"
    cpi = ds.lookup("UKCONPRCF")
    assert ds.release_dates(cpi.series_id)


def test_constituents(ds):
    members = ds.constituents("LFTSE100")
    assert len(members) == 100 and all(m.isin for m in members)


def test_snapshot(ds):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", TruncatedResultsWarning)
        snap = ds.snapshot("FTSE 100", category="Equity Indices")
    assert snap.rows and "PI" in snap.rows[0].values


def test_frame(ds):
    pytest.importorskip("pandas")
    frame = ds.search_frame(subset="exp1|12-4416|M#UKKEY||Y|||")
    assert len(frame) > 50 and frame["full_name"].notna().all()


def test_datatypes_and_tree(ds):
    assert {h.mnemonic for h in ds.lookup_datatypes(["PI", "DY"])} == {"PI", "DY"}
    assert "Dividend" in ds.datatype_definition("DY", "Equity Indices").text
    path = ds.tree_search("inflation")
    assert path and path[-1].subset
    assert ds.count(subset=path[-1].subset) > 0
