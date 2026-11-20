"""Regressions for problems found by using the library as a newcomer would: the inputs
people reach for first (mnemonics, result objects, labels) and silent wrong answers."""
import json
import warnings

import pytest
from conftest import fixture_json, fixture_text

from ds_web import Exclude, LoginError, Query, SearchHit, Series, TreeNode
from ds_web._session import Session
from ds_web.errors import DatastreamWebError
from ds_web.query import as_query

RESOLVE = {obj["_key"]: obj for obj in fixture_json("resolve.json")}  # TRUK10T, VOD


def _resolver(data, **_):  # like the site: answers in order, drops unknowns
    return json.dumps([RESOLVE[s] for s in data["symbols"].split("|") if s in RESOLVE])


def _no_hits_page():
    """search_futures.html as the site serves a page past the end: no grid, 0 hits."""
    return (fixture_text("search_futures.html")
            .replace('"totalHits":1679', '"totalHits":0')
            .replace('id="resulttable"', 'id="noresults"'))


def _node(subset="exp1|9-3811|BIG#2992||Y|||"):
    return TreeNode(id="9-3811", text="Angola [3]", parent="9-3809", depth=1, type="series",
                    has_children=False, subset=subset, path="Angola [3]", size=3)


class TestQueryInputs:
    def test_category_exclude_is_refused(self):
        # the site reads nav_category=-15 as category 0 (Equities) rather than "not Futures"
        with pytest.raises(ValueError, match="cannot exclude categories"):
            Query("gold", category=Exclude("Futures"))
        Query("gold", market=Exclude("Japan"))  # other filters still exclude

    def test_subset_takes_a_tree_node(self):
        assert Query(subset=_node()).subset == "exp1|9-3811|BIG#2992||Y|||"
        assert Query("x").replace(subset=_node()).subset == "exp1|9-3811|BIG#2992||Y|||"
        with pytest.raises(ValueError, match="holds no series"):
            Query(subset=_node(subset=None))
        with pytest.raises(TypeError, match="subset must be"):
            Query(subset=42)

    def test_entitled_is_validated(self):
        with pytest.raises(TypeError, match="entitled"):
            Query("gold", entitled="yes")
        assert Query("gold", entitled=None).entitled is None

    def test_sort_accepts_labels_and_any_case(self):
        assert Query("x", sort="Name").sort == "N"
        assert Query("x", sort="end").sort == "END"
        assert Query("x", sort="Settlement Date").sort == "END"
        assert Query("x").replace(sort="hist").sort == "S"
        with pytest.warns(UserWarning, match="not a known sort code"):
            Query("x", sort="ZZZ")

    def test_blank_term_is_no_term(self):
        assert Query("   ").term is None
        assert as_query("").is_empty()
        assert Query("  ", category="Futures").to_params(None) == {"nav_category": "15"}

    def test_subset_string_where_a_term_goes(self):
        assert as_query("mex1|9-3811|9-3812") == Query(subset="mex1|9-3811|9-3812")
        assert as_query("gold").term == "gold"


