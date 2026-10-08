"""Client logic against a fake session: request shapes, batching and edge cases."""
import json

import pytest
from conftest import FakeResponse, fixture_json, fixture_text

from ds_web import ALL, ResultSetTooLargeError, Series, TruncatedResultsWarning
from ds_web.client import _align
from ds_web.errors import DatastreamWebError, ServerError


def _series(i, symbol=None):
    return {"docid": str(i), "Name": f"S{i}", "_key": symbol or f"SYM{i}", "DS Mnemonic": symbol or f"SYM{i}"}


class TestSearch:
    def test_params_and_entitlement(self, make_client):
        client, fake = make_client({"search.aspx": fixture_text("search_futures.html")})
        page = client.search("sugar", category="Futures", page=2)
        params = fake.calls[0]["params"]
        assert params == {"q": "sugar", "nav_category": "15", "nav_ldbpermission": "Entitled", "page": "2"}
        assert page.page == 2 and len(page.hits) == 15

    def test_entitled_only_off(self, make_client):
        client, fake = make_client({"search.aspx": fixture_text("search_futures.html")}, entitled_only=False)
        client.search("sugar")
        assert "nav_ldbpermission" not in fake.calls[0]["params"]

    @pytest.mark.parametrize("entitled_only", [True, False])
    def test_needs_something_to_search(self, make_client, entitled_only):
        client, fake = make_client({}, entitled_only=entitled_only)
        for method in (client.search, client.search_all, client.export, client.snapshot, client.count):
            with pytest.raises(TypeError, match="needs a term"):
                method()
        assert fake.calls == []

    def test_accepts_links_and_nodes_but_not_other_objects(self, make_client):
        from ds_web import Link, TreeNode

        client, fake = make_client({"search.aspx": fixture_text("search_futures.html")})
        client.search(Link("100 Constituents", "rel1|x", {"nav_source": "A"}))
        assert fake.calls[-1]["params"]["subset"] == "rel1|x" and fake.calls[-1]["params"]["nav_source"] == "A"
        client.search(TreeNode("1", "n", None, 1, "series", False, "exp1|y", "n"))
        assert fake.calls[-1]["params"]["subset"] == "exp1|y"
        with pytest.raises(TypeError):
            client.search(object())

    def test_count_of_sorted_query_reads_one_page(self, make_client):
        client, fake = make_client({"search.aspx": fixture_text("search_futures.html")})
        assert client.count("sugar", sort="N") == 1679
        assert "page" not in fake.calls[0]["params"] and "s" not in fake.calls[0]["params"]

    def test_rejects_bare_strings(self, make_client):
        client, _ = make_client({})
        for call in (lambda: client.resolve("VOD"), lambda: client.details_many("123"),
                     lambda: client.series("123"), lambda: client.combine_explorers("12-1")):
            with pytest.raises(TypeError, match="single string"):
                call()

    def test_sort_fetches_all(self, make_client):
        client, fake = make_client({"search.aspx": fixture_text("search_futures.html")})
        with pytest.warns(TruncatedResultsWarning):  # fixture is page 1 of 1679
            client.search("sugar", sort="N")
        assert fake.calls[0]["params"]["page"] == str(ALL) and fake.calls[0]["params"]["s"] == "N"
        with pytest.raises(ValueError):
            client.search("sugar", sort="N", page=3)

    def test_bad_page(self, make_client):
        client, _ = make_client({})
        with pytest.raises(ValueError):
            client.search("x", page=0)


class TestSearchAll:
    def test_returns_series(self, make_client):
        client, fake = make_client({"hitdata.aspx": json.dumps([_series(1), _series(2), {}])})
        found = client.search_all("x")
        assert [s.series_id for s in found] == ["1", "2"]
        assert fake.calls[0]["mode"] == {"host": "unity"}
        assert fake.calls[0]["params"]["q"] == "x"

    def test_cap_raises_when_query_is_bigger(self, make_client):
        rows = json.dumps([_series(i) for i in range(12000)])
        client, _ = make_client({"hitdata.aspx": rows, "search.aspx": fixture_text("search_unfiltered.html")})
        with pytest.raises(ResultSetTooLargeError) as info:
            client.search_all("gdp")
        assert info.value.total_hits == 2117515
        assert len(client.search_all("gdp", limit=500)) == 500

    def test_small_limit_uses_first_page(self, make_client):
        client, fake = make_client({
            "search.aspx": fixture_text("search_futures.html"),
            "hitdata.aspx": lambda **kw: json.dumps([_series(i) for i in kw["data"]["ids"].split("|")]),
        })
        found = client.search_all("sugar", limit=3)
        assert len(found) == 3
        assert [c["endpoint"] for c in fake.calls] == ["search.aspx", "hitdata.aspx"]

    def test_limit_validation(self, make_client):
        client, _ = make_client({})
        assert client.search_all("x", limit=0) == []
        with pytest.raises(ValueError):
            client.search_all("x", limit=12001)


