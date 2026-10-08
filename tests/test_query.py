import base64

import pytest

from ds_web import ENTITLED, NOT_ENTITLED, Exclude, Query, criteria
from ds_web.query import DEFAULT, resolve_category


class TestFilters:
    def test_bare_and_prefixed_names(self):
        assert Query(source="BoE").to_params(None) == {"nav_source": "BoE"}
        assert Query(nav_source="BoE").to_params(None) == {"nav_source": "BoE"}

    def test_list_joins_with_pipe(self):
        params = Query(exchange=["ICE", "CME"]).to_params(None)
        assert params["nav_exchange"] == "ICE|CME"

    def test_exclude_prefixes_minus(self):
        assert Query(market=Exclude("US", "Japan")).to_params(None)["nav_market"] == "-US|Japan"
        assert Query(market=Exclude(["US"])).to_params(None)["nav_market"] == "-US"

    def test_category_names_resolve(self):
        assert Query(category="Futures").to_params(None)["nav_category"] == "15"
        assert Query(category="bonds and convertibles").to_params(None)["nav_category"] == "13"
        assert Query(category=["Futures", "Options"]).to_params(None)["nav_category"] == "15|14"
        assert Query(category=0).to_params(None)["nav_category"] == "0"

    def test_unknown_category_raises(self):
        with pytest.raises(ValueError, match="unknown category"):
            Query(category="Spaceships")

    def test_tree_node_id_warns_and_reduces(self):
        with pytest.warns(UserWarning, match="top-level"):
            assert resolve_category("12-4428") == "12"

    def test_unknown_filter_raises_unless_prefixed(self):
        with pytest.raises(TypeError, match="unknown filter"):
            Query(sauce="hot")
        assert Query(nav_sauce="hot").to_params(None) == {"nav_sauce": "hot"}

    def test_ldbpermission_sets_entitled(self):
        # as the sidebar offers it; anything but NotEntitled means Entitled, as on the site
        assert Query(ldbpermission="Entitled").entitled is True
        assert Query(nav_ldbpermission="NotEntitled").entitled is False
        assert Query("x").replace(ldbpermission=None).entitled is None

    def test_prefixed_filter_survives_replace(self):
        q = Query(nav_sauce="hot").replace(term="y")
        assert q.to_params(None) == {"nav_sauce": "hot", "q": "y"}
        assert Query.from_url("?nav_foo=1").replace(term="x").filters == {"foo": "1"}

    def test_any_iterable_and_stable_sets(self):
        assert Query(source=(s for s in ["A", "B"])).to_params(None)["nav_source"] == "A|B"
        assert Query(source={"B", "A"}) == Query(source=["A", "B"])

    def test_term_must_be_text(self):
        with pytest.raises(TypeError):
            Query(42)  # type: ignore[arg-type]

    def test_none_drops_filter(self):
        assert Query(source=None).to_params(None) == {}

    def test_too_many_values(self):
        with pytest.raises(ValueError, match="at most 25"):
            Query(source=[str(i) for i in range(26)])


class TestEntitlement:
    def test_default_follows_client(self):
        assert Query("x").to_params(True)["nav_ldbpermission"] == ENTITLED
        assert "nav_ldbpermission" not in Query("x").to_params(False)

    def test_explicit_overrides(self):
        assert Query("x", entitled=False).to_params(True)["nav_ldbpermission"] == NOT_ENTITLED
        assert "nav_ldbpermission" not in Query("x", entitled=None).to_params(True)
        assert Query("x", entitled=True).to_params(False)["nav_ldbpermission"] == ENTITLED


class TestQueryValue:
    def test_params(self):
        q = Query("sugar", subset="exp1|x", sort="N", category="Futures")
        assert q.to_params(None) == {"q": "sugar", "subset": "exp1|x", "s": "N", "nav_category": "15"}

    def test_immutable(self):
        q = Query("x")
        with pytest.raises(AttributeError):
            q.term = "y"  # type: ignore[misc]

    def test_replace_merges_and_drops_filters(self):
        q = Query("x", source="A", market="B")
        r = q.replace(term="y", market=None, exchange="C")
        assert r.term == "y"
        assert r.filters == {"source": "A", "exchange": "C"}
        assert q.filters == {"source": "A", "market": "B"}

    def test_equality_and_hash(self):
        assert Query("x", source=["A", "B"]) == Query("x", nav_source=["A", "B"])
        assert len({Query("x"), Query("x"), Query("y")}) == 2

    def test_filters_property_decodes(self):
        q = Query(category="Futures", market=Exclude("US"), source=["A", "B"])
        assert q.filters == {"category": "15", "market": Exclude("US"), "source": ["A", "B"]}

    def test_repr(self):
        assert repr(Query("x", category="Futures")) == "Query('x', category='15')"
        assert repr(Query(entitled=None)) == "Query(entitled=None)"
        assert Query().entitled is DEFAULT

    def test_is_empty(self):
        assert Query().is_empty()
        assert not Query(source="A").is_empty()


