"""The client: one method per thing the Navigator site can do."""
from __future__ import annotations

import json
import os
import re
import warnings
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from . import _parsers as parse
from ._session import HOST_FOR_HIT_DATA, Session
from .constants import (
    BULK_CAP,
    DATATYPE_CATEGORIES,
    DETAILS_BATCH_SIZE,
    PAGE_SIZE,
    SHOW_ALL_CAP,
)
from .errors import (
    DatastreamWebError,
    ResultSetTooLargeError,
    ServerError,
    TruncatedResultsWarning,
)
from .models import (
    Category,
    Datatype,
    DatatypeDefinition,
    FilterOption,
    Note,
    ReleaseDate,
    SavedList,
    SearchHit,
    SearchPage,
    Series,
    SeriesDetails,
    Snapshot,
    TreeNode,
)
from .query import Query, QueryLike, as_query, criteria, encode_ref, resolve_category

if TYPE_CHECKING:
    import pandas as pd

T = TypeVar("T")
R = TypeVar("R")

# page= values with special meaning to search.aspx
ALL = -1  # "Show all": every hit (up to SHOW_ALL_CAP) on one page
_SNAPSHOT_PAGE = -2  # the values preview grid

DEFAULT_MAX_WORKERS = 4
DEFAULT_TIMEOUT = 30.0

# how many symbols or ids to send per resolve/hit-data request
_ID_BATCH_SIZE = 500


