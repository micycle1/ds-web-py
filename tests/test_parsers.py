import datetime as dt

import pytest
from conftest import fixture_json, fixture_text

from ds_web import Query
from ds_web import _parsers as parse
from ds_web.errors import ParseError


@pytest.fixture(scope="module")
def futures():
    return parse.parse_search_page(fixture_text("search_futures.html"), Query("sugar"), 1)


class TestSearchPage:
    def test_hits(self, futures):
        assert futures.total_hits == 1679
        assert len(futures.hits) == 15
        hit = futures.hits[0]
        assert (hit.series_id, hit.name, hit.mnemonic) == ("67924415", "CSCE - SUGAR #11 CONTINUOUS", "NSBCS00")
        assert hit.status == ("Active", "Continuous Series", "Not Principal")
        assert hit.active is True
        assert hit.fields["exchange"] == "ICE Futures U.S."
        assert hit.fields["underlying_series"] == "Raw Sugar-ISA Daily Price c/lb"
        assert not futures.is_complete

    def test_sort_options(self, futures):
        assert futures.sort_options["Name"] == "N"
        assert futures.sort_options["Settlement Date"] == "END"

    def test_filters(self, futures):
        category = [f for f in futures.filters if f.filter_name == "category"]
        assert category[0].applied and category[0].value_label == "Futures"
        exchanges = [f for f in futures.filters if f.filter_name == "exchange"]
        assert any(f.value == "ICE Futures U.S." and f.count and f.count > 100 for f in exchanges)
        assert all(f.param == "nav_exchange" for f in exchanges)

    def test_search_ref(self, futures):
        assert Query.from_ref(futures.search_ref) == Query("sugar", category="Futures", entitled=True)

    def test_unfiltered_grid_columns_and_notes(self):
        page = parse.parse_search_page(fixture_text("search_unfiltered.html"), Query("gdp"), 1)
        hit = page.hits[0]
        assert set(hit.fields) == {"category", "market", "origin"}
        assert hit.has_notes and hit.status == ("Active", "Key Indicator", "Headline")
        assert page.explorer_suggestions and page.explorer_suggestions[0].subset.startswith("exp1|")
        assert page.explorer_suggestions[1].path.startswith("Economics »")

    def test_categories(self):
        cats = parse.parse_categories(fixture_text("search_empty.html"))
        assert len(cats) == 15
        assert {c.name: c.id for c in cats}["Futures"] == "15"
        assert all(c.count and c.count > 0 for c in cats)

    def test_datatype_search_page(self):
        page = parse.parse_search_page(fixture_text("search_datatypes.html"), Query(), 1)
        assert page.total_hits == 20
        assert {(h.mnemonic, h.fields["datatype_category"]) for h in page.hits} >= {("MV", "Equities"), ("DY", "Equities")}

    def test_login_page_rejected(self):
        login = '<input name="usernameTextBox" /><input name="passwordTextBox" />'
        assert parse.is_login_page(login)
        with pytest.raises(ParseError):
            parse.parse_search_page(login, Query("x"), 1)

    def test_page_data(self):
        data = parse.extract_page_data(fixture_text("search_futures.html"))
        assert data["totalHits"] == 1679
        assert parse.extract_page_data("no page data") == {}


def test_snapshot():
    snap = parse.parse_snapshot(fixture_text("snapshot.html"))
    assert [c.datatype for c in snap.columns][:2] == ["PI", "DY"]
    assert "PCH#(X(PI),1Y)" in {c.datatype for c in snap.columns}
    assert len(snap.rows) == 5 and snap.total_hits == 159
    row = snap.rows[0]
    assert (row.name, row.mnemonic) == ("FTSE 100", "FTSE100")
    assert all(isinstance(v, float) or v is None for v in row.values.values())