class TestResolve:
    def test_align_by_order_and_identity(self):
        a, b = Series("1", "A", "AAA", isin="GB1"), Series("2", "B", "BBB")
        assert _align(["AAA", "BBB"], [a, b]) == {"AAA": a, "BBB": b}
        aligned = _align(["X", "GB1", "BBB"], [a, b])
        assert aligned["GB1"] is a and aligned["BBB"] is b
        assert aligned["X"] is not a and aligned["X"] is not None  # ambiguous marker

    def test_unknowns_and_case(self, make_client):
        known = {obj["_key"]: obj for obj in fixture_json("resolve.json")}  # TRUK10T, VOD

        def handler(data, **_):  # like the site: answers in order, drops unknowns
            return json.dumps([known[s] for s in data["symbols"].split("|") if s in known])

        client, fake = make_client({"resolveLegacySelections.aspx": handler})
        result = client.resolve(["TRUK10T", "NOTREAL", "VOD", "vod"])
        assert result["TRUK10T"].series_id == "2424608"
        assert result["NOTREAL"] is None
        assert result["vod"] is result["VOD"]
        sent = fake.calls[0]["data"]["symbols"].split("|")
        assert sent == ["TRUK10T", "NOTREAL", "VOD"]  # case-duplicates sent once

    def test_bisects_on_duplicate_series_failure(self, make_client):
        def handler(data, **_):
            symbols = data["symbols"].split("|")
            if {"VOD", "VOD.L"} <= set(symbols):
                raise ServerError("boom", 500)
            return json.dumps([_series(23675100, "VOD") for s in symbols if s.startswith("VOD")] +
                              [_series(2424608, "TRUK10T") for s in symbols if s == "TRUK10T"])

        client, fake = make_client({"resolveLegacySelections.aspx": handler})
        result = client.resolve(["VOD", "VOD.L", "TRUK10T"])
        assert result["VOD"].series_id == result["VOD.L"].series_id == "23675100"
        assert result["TRUK10T"].symbol == "TRUK10T"
        assert len(fake.calls) >= 3

    def test_single_symbol_failure_propagates(self, make_client):
        def handler(**_):
            raise ServerError("boom", 500)

        client, _ = make_client({"resolveLegacySelections.aspx": handler})
        with pytest.raises(ServerError):
            client.lookup("X")


class TestDetails:
    def test_batches_of_200(self, make_client):
        def handler(data, **_):
            ids = data["ids"].split(",")
            assert len(ids) <= 200
            return "".join(f'<div class="spot-wrapper" id="spot_{i}"><h3>N{i}</h3></div>' for i in ids)

        client, fake = make_client({"searchstraggler.aspx": handler}, max_workers=1)
        found = client.details_many(str(i) for i in range(450))
        assert len(found) == 450 and len(fake.calls) == 3
        assert found["7"].full_name == "N7"

    def test_missing(self, make_client):
        client, _ = make_client({"searchstraggler.aspx": ""})
        with pytest.raises(DatastreamWebError):
            client.details("1")

    def test_release_dates_without_calendar(self, make_client):
        client, fake = make_client({"searchstraggler.aspx": fixture_text("details_ftse100.html")})
        assert client.release_dates("173737703") == []
        assert [c["endpoint"] for c in fake.calls] == ["searchstraggler.aspx"]

    def test_release_dates_looks_up_frequency(self, make_client):
        client, fake = make_client({
            "searchstraggler.aspx": fixture_text("details_ukcpi.html"),
            "ndor.aspx": fixture_text("ndor_ukcpi.html"),
        })
        assert len(client.release_dates("246312")) == 4
        assert fake.calls[1]["data"] == {"id": "246312", "frequency": "M"}

    def test_chart_rejects_non_image(self, make_client):
        client, _ = make_client({
            "searchstraggler.aspx": fixture_text("details_ftse100.html"),
            "thumbnail.aspx": FakeResponse("<html>", "text/html"),
        })
        with pytest.raises(ServerError):
            client.chart("173737703")

    def test_chart_params(self, make_client):
        client, fake = make_client({
            "searchstraggler.aspx": fixture_text("details_ftse100.html"),
            "thumbnail.aspx": FakeResponse("PNG", "image/png"),
        })
        assert client.chart("173737703", datatype="DY") == b"PNG"
        params = fake.calls[-1]["params"]
        assert params["datatype"] == "DY" and params["symbol"] == "FTSE100"
        assert params["recent"] == "false" and params["summed"] == "false"