class DatastreamWebClient:
    """A signed-in session on the Datastream Navigator (product.datastream.com/browse).

        with DatastreamWebClient("USER", "PASSWORD") as ds:
            ds.search("uk cpi", category="Economics").hits
            ds.lookup("VOD")
            ds.details("173737703")

    Credentials default to the DS_WEB_USERNAME / DS_WEB_PASSWORD environment variables.
    Sign-in happens on the first request and is renewed transparently if the session
    expires. The client is safe to share between threads.

    entitled_only: restrict every search to series this login is licensed to pull data
        for (the site's "LDB permission" filter). Override per query with
        Query(entitled=...). Lookups by symbol (resolve/lookup/series) are never filtered.
    max_workers: requests in flight for the methods that fan out (details_many, tree).
    timeout: seconds per request (or a (connect, read) pair).
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        entitled_only: bool = True,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: float | tuple[float, float] | None = DEFAULT_TIMEOUT,
    ):
        username = username or os.environ.get("DS_WEB_USERNAME")
        password = password or os.environ.get("DS_WEB_PASSWORD")
        if not username or not password:
            raise ValueError(
                "pass username and password, or set DS_WEB_USERNAME and DS_WEB_PASSWORD"
            )
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.entitled_only = entitled_only
        self.max_workers = max_workers
        self._session = Session(username, password, timeout=timeout, pool_size=max_workers)

    @property
    def username(self) -> str:
        return self._session.username

    def __repr__(self) -> str:
        return f"DatastreamWebClient(username={self.username!r}, entitled_only={self.entitled_only})"

    def __enter__(self) -> DatastreamWebClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def login(self) -> None:
        """Sign in now rather than on the first request (e.g. to check credentials)."""
        self._session.login()

    def close(self) -> None:
        self._session.close()

    # =================================================================================
    # Searching
    # =================================================================================

    def _search_html(self, params: dict[str, str]) -> str:
        return self._session.request("GET", "search.aspx", style="page", params=params, xhr=False).text

    def _params(self, query: Query) -> dict[str, str]:
        return query.to_params(self.entitled_only)

    def search(self, query: QueryLike = None, *, page: int = 1, **kwargs: Any) -> SearchPage:
        """One page of results — the site's results grid.

            ds.search("sugar")
            ds.search("sugar", category="Futures", exchange="ICE Futures U.S.")
            ds.search(subset=node.subset)
            ds.search(Query("gold", category="Equities", sort="N"), page=ALL)

        `page` is 1-based, 15 hits each; page=ALL returns every hit on one page, up to
        2,000. Paging is not a stable partition of the results — the site reorders tied
        hits between requests, so consecutive pages overlap and miss rows. To get a whole
        result set use search_all() (or page=ALL), never a page walk.

        A sorted query is always fetched with page=ALL: the site only sorts a complete
        page.
        """
        query = as_query(query, **kwargs)
        params = self._params(query)
        if not params:
            raise TypeError("search() needs a term, a subset, at least one filter, or a mix")
        if query.sort is not None:
            if page not in (1, ALL):
                raise ValueError("a sorted search comes back as a single page; use page=ALL")
            page = ALL
        if page == 0 or page < ALL:
            raise ValueError(f"page must be >= 1, or ALL; got {page}")
        if page != 1:
            params["page"] = str(page)
        result = parse.parse_search_page(self._search_html(params), query, page)
        if page == ALL and not result.is_complete:
            warnings.warn(
                f"this query has {result.total_hits:,} hits but a single page holds at most "
                f"{SHOW_ALL_CAP:,}; use search_all() for up to {BULK_CAP:,}",
                TruncatedResultsWarning,
                stacklevel=2,
            )
        return result

    def count(self, query: QueryLike = None, **kwargs: Any) -> int:
        """How many series a query matches."""
        return self.search(query, **kwargs).total_hits

    def filters(self, query: QueryLike = None, **kwargs: Any) -> list[FilterOption]:
        """The filter values the "Add Filters" sidebar offers for a query. Apply one with
        `query.replace(**{opt.filter_name: opt.value})`."""
        return self.search(query, **kwargs).filters

    def categories(self) -> list[Category]:
        """The top-level series categories with their sizes, live.

        `count` is the site's total, which ignores entitlement — the facet reports the
        same figure with or without the filter. For an entitled count use
        `count(category=...)`."""
        return parse.parse_categories(self._search_html({}))

    def search_all(self, query: QueryLike = None, *, limit: int | None = None, **kwargs: Any) -> list[Series]:
        """Every series a query matches, with their identifiers (DS mnemonic and code,
        RIC, ISIN, SEDOL, local code, LDB, ...), in rank order.

        The site returns at most 12,000 series per query. A query with more raises
        ResultSetTooLargeError unless `limit` asks for no more than that — in which case
        you get the top `limit` by rank (or by the query's sort).
        """
        query = as_query(query, **kwargs)
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        if limit is not None and limit > BULK_CAP:
            raise ValueError(f"limit can be at most {BULK_CAP:,}, the site's cap on one result set")
        if limit == 0:
            return []
        if limit is not None and limit <= PAGE_SIZE and query.sort is None:
            # the top of the ranking is on page 1: skip pulling 12,000 rows to keep 15
            return self.series([hit.series_id for hit in self.search(query).hits[:limit]])

        params = self._params(query)
        if not params:
            raise TypeError("search_all() needs a term, a subset, at least one filter, or a mix")
        data = self._session.json("POST", "hitdata.aspx", style="mode", params=params, mode={"host": HOST_FOR_HIT_DATA})
        found = parse.parse_series_list(data)
        if len(found) >= BULK_CAP and limit is None:
            total = self.count(query)
            if total > len(found):
                raise ResultSetTooLargeError(total, BULK_CAP)
        return found[:limit] if limit is not None else found

    def search_frame(
        self,
        query: QueryLike = None,
        *,
        limit: int | None = None,
        columns: bool = True,
        details: bool = False,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """search_all() as a DataFrame (needs pandas).

        columns: also include the grid's columns for the query (exchange, currency,
            settlement date, ... — whatever the site shows for it) plus `full_name` and
            `activity`, from the site's export. One extra request.
        details: also include everything details() returns, for every row. One request
            per 200 rows.

        Where sources overlap the earlier one wins: identifiers, then grid columns, then
        details.
        """
        from .frames import frame_from_rows

        query = as_query(query, **kwargs)
        found = self.search_all(query, limit=limit)
        rows = [_series_row(s) for s in found]

        if columns and rows:
            exported = self._export_rows(query)
            by_symbol: dict[str, dict[str, Any]] = {}
            for record in exported:
                by_symbol.setdefault(record.get("symbol", ""), record)
            for row in rows:
                for key, value in by_symbol.get(row["symbol"], {}).items():
                    row.setdefault(key, value)

        if details and rows:
            info = self.details_many([row["series_id"] for row in rows])
            for row in rows:
                if row["series_id"] in info:
                    for key, value in info[row["series_id"]].to_dict().items():
                        row.setdefault(key, value)

        return frame_from_rows(rows, first=("series_id", "name", "symbol"))

    def export(self, query: QueryLike = None, *, format: str = "csv", **kwargs: Any) -> bytes:
        """The site's file export of a query's results: "csv", "xlsx" or "xls" bytes.
        Covers the whole result set up to 12,000 rows, with the grid's columns plus the
        full name and activity status."""
        if format not in ("csv", "xlsx", "xls"):
            raise ValueError(f"format must be csv, xlsx or xls, not {format!r}")
        query = as_query(query, **kwargs)
        params = {**self._params(query), "format": format}
        return self._session.request("GET", "excelexport.aspx", style="page", params=params, xhr=False).content

    def _export_rows(self, query: Query) -> list[dict[str, Any]]:
        import csv
        import io

        text = self.export(query, format="csv").decode("utf-8-sig")
        records = []
        for raw in csv.DictReader(io.StringIO(text)):
            record: dict[str, Any] = {}
            for label, value in raw.items():
                if not label:
                    continue  # the export ends every line with a trailing comma
                key = parse._unique_key(parse.snake_case(label), record)
                if value in ("-", ""):
                    record[key] = None
                elif key.endswith("date") and (day := parse.parse_dmy(value)) is not None:
                    record[key] = day
                else:
                    record[key] = value
            records.append(record)
        return records

    def snapshot(
        self,
        query: QueryLike = None,
        *,
        default_values: bool = True,
        change: str | None = None,
        percent_change: str | None = None,
        **kwargs: Any,
    ) -> Snapshot:
        """Latest values for every hit — the site's values preview grid.

            ds.snapshot(category="Equity Indices", term="FTSE 100", percent_change="1Y")

        default_values: the category's default datatypes (e.g. price index and dividend
            yield for equity indices)
        change / percent_change: add the actual / percentage change over a period,
            e.g. "1Y", "6M", "1W"

        At most 2,000 rows (warns when the query has more).
        """
        query = as_query(query, **kwargs)
        params = self._params(query)
        if not params:
            raise TypeError("snapshot() needs a term, a subset, at least one filter, or a mix")
        params.update({
            "page": str(_SNAPSHOT_PAGE),
            "exportDeafaultValueState": _bool(default_values),  # sic: the site's spelling
            "exportActualChangeState": _bool(change is not None),
            "exportActualChangeText": change or "1Y",
            "exportPercentChangeState": _bool(percent_change is not None),
            "exportPercentChangeText": percent_change or "1Y",
        })
        result = parse.parse_snapshot(self._search_html(params))
        if len(result.rows) < result.total_hits:
            warnings.warn(
                f"the values preview holds at most {SHOW_ALL_CAP:,} rows; this query has "
                f"{result.total_hits:,}",
                TruncatedResultsWarning,
                stacklevel=2,
            )
        return result

    def search_ref(self, query: QueryLike = None, **kwargs: Any) -> str:
        """The site's search reference (permalink) for a query, with this client's
        entitlement setting applied. Paste it into the site's search box, or decode it
        with Query.from_ref()."""
        return encode_ref(self._params(as_query(query, **kwargs)))

    def describe(self, query: QueryLike = None, **kwargs: Any) -> str:
        """The site's one-line description of a query, as in its Recent Searches list:
        "Text Search - Futures : sugar"."""
        ref = self.search_ref(query, **kwargs)
        html = self._session.request("POST", "recentsearches.aspx", style="mode", data={"rq": ref}).text
        anchor = parse.soup_of(html).find("a")
        if anchor is None:
            raise DatastreamWebError("the site returned no description for this query")
        return anchor.get_text(strip=True)

    # =================================================================================
    # Series by symbol or id
    # =================================================================================

    def resolve(self, symbols: Iterable[str]) -> dict[str, Series | None]:
        """Resolve symbols of any kind — DS mnemonic, DS code, RIC, ISIN, SEDOL, local
        code — to series, in bulk. Unknown symbols map to None.

            ds.resolve(["VOD", "GB00BH4HKS39", "VOD.L", "NOTREAL"])
            # {'VOD': Series(...), 'GB00BH4HKS39': Series(...), 'VOD.L': Series(...), 'NOTREAL': None}

        Matching is exact, never "best guess": a near-miss is None, not a neighbour.
        Entitlement doesn't apply — a series you can't pull data for still resolves (see
        entitlement()).
        """
        given = [s.strip() for s in symbols if s and s.strip()]
        # symbols are case-insensitive, and two spellings of one symbol in a request
        # trip the site's duplicate-series failure (see _resolve_batch), so ask once each
        unique = list(dict.fromkeys(s.upper() for s in given))
        by_upper: dict[str, Series | None] = {}
        for batch in _chunks(unique, _ID_BATCH_SIZE):
            found = self._resolve_batch(batch)
            aligned = _align(batch, found)
            # the site drops unknown symbols without saying which; when order alone can't
            # say which symbol a series answers, ask about the leftovers one at a time
            for symbol in [s for s, v in aligned.items() if v is _AMBIGUOUS]:
                single = self._resolve_batch([symbol])
                aligned[symbol] = single[0] if single else None
            by_upper.update(aligned)
        return {s: by_upper[s.upper()] for s in given}

    def _resolve_batch(self, symbols: list[str]) -> list[Series]:
        """One resolve request. The site fails the whole request (HTTP 500) when two
        symbols in it name the same series — "VOD" and "VOD.L" — so on a failure the batch
        is split in half until the clash is isolated; a lone symbol can't clash, so its
        own failure is real and propagates."""
        try:
            return self._resolve_request(symbols)
        except ServerError as exc:
            if len(symbols) == 1 or exc.status_code != 500:
                raise
        middle = len(symbols) // 2
        return self._resolve_batch(symbols[:middle]) + self._resolve_batch(symbols[middle:])

    def _resolve_request(self, symbols: list[str]) -> list[Series]:
        data = self._session.json(
            "POST",
            "resolveLegacySelections.aspx",
            style="mode",
            mode={"host": HOST_FOR_HIT_DATA},
            data={"symbols": "|".join(symbols), "selected": "|".join(["_clicked"] * len(symbols))},
        )
        return parse.parse_series_list(data)

    def lookup(self, symbol: str) -> Series | None:
        """One symbol's series, or None. See resolve()."""
        return self.resolve([symbol]).get(symbol.strip())

    def series(self, series_ids: Iterable[str]) -> list[Series]:
        """Identifiers for known series ids, in the order given (unknown ids dropped)."""
        ids = list(dict.fromkeys(str(i) for i in series_ids))
        found: dict[str, Series] = {}
        for batch in _chunks(ids, _ID_BATCH_SIZE):
            data = self._session.json(
                "POST", "hitdata.aspx", style="mode", mode={"host": HOST_FOR_HIT_DATA}, data={"ids": "|".join(batch)}
            )
            found.update((s.series_id, s) for s in parse.parse_series_list(data))
        return [found[i] for i in ids if i in found]

    def entitlement(self, symbol: str) -> bool | None:
        """Whether this login is licensed for a series' data: True, False, or None if the
        symbol doesn't resolve.

        This is the site's LDB-permission flag. True has always meant the data API serves
        the series. False matched an API "access denied" everywhere tested except a few
        Economics databases (sovereign ESG scores, Reuters polls) the API serves anyway —
        a get_data() call is the definitive check."""
        found = self.lookup(symbol)
        if found is None:
            return None
        for flag in (True, False):
            hits = self.search(found.symbol, entitled=flag, page=ALL).hits
            if any(hit.series_id == found.series_id for hit in hits):
                return flag
        return None

    def details(self, series_id: str) -> SeriesDetails:
        """The series details panel: full name, identifiers, latest value, timespan,
        market/source/currency and category-specific fields, headline datatypes, notes,
        and links to the lists, explorers, constituents and derivatives it belongs to."""
        found = self.details_many([series_id])
        if str(series_id) not in found:
            raise DatastreamWebError(f"no details returned for series {series_id!r}")
        return found[str(series_id)]

    def details_many(self, series_ids: Iterable[str]) -> dict[str, SeriesDetails]:
        """details() for many series, batched (200 per request, max_workers in flight).
        Ids the site doesn't know are left out of the result."""
        ids = list(dict.fromkeys(str(i) for i in series_ids))
        found: dict[str, SeriesDetails] = {}
        for part in self._map(self._details_batch, list(_chunks(ids, DETAILS_BATCH_SIZE))):
            found.update(part)
        return found

    def _details_batch(self, ids: list[str]) -> dict[str, SeriesDetails]:
        html = self._session.request(
            "POST",
            "searchstraggler.aspx",
            style="mode",
            params={"lazy": "false", "term": ""},
            data={"ids": ",".join(ids)},
        ).text
        return parse.parse_details(html)

    def datatypes(self, series_id: str) -> list[Datatype]:
        """Every datatype available for a series (the site's "More..." list), with the
        date each one's history starts."""
        html = self._session.request(
            "GET",
            "moredatatypes.aspx",
            style="mode",
            params={"docid": str(series_id), "diagnostics": "false", "editing": "false"},
        ).text
        return parse.parse_datatypes(html)

    def notes(self, series_id: str) -> list[Note]:
        """The full notes on a series (definitions, methodology, revisions, sources).
        Most series have none."""
        html = self._session.request("POST", "notes.aspx", style="mode", data={"ids": str(series_id)}).text
        return parse.parse_notes(html)

    def release_dates(self, series_id: str, frequency: str | None = None) -> list[ReleaseDate]:
        """Upcoming release dates of an economic series. `frequency` (its one-letter code)
        is looked up from details() when not given; a series with no release calendar
        returns []."""
        if frequency is None:
            frequency = self.details(series_id).release_frequency
            if frequency is None:
                return []
        html = self._session.request(
            "POST", "ndor.aspx", style="mode", data={"id": str(series_id), "frequency": frequency}
        ).text
        return parse.parse_release_dates(html)

    def chart(
        self,
        series: str | SeriesDetails,
        *,
        datatype: str | None = None,
        latest_only: bool = False,
    ) -> bytes:
        """The site's chart thumbnail of a series as PNG bytes.

        `datatype` picks what to plot (SeriesDetails.chart_datatypes lists the quick
        choices; defaults to the series' own); `latest_only` zooms to recent history.
        Pass a SeriesDetails to save the details request."""
        info = series if isinstance(series, SeriesDetails) else self.details(series)
        if not info.chart:
            raise DatastreamWebError(f"series {info.series_id} has no chart")
        params = {k: _bool(v) if isinstance(v, bool) else str(v) for k, v in info.chart.items()}
        if datatype is not None:
            params["datatype"] = datatype
        params["recent"] = _bool(latest_only)
        response = self._session.request("GET", "thumbnail.aspx", style="mode", params=params, xhr=False)
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise ServerError(f"thumbnail.aspx returned {response.headers.get('Content-Type')!r}, not an image")
        return response.content

    def constituents(self, symbol: str, *, identifiers: bool = True) -> list[Series]:
        """The members of a list or index: constituents("LFTSE100"). A symbol that isn't
        a list returns just itself.

        identifiers: fetch full identifiers for each member (one more request); otherwise
        each Series has just id, name and symbol."""
        try:
            data = self._session.json(
                "POST",
                "expandmnemonics.aspx",
                style="progress",
                data={"current": "", "mnem": symbol, "allornothing": "false"},
            )
        except ServerError as exc:
            raise ServerError(
                f"no list or series {symbol!r} to expand", exc.status_code, exc.detail
            ) from exc
        if data.get("isError"):
            raise DatastreamWebError(data.get("message") or f"could not expand {symbol!r}")
        if data.get("error"):
            warnings.warn(f"constituents({symbol!r}): {data['error']}", stacklevel=2)
        members = parse.parse_series_list(data.get("constituents", []))
        if not identifiers:
            return members
        full = {s.series_id: s for s in self.series([m.series_id for m in members])}
        return [full.get(m.series_id, m) for m in members]

    # =================================================================================
    # Datatypes
    # =================================================================================

    def search_datatypes(
        self,
        term: str | None = None,
        *,
        category: str = "All Datatypes",
        page: int = 1,
    ) -> SearchPage:
        """Search datatypes rather than series — the site's "Datatype Search".

            ds.search_datatypes("dividend yield", category="Equities")

        `category` is a DATATYPE_CATEGORIES name (or its subset string). Hits carry the
        datatype's name and mnemonic, plus type/source/currency fields."""
        subset = DATATYPE_CATEGORIES.get(category, category)
        params = {"dt": "true", "subset": subset, "prev": subset}
        if term is not None:
            params["q"] = term
        if page != 1:
            params["page"] = str(page)
        return parse.parse_search_page(self._search_html(params), Query(term, subset=subset), page)

    def lookup_datatypes(self, mnemonics: Iterable[str], *, category: str = "All Datatypes") -> list[SearchHit]:
        """Datatypes by mnemonic: lookup_datatypes(["PI", "DY", "MV"]). A mnemonic used in
        several categories comes back once per category (`fields["datatype_category"]`)."""
        names = [m.strip() for m in mnemonics if m and m.strip()]
        if not names:
            return []
        if len(names) > 500:
            raise ValueError("at most 500 datatype mnemonics per lookup")
        return self.search_datatypes(criteria("MNEM", names), category=category, page=ALL).hits

    def datatype_definition(self, mnemonic: str, category: str | int) -> DatatypeDefinition:
        """The site's help text for a datatype, which depends on the series category:
        datatype_definition("DY", "Equity Indices")."""
        category_id = resolve_category(category)
        html = self._session.request(
            "POST", "datatypedefinition.aspx", style="mode", data={"cat": category_id, "mnem": mnemonic}
        ).text
        return parse.parse_datatype_definition(html, mnemonic, category_id)

    def datatype_categories(self) -> dict[str, str]:
        """The datatype categories, live: {name: subset}. (DATATYPE_CATEGORIES is a
        snapshot of this.)"""
        return parse.parse_datatype_categories(self._search_html({"dt": "true"}))

    # =================================================================================
    # The browse tree
    # =================================================================================

    def _tree_raw(self, nid: str) -> list[dict[str, Any]]:
        params = {"nid": nid, "dt": "", "str": ""}
        if nid != "#":
            # the root call must not send ops: with it the site returns an empty list
            params["ops"] = "getidswithdata"
        return parse.tree_children(self._session.json("GET", "explorerleaves.aspx", style="none", params=params), nid)

    def tree(self, nid: str = "#", depth: int | None = 1) -> list[TreeNode]:
        """Walk the browse tree below node `nid` ('#' is the root).

        depth: levels below `nid` to fetch — 1 is its children, None the whole subtree
            (one request per branch node, max_workers in flight; don't do that from the
            root, which bottoms out in millions of series).

        Nodes with a `subset` hold series: ds.search(subset=node.subset).
        """
        if depth is not None and depth < 1:
            raise ValueError(f"depth must be >= 1 or None, got {depth}")
        nodes: list[TreeNode] = []
        seen = {nid}
        frontier: list[tuple[str, tuple[str, ...]]] = [(nid, ())]
        level = 1
        while frontier and (depth is None or level <= depth):
            batches = self._map(lambda item: self._tree_raw(item[0]), frontier)
            next_frontier = []
            for (parent_id, parent_path), children in zip(frontier, batches):
                for raw in children:
                    node = parse.tree_node(raw, None if parent_id == "#" else parent_id, level, parent_path)
                    nodes.append(node)
                    # a node can appear under two parents; without this depth=None loops
                    if node.has_children and node.id and node.id not in seen:
                        seen.add(node.id)
                        next_frontier.append((node.id, parent_path + (node.text,)))
            frontier = next_frontier
            level += 1
        return nodes

    def tree_search(self, text: str) -> list[TreeNode]:
        """Find the first tree node whose name matches `text`; returns the path to it,
        root first, ending with the match. [] when nothing matches."""
        data = self._session.json(
            "GET",
            "explorerleaves.aspx",
            style="none",
            params={"ops": "getidswithdataforfirstsearchresult", "nid": "", "dt": "", "str": text},
        )
        return parse.tree_path(data, self._tree_raw("#")) if data else []

    def combine_explorers(self, node_ids: Sequence[str]) -> str:
        """A subset covering several tree nodes at once (the site's multi-explorer
        selection): ds.search(subset=ds.combine_explorers(["12-4416", "8-563"]))."""
        if not node_ids:
            raise ValueError("combine_explorers() needs at least one node id")
        text = self._session.request(
            "GET", "multiexplorerencoding.aspx", style="none", params={"nids": "|".join(node_ids)}
        ).text.strip()
        return json.loads(text) if text.startswith('"') else text

    # =================================================================================
    # User data (changes your account)
    # =================================================================================

    def save_list(
        self,
        mnemonic: str,
        description: str,
        symbols: Iterable[str | Series],
        *,
        overwrite: bool = False,
    ) -> SavedList:
        """Save series as a Datastream user list (L#...), usable anywhere Datastream takes
        a list mnemonic. The site's "My Selections → Save".

        mnemonic: the list's name; normalized as the site does — upper-cased, "L#"
            prefixed, at most 8 characters in all.
        symbols: anything resolve() accepts, or Series. Unresolvable symbols raise.
        overwrite: replace an existing list of that name; otherwise that's an error.
        """
        name = re.sub(r"^(L#|TR#)*", "", mnemonic.strip(), flags=re.I)
        list_mnemonic = ("L#" + name.upper())[:8]
        if len(list_mnemonic) <= 2:
            raise ValueError("the list mnemonic is empty")

        members: list[Series] = []
        to_resolve = [s for s in symbols]
        lookups = self.resolve([s for s in to_resolve if isinstance(s, str)])
        unknown = [s for s in to_resolve if isinstance(s, str) and lookups.get(s.strip()) is None]
        if unknown:
            raise ValueError(f"cannot resolve: {', '.join(unknown)}")
        for item in to_resolve:
            members.append(item if isinstance(item, Series) else lookups[item.strip()])  # type: ignore[arg-type]
        if not members:
            raise ValueError("a list cannot be empty")

        args = {
            "command": "newucl",
            "desc": description,
            "mnem": list_mnemonic,
            "type": "D",
            "ids": "|".join(m.series_id for m in members),
            "symbols": "|".join(m.symbol for m in members),
        }
        result = self._session.json("POST", "usercreateddata.aspx", style="progress", data=args)
        if result.get("overwrite"):
            if not overwrite:
                raise DatastreamWebError(f"{result.get('message', 'list exists')} (pass overwrite=True to replace it)")
            result = self._session.json(
                "POST", "usercreateddata.aspx", style="progress", data={**args, "command": "saveucl"}
            )
        if result.get("isError"):
            raise DatastreamWebError(result.get("message") or "saving the list failed")
        return SavedList(
            mnemonic=list_mnemonic,
            message=result.get("message", ""),
            details=result.get("lstdetails") or {},
        )

    def refresh_user_data(self) -> str:
        """Resynchronise your user-created lists and series with the search index (the
        site's "Synchronise User Data"). Returns the site's message."""
        result = self._session.json("POST", "usercreateddata.aspx", style="progress", data={"command": "refresh"})
        if result.get("isError"):
            raise DatastreamWebError(result.get("message") or "synchronising user data failed")
        return result.get("message", "")

    # =================================================================================

    def _map(self, func: Callable[[T], R], items: list[T]) -> list[R]:
        """`func` over `items` with up to max_workers in flight, results in order."""
        if len(items) <= 1 or self.max_workers == 1:
            return [func(item) for item in items]
        self._session.ensure_login()  # once, before the workers race to do it
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(items))) as pool:
            return list(pool.map(func, items))