class TestRefsAndUrls:
    def test_ref_round_trip(self):
        q = Query("sugar", category="Futures", market=Exclude("US"), entitled=True)
        assert Query.from_ref(q.to_ref()) == q

    def test_ref_is_sites_format(self):
        # the site's own reference for q=sugar
        assert Query.from_ref("cT1zdWdhcg==") == Query("sugar")
        assert base64.b64decode(Query("sugar").to_ref()) == b"q=sugar"

    def test_bad_ref(self):
        with pytest.raises(ValueError, match="not a search reference"):
            Query.from_ref("!!!")

    def test_from_url(self):
        url = (
            "https://product.datastream.com/browse/search.aspx?dsid=X&AppGroup=DSAddin"
            "&prev=expBI%23TRCDSCR5Y&subset=exp1%7C32-300%7CBI%23TRCDSSV5%7C%7CY%7C%7C%7C"
            "&nav_activity=Active&nav_source=A%7CB&page=3"
        )
        q = Query.from_url(url)
        assert q.subset == "exp1|32-300|BI#TRCDSSV5||Y|||"
        assert q.filters == {"activity": "Active", "source": ["A", "B"]}

    def test_from_url_query_string_and_entitlement(self):
        q = Query.from_url("?q=gold&nav_ldbpermission=NotEntitled&s=N")
        assert (q.term, q.entitled, q.sort) == ("gold", False, "N")
        # the site reads anything but NotEntitled as Entitled; a blank is no filter
        assert Query.from_url("?q=x&nav_ldbpermission=entitled").entitled is True
        assert Query.from_url("?q=x&nav_ldbpermission=").entitled is DEFAULT

    def test_from_url_keeps_site_encoding(self):
        q = Query.from_url("?nav_category=15%7C14&nav_market=-US%7CJP")
        assert q.to_params(None) == {"nav_category": "15|14", "nav_market": "-US|JP"}

    def test_ref_with_plus_survives_a_url(self):
        ref = Query("a>b?c~~").to_ref()
        assert "+" in ref
        assert Query.from_url(f"?searchref={ref}") == Query("a>b?c~~")  # unescaped + reads as space

    def test_pickle_and_copy(self):
        import copy
        import pickle

        q = Query("x", category="Futures", market=Exclude("US"), entitled=None)
        assert pickle.loads(pickle.dumps(q)) == q
        assert copy.deepcopy(q) == q and copy.copy(q) == q
        assert pickle.loads(pickle.dumps(Query("y"))).entitled is DEFAULT

    def test_from_url_splices_searchref(self):
        ref = Query("sugar", category="Futures").to_ref()
        assert Query.from_url(f"search.aspx?searchref={ref}") == Query("sugar", category="Futures")

    def test_from_url_warns_on_unknown(self):
        with pytest.warns(UserWarning, match="mystery"):
            Query.from_url("?q=x&mystery=1")


class TestCriteria:
    def test_any(self):
        assert criteria("ISIN", ["GB00BH4HKS39", "US0378331005"]) == "ISIN:or(GB00BH4HKS39,US0378331005)"

    def test_single_value_and_case(self):
        assert criteria("mnem", "VOD*") == "MNEM:or(VOD*)"

    def test_all_none_and_comma_escape(self):
        assert criteria("DESC", ["a,b", '"c"'], match="all") == "DESC:and(a%2Cb,c)"
        assert criteria("DESC", "x", match="none") == "DESC:not(x)"

    def test_rejects_unknown(self):
        with pytest.raises(ValueError):
            criteria("TICKER", "x")
        with pytest.raises(ValueError):
            criteria("MNEM", "x", match="some")
        with pytest.raises(ValueError):
            criteria("MNEM", [])