class TestSeriesInputs:
    def test_notes_accepts_a_mnemonic(self, make_client):
        client, fake = make_client({
            "resolveLegacySelections.aspx": _resolver,
            "notes.aspx": fixture_text("notes_usgdp.html"),
        })
        client.notes("TRUK10T")
        assert fake.calls[-1]["data"] == {"ids": "2424608"}

    def test_result_objects_and_ints_skip_resolving(self, make_client):
        client, fake = make_client({"notes.aspx": fixture_text("notes_usgdp.html")})
        hit = SearchHit(series_id="246312", name="UK CPI", symbol="UKCONPRCF")
        client.notes(hit)
        client.notes(246312)
        client.notes(" 246312 ")
        assert [c["data"] for c in fake.calls] == [{"ids": "246312"}] * 3

    def test_unknown_symbol_is_a_clear_error(self, make_client):
        client, _ = make_client({
            "resolveLegacySelections.aspx": "[]",
            "search.aspx": fixture_text("search_empty.html"),
            "hitdata.aspx": "[]",
        })
        with pytest.raises(DatastreamWebError, match="no series found for symbol 'NOTREAL'"):
            client.datatypes("NOTREAL")

    def test_bad_types_are_type_errors(self, make_client):
        client, _ = make_client({})
        with pytest.raises(TypeError, match="series id, a symbol"):
            client.notes(["246312"])
        with pytest.raises(ValueError, match="empty"):
            client.notes("  ")
        with pytest.raises(TypeError, match="one symbol"):
            client.lookup(["VOD"])

    def test_details_many_keys_follow_the_input(self, make_client):
        def details(data, **_):
            ids = data["ids"].split(",")
            return fixture_text("details_batch.html") if ids else ""

        client, _ = make_client({"resolveLegacySelections.aspx": _resolver, "searchstraggler.aspx": details})
        from ds_web import _parsers as parse
        known = list(parse.parse_details(fixture_text("details_batch.html")))
        sid = known[0]
        found = client.details_many([int(sid), sid])
        assert set(found) == {int(sid), sid}

    def test_resolve_takes_ints(self, make_client):
        client, fake = make_client({"resolveLegacySelections.aspx": _resolver,
                                    "search.aspx": fixture_text("search_empty.html"), "hitdata.aspx": "[]"})
        assert client.resolve([953133]) == {"953133": None}
        assert fake.calls[0]["data"]["symbols"] == "953133"

    def test_release_dates_frequency_code_or_word(self, make_client):
        client, fake = make_client({"ndor.aspx": fixture_text("ndor_ukcpi.html")})
        client.release_dates("246312", frequency="Monthly")
        client.release_dates("246312", frequency="m")
        assert [c["data"]["frequency"] for c in fake.calls] == ["M", "M"]
        with pytest.raises(ValueError, match="release code"):
            client.release_dates("246312", frequency="Fortnightly")

    def test_constituents_needs_a_symbol(self, make_client):
        client, _ = make_client({})
        with pytest.raises(ValueError, match="needs a list"):
            client.constituents("")


class TestResolveFallback:
    def test_ds_code_the_resolver_misses(self, make_client):
        # resolveLegacySelections.aspx misses bond DS codes and RICs; fielded search finds them
        def resolver(data, **_):
            return json.dumps([RESOLVE[s] for s in data["symbols"].split("|") if s == "VOD"])

        searched = []

        def search(params, **_):
            searched.append(params["q"])
            return fixture_text("search_futures.html")

        client, _ = make_client({
            "resolveLegacySelections.aspx": resolver,
            "search.aspx": search,
            # answer for the first hit's id, as the series behind the DS code
            "hitdata.aspx": lambda data, **_: json.dumps([{**RESOLVE["TRUK10T"], "docid": data["ids"].split("|")[0]}]),
        })
        found = client.resolve(["VOD", "S310R0", "NOT*REAL"])
        assert found["S310R0"].symbol == "TRUK10T"
        assert found["VOD"].symbol == "VOD"
        assert found["NOT*REAL"] is None
        assert searched == ["DSCD:or(S310R0)"]  # wildcards never reach the fallback


class TestSearchEdges:
    def test_combined_subset_warns_about_filters(self, make_client):
        client, _ = make_client({"search.aspx": fixture_text("search_futures.html")})
        with pytest.warns(UserWarning, match="no filters to a combined subset"):
            client.search(subset="mex1|9-3811|9-3812")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            client.search(subset="mex1|9-3811|9-3812", entitled=None)
            client.search(subset="exp1|9-3811|BIG#2992||Y|||")

    def test_page_past_the_end_reports_the_real_total(self, make_client):
        def search(params, **_):
            return _no_hits_page() if params.get("page") == "900" else fixture_text("search_futures.html")

        client, _ = make_client({"search.aspx": search})
        real = client.count("sugar", category="Futures")
        with pytest.warns(UserWarning, match="past the end"):
            page = client.search("sugar", category="Futures", page=900)
        assert page.hits == [] and page.total_hits == real

    def test_snapshot_period_is_validated(self, make_client):
        client, fake = make_client({})
        with pytest.raises(ValueError, match="period"):
            client.snapshot("FTSE 100", change="banana")
        with pytest.raises(ValueError, match="period"):
            client.snapshot("FTSE 100", percent_change="YTD")
        assert fake.calls == []

    def test_export_format_any_case(self, make_client):
        client, fake = make_client({"excelexport.aspx": "a,b\n"})
        client.export("gold", format="CSV")
        assert fake.calls[0]["params"]["format"] == "csv"