# --- helpers -------------------------------------------------------------------------

_AMBIGUOUS: Any = object()

# Series attributes a resolved symbol may have matched on
_SYMBOL_ATTRS = ("symbol", "ds_mnemonic", "ds_code", "ric", "isin", "sedol", "local_code", "t1_code")


def _matches(symbol: str, series: Series) -> bool:
    wanted = symbol.upper()
    return any((getattr(series, attr) or "").upper() == wanted for attr in _SYMBOL_ATTRS)


def _align(symbols: list[str], found: list[Series]) -> dict[str, Any]:
    """Pair each requested symbol with the series it resolved to.

    The site answers in request order but silently drops unknowns. So walk both lists:
    a series that carries the symbol among its identifiers is a match; when the counts
    agree every pair is a match; otherwise a symbol that doesn't match the next series
    is either unknown or resolved through an identifier the site didn't return —
    marked _AMBIGUOUS for the caller to check on its own."""
    if len(found) == len(symbols):
        return dict(zip(symbols, found))
    result: dict[str, Any] = {}
    i = 0
    for symbol in symbols:
        if i < len(found) and _matches(symbol, found[i]):
            result[symbol] = found[i]
            i += 1
        else:
            result[symbol] = _AMBIGUOUS
    return result


def _chunks(items: Sequence[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _series_row(series: Series) -> dict[str, Any]:
    row: dict[str, Any] = {
        "series_id": series.series_id,
        "name": series.name,
        "symbol": series.symbol,
        "category_id": series.category_id,
        "category_name": series.category_name,
        "ds_mnemonic": series.ds_mnemonic,
        "ds_code": series.ds_code,
        "ric": series.ric,
        "isin": series.isin,
        "sedol": series.sedol,
        "local_code": series.local_code,
        "t1_code": series.t1_code,
        "ldb": series.ldb,
        "base_date": series.base_date,
    }
    for key, value in series.extra.items():
        row.setdefault(key, value)
    return row