class TestConstituents:
    def test_enriches_with_identifiers(self, make_client):
        client, _ = make_client({
            "expandmnemonics.aspx": fixture_text("constituents_lftse100.json"),
            "hitdata.aspx": lambda data, **_: json.dumps([_series(i) for i in data["ids"].split("|")]),
        })
        members = client.constituents("LFTSE100")
        assert len(members) == 100 and members[0].ds_mnemonic == "SYM240305"

    def test_outage_is_not_reported_as_unknown_list(self, make_client):
        def handler(**_):
            raise ServerError("unavailable", 503)

        client, _ = make_client({"expandmnemonics.aspx": handler})
        with pytest.raises(ServerError, match="unavailable"):
            client.constituents("LFTSE100")

    def test_unknown_list_warns(self, make_client):
        client, _ = make_client({"expandmnemonics.aspx": {"constituents": [], "error": "No list exists"}})
        with pytest.warns(UserWarning, match="No list exists"):
            assert client.constituents("L#NOPE", identifiers=False) == []


class TestSaveList:
    def test_normalizes_and_saves(self, make_client):
        client, fake = make_client({
            "resolveLegacySelections.aspx": fixture_text("resolve.json"),
            "usercreateddata.aspx": {"isError": False, "message": "Saved", "lstdetails": {"id": "L#MYLIS"}},
        })
        saved = client.save_list("l#mylist", "My list", ["TRUK10T", "VOD"])
        assert saved.mnemonic == "L#MYLIST"  # upper-cased and prefixed
        with pytest.raises(ValueError, match="at most 6"):
            client.save_list("mylistname", "d", ["VOD"])
        sent = fake.calls[-1]["data"]
        assert sent["command"] == "newucl" and sent["ids"] == "2424608|23675100" and sent["symbols"] == "TRUK10T|VOD"

    def test_overwrite_needs_consent(self, make_client):
        replies = iter([{"overwrite": "L#X", "message": "exists"}, {"isError": False, "message": "ok"}])
        client, fake = make_client({"usercreateddata.aspx": lambda **_: next(replies)})
        member = Series("1", "A", "A")
        with pytest.raises(DatastreamWebError, match="overwrite=True"):
            client.save_list("X", "d", [member])
        replies = iter([{"overwrite": "L#X", "message": "exists"}, {"isError": False, "message": "ok"}])
        client.save_list("X", "d", [member], overwrite=True)
        assert fake.calls[-1]["data"]["command"] == "saveucl"

    def test_unresolvable_symbols_raise(self, make_client):
        client, _ = make_client({"resolveLegacySelections.aspx": "[]"})
        with pytest.raises(ValueError, match="cannot resolve"):
            client.save_list("X", "d", ["NOTREAL"])


def test_combine_explorers_decodes_json_string(make_client):
    client, fake = make_client({"multiexplorerencoding.aspx": '"mex1|12-4416|8-563"'})
    assert client.combine_explorers(["12-4416", "8-563"]) == "mex1|12-4416|8-563"
    assert fake.calls[0]["params"] == {"nids": "12-4416|8-563"}


def test_tree_walks_levels(make_client):
    routes = {"explorerleaves.aspx": lambda params, **_: json.dumps(
        fixture_json("tree_root.json") if params["nid"] == "#" else [{params["nid"]: []}]
    )}
    client, _ = make_client(routes, max_workers=2)
    nodes = client.tree(depth=2)
    assert len(nodes) == 8 and all(n.depth == 1 for n in nodes)
    with pytest.raises(ValueError):
        client.tree(depth=0)


def test_credentials_from_env(monkeypatch):
    from ds_web import DatastreamWebClient

    monkeypatch.delenv("DS_WEB_USERNAME", raising=False)
    monkeypatch.delenv("DS_WEB_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        DatastreamWebClient()
    monkeypatch.setenv("DS_WEB_USERNAME", "U")
    monkeypatch.setenv("DS_WEB_PASSWORD", "P")
    assert DatastreamWebClient().username == "U"


def test_tree_search_steps_through_matches(make_client):
    roots = fixture_json("tree_root.json")
    economics = next(n for n in roots if n["id"] == "12-4731")
    leaves = [{"id": f"12-{i}", "text": f"Gilts {i}", "data": f"exp1|12-{i}|L||Y|||", "children": False} for i in (1, 2, 3)]

    def handler(params, **_):
        if params.get("nid") == "#":
            return json.dumps(roots)
        position = 0 if not params["searchedId"] else int(params["searchedId"].split("-")[1])
        assert params["parentId"] == ""
        return json.dumps({"path": [{leaves[position]["id"]: []}, {economics["id"]: leaves}], "last": 1 if position == 2 else 0})

    client, _ = make_client({"explorerleaves.aspx": handler})
    matches = client.tree_search("gilts")
    assert [m.text for m in matches] == ["Gilts 1", "Gilts 2", "Gilts 3"]
    assert matches[0].path == "Economics » Gilts 1" and matches[0].subset == "exp1|12-1|L||Y|||"
    assert len(client.tree_search("gilts", limit=2)) == 2