class TestCombineExplorers:
    def test_single_node_gives_its_own_subset(self, make_client):
        client, _ = make_client({"multiexplorerencoding.aspx": '"exp1|9-3811|BIG#2992||Y|||"'})
        assert client.combine_explorers([_node()]) == "exp1|9-3811|BIG#2992||Y|||"

    def test_no_known_nodes_raises(self, make_client):
        client, _ = make_client({"multiexplorerencoding.aspx": '"mex1|"'})
        with pytest.raises(ValueError, match="none of these"):
            client.combine_explorers(["99-999999999"])


def test_failed_sign_in_is_detected(monkeypatch):
    """A failed sign-in re-serves DSLogon.aspx with ...search.aspx in its redirect
    parameter, which a substring check on the URL took for success."""
    session = Session("NOTAREALUSER", "x", timeout=5, pool_size=1)

    class Page:
        status_code = 200
        text = '<form><input name="usernameTextBox"/><input name="passwordTextBox"/></form>'
        url = "https://x/dsws/1.0/DSLogon.aspx?redirect=https%3a%2f%2fx%2fbrowse%2fsearch.aspx"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(session.http, "get", lambda *a, **k: Page())
    monkeypatch.setattr(session.http, "post", lambda *a, **k: Page())
    with pytest.raises(LoginError, match="check the credentials"):
        session.login()


def test_series_accepts_result_objects(make_client):
    client, fake = make_client({"hitdata.aspx": json.dumps([RESOLVE["VOD"]])})
    found = client.series([Series(series_id="23675100", name="VOD", symbol="VOD")])
    assert found[0].symbol == "VOD"
    assert fake.calls[0]["data"] == {"ids": "23675100"}


def _details(name):
    from ds_web import _parsers as parse
    return next(iter(parse.parse_details(fixture_text(name)).values()))


class TestDetailsShortcuts:
    def test_economics_market_carries_the_currency(self):
        d = _details("details_ukcpi.html")
        assert d.fields["market"] == "United Kingdom (United Kingdom Pound)"
        assert (d.market, d.currency) == ("United Kingdom", "United Kingdom Pound")
        assert d.frequency == "M"
        assert d.latest_value == 143.643
        assert d.ric == "aGBCPI" and d.ds_mnemonic == d.mnemonic

    def test_market_data_series(self):
        d = _details("details_ftse100.html")
        assert (d.market, d.currency, d.frequency, d.latest_value) == (
            "United Kingdom", "United Kingdom Pound", "D", 10541.69)
        assert d.isin is None and d.ds_code is None

    def test_to_dict_uses_the_shortcuts(self):
        row = _details("details_ukcpi.html").to_dict()
        assert row["market"] == "United Kingdom"
        assert row["currency"] == "United Kingdom Pound"
        assert row["frequency"] == "M" and "frequency_code" not in row

    def test_frequency_falls_back_to_the_timespan(self):
        from ds_web import SeriesDetails
        d = SeriesDetails(series_id="1", full_name=None, fields={"timespan": "Q1 1947 - Q3 2026, Quarterly"})
        assert d.frequency == "Q"
        assert SeriesDetails(series_id="1", full_name=None).frequency is None


class TestNameAliases:
    def test_datatype_names_match_both_ways(self):
        import datetime as dt

        from ds_web import Datatype, DatatypeCoverage
        cov = DatatypeCoverage("RI", "Total Return Index", dt.date(1988, 10, 1))
        dty = Datatype("RI", "Total Return Index", True, dt.date(1988, 10, 1))
        assert (cov.mnemonic, cov.since) == (dty.mnemonic, dty.since)
        assert (dty.code, dty.available_from) == (cov.code, cov.available_from)

    def test_series_mnemonic_and_frequency(self):
        s = Series(series_id="1", name="x", symbol="UKCONPRCF", ds_mnemonic="UKCONPRCF",
                   extra={"frequency": "Monthly"})
        assert s.mnemonic == "UKCONPRCF" and s.frequency == "M"

    def test_hit_data_frequency_is_a_code(self):
        from ds_web import _parsers as parse
        s = parse.parse_series({"docid": "1", "Name": "x", "_key": "X", "Frequency": "Quarterly"})
        assert s.extra["frequency"] == "Q" and s.frequency == "Q"


def test_economics_local_code_placeholder_is_dropped():
    from ds_web import _parsers as parse
    s = parse.parse_series({"docid": "246312", "Name": "UK CPI", "_key": "UKCONPRCF", "Local Code": "Country"})
    assert s.local_code is None and "local_code" not in s.extra
    assert parse.parse_series({**RESOLVE["VOD"]}).local_code == "UK:VOD"