class TestDetails:
    def test_index(self):
        d = parse.parse_details(fixture_text("details_ftse100.html"))["173737703"]
        assert d.full_name == "FTSE 100"
        assert d.symbols == {"mnemonic": "FTSE100", "ric": ".FTSE", "t1_code": "UKX-FT", "ibes_aggregate": "@:UKFT100", "ldb": "SIF"}
        assert d.mnemonic == "FTSE100" and d.ldb == "SIF"
        assert d.fields["market"] == "United Kingdom"
        assert d.headline_datatypes[0].code == "PI" and d.headline_datatypes[0].available_from == "Dec 1983"
        assert "ibes_aggregate" in d.datatypes
        contains = d.links["contains"][0]
        assert contains.count == 100 and contains.subset.startswith("rel1|")
        assert {link.count for link in d.links["derivatives"]} == {4844, 192}
        assert d.links["explorers"][1].path.startswith("Equity Indices » By Source")
        assert d.chart["symbol"] == "FTSE100" and "PI" in d.chart_datatypes
        assert d.start_date == dt.date(1983, 12, 30)
        assert d.release_frequency is None

    def test_economic_series(self):
        d = parse.parse_details(fixture_text("details_ukcpi.html"))["246312"]
        assert d.release_frequency == "M"
        assert d.urls["source"].startswith("http://www.ons.gov.uk")
        assert d.fields["next_date_of_release"] == "21/10/2026"  # "More..." link stripped
        assert d.notes and d.notes[0].title
        filtered = [link for link in d.links["explorers"] if link.filters]
        assert filtered and all(k.startswith("nav_") for k in filtered[0].filters)
        q = filtered[0].query()
        assert q.subset == filtered[0].subset and "frequency" in q.filters

    def test_future_label_collision(self):
        d = parse.parse_details(fixture_text("details_future.html"))["67924415"]
        assert d.symbols["underlying_series"] == "WSUGDLY"
        assert d.fields["underlying_series"] == "Raw Sugar-ISA Daily Price c/lb"
        row = d.to_dict()
        assert row["underlying_series"] == "Raw Sugar-ISA Daily Price c/lb"
        assert row["underlying_series_symbol"] == "WSUGDLY"
        assert "settlement_date" not in d.fields  # "-" is the site's empty value

    def test_batch(self):
        assert set(parse.parse_details(fixture_text("details_batch.html"))) == {"2424608", "23675100", "305323912"}


def test_datatypes():
    found = parse.parse_datatypes(fixture_text("datatypes_ftse100.html"))
    assert len(found) == 26
    dy = next(d for d in found if d.mnemonic == "DY")
    assert dy.time_series and dy.since == dt.date(1985, 12, 31) and dy.category_id == "3"
    assert next(d for d in found if d.mnemonic == "BDATE").time_series is False


def test_datatype_definition():
    d = parse.parse_datatype_definition(fixture_text("datatype_definition_dy.html"), "DY", "3")
    assert "Dividend yield" in d.text and "<" not in d.text


def test_notes():
    notes = parse.parse_notes(fixture_text("notes_usgdp.html"))
    assert [n.title for n in notes] == ["Gross Domestic Product United States", "Link to Current Statistical Release"]
    assert notes[0].text.startswith("Gross domestic product (GDP)")


def test_release_dates():
    dates = parse.parse_release_dates(fixture_text("ndor_ukcpi.html"))
    assert dates[0].date == dt.date(2026, 10, 21)
    assert (dates[0].time, dates[0].period) == ("06:00 GMT", "Sep 2026")


class TestSeriesJson:
    def test_hit_data_drops_empty_objects(self):
        found = parse.parse_series_list(fixture_json("hitdata_ids.json"))
        assert [s.series_id for s in found] == ["2424608", "23675100"]
        vod = found[1]
        assert (vod.isin, vod.sedol, vod.ric, vod.ds_code) == ("GB00BH4HKS39", "BH4HKS3", "VOD.L", "953133")
        assert vod.base_date == dt.date(1988, 10, 25)
        assert found[0].ldb == "INT"

    def test_parse_series_empty(self):
        assert parse.parse_series({}) is None
        with pytest.raises(ParseError):
            parse.parse_series_list({"not": "a list"})

    def test_constituents_shape(self):
        members = parse.parse_series_list(fixture_json("constituents_lftse100.json")["constituents"])
        assert len(members) == 100 and members[0].name == "3I GROUP"


class TestTree:
    def test_root(self):
        roots = parse.tree_children(fixture_json("tree_root.json"), "#")
        assert ("12-4731", "Economics") in [(n["id"], n["text"]) for n in roots]

    def test_node_and_sizes(self):
        children = parse.tree_children(fixture_json("tree_node.json"), "12-4238")
        node = parse.tree_node(children[0], "12-4238", 1, ("Economics", "UK"))
        assert node.subset == "exp1|12-602|M#UKKEY||Y|||"
        assert node.size == 73 and node.path == "Economics » UK » Key Indicators [73]"

    def test_search_path(self):
        roots = parse.tree_children(fixture_json("tree_root.json"), "#")
        path = parse.tree_path(fixture_json("tree_search.json"), roots)
        assert [n.text for n in path][0] == "Economics"
        assert path[-1].subset == "exp1|12-4591|M#CONPRCX||Y|||" and path[-1].size == 143
        assert [n.depth for n in path] == list(range(1, len(path) + 1))
        assert path[1].parent == path[0].id


def test_snake_case():
    assert parse.snake_case("Maturity Date") == "maturity_date"
    assert parse.snake_case("Hist.") == "hist"
    assert parse.snake_case("categoryId") == "category_id"
    assert parse.snake_case("seriesstartdate") == "seriesstartdate"
