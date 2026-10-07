"""
Unofficial client for the Datastream Navigator web app's search/browse
functionality, which is not exposed by the official Datastream Python SDK.

Reverse-engineered from a HAR capture of https://product.datastream.com/browse/.
It's a server-rendered ASP.NET WebForms app, not a JSON API:
  - search results are an HTML table embedded in the search.aspx page itself
  - the category browse tree is a separate JSON endpoint (explorerleaves.aspx)
  - hovering/pinning a hit fetches an HTML preview fragment (searchstraggler.aspx)

Endpoints used:
  POST /dsws/1.0/DSLogon.aspx   - form login (ASP.NET WebForms, needs viewstate)
  GET  /browse/search.aspx      - search (q=) and/or browse filters (nav_category=/nav_source=/...),
                                   results embedded in the returned HTML page
  GET  /browse/explorerleaves.aspx?nid=&ops=getids|getidswithdata - browse-tree children (JSON)
  POST /browse/searchstraggler.aspx?...&dsid=  body: ids=<seriesId> - hit preview (HTML fragment)
"""
from __future__ import annotations

import json
import math
import re
import warnings
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://product.datastream.com"
LOGIN_URL = f"{BASE}/dsws/1.0/DSLogon.aspx"
SEARCH_URL = f"{BASE}/browse/search.aspx"
EXPLORER_LEAVES_URL = f"{BASE}/browse/explorerleaves.aspx"
SEARCHSTRAGGLER_URL = f"{BASE}/browse/searchstraggler.aspx"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}

# how many result pages/previews to fetch concurrently by default
DEFAULT_MAX_WORKERS = 3

# seconds allowed for each request, applied to both the connect and the read phase.
# requests defaults to *no* timeout, which on a hung server hangs the caller forever —
# and a ThreadPoolExecutor of hung workers doesn't even interrupt on Ctrl-C. Raise it via
# DatastreamWebClient(timeout=...) if you pull large page=-1 result sets over a slow link.
DEFAULT_TIMEOUT = 30

# the results grid pages at 15 hits, so the default DataFrame size is one full
# parallel round: DEFAULT_MAX_WORKERS pages, fetched in one go
PAGE_SIZE = 15
DEFAULT_HITS = DEFAULT_MAX_WORKERS * PAGE_SIZE

# `n` left alone, as distinct from n=DEFAULT_HITS asked for deliberately. The default is a
# cost cap (previews are one request per hit), not a claim about the result set, so when it
# throws away rows that are already in hand the caller should hear about it — but an
# explicit n is a considered choice and warning about it would just be noise.
_N_DEFAULT: Any = object()

# page=-1 is the site's "Show all": one request for the whole result set, no pager.
# It exists because paging is *not* a stable partition of the results — hits are ranked by
# relevance with no tiebreak, so a block of equally-scoring series is ordered differently on
# every request and the page boundaries slide through it. Walking pages 1..N therefore both
# repeats rows and skips others (measured on q=sugar + NYMEX: 101 hits, a serial 7-page walk
# returned 84 distinct — 17 duplicated, 17 never seen — while page=-1 returned all 101 exactly
# once). Not a concurrency artifact: serial paging is just as lossy. Prefer page=-1 whenever
# the caller wants the complete set.
SHOW_ALL_PAGE = -1

# ...but only up to a point: page=-1 on a six-figure result set asks the server to render
# every row into one HTML page. Above this we page and accept the duplication, warning
# rather than pretending the walk is exhaustive.
SHOW_ALL_MAX_HITS = 5000

# The filter inputs in the page's <fieldset id="inputs-query">. The site serializes
# that whole fieldset (text query included) into a single search.aspx GET, so a text
# search and browse filters combine — which is what the "Add Filters" sidebar does.
#
# The bracketed ids are which categories offer each filter, measured by calling
# category_filters() on all fifteen of CATEGORIES — not inferred, and no longer limited to
# the eight the browse tree exposes. Still a typo-catcher rather than a contract: the
# sidebar's offer also narrows as you add filters, so use category_filters() for the live
# set. A filter a category doesn't offer is simply ignored by the site rather than erroring.
NAV_FILTERS = (
    "category",           # top-level category, by name or id — CATEGORIES / categories()
                          #                                    [all]
    "activity",           # "Active" for the active-series-only toggle
                          #                                    [0,1,3,4,5,6,8,9,11,13,14,15,32]
    "currency",           #                                    [0,3,4,5,6,7,8,9,11,13,15,32]
    "startyear",          # base date, matched as equals-or-before
                          #                                    [0,3,4,5,6,7,8,9,11,12,13,32]
    "market",             #                                    [0,1,3,4,5,8,9,11,12,13,32]
    "type",               #                                    [0,1,6,9,11,12,13,14,15,32]
    "frequency",          #                                    [3,4,6,7,8,9,12,32]
    "hasric",             #                                    [0,3,4,5,11,12,13,14]
    "source",             #                                    [1,3,6,7,8,9,12,32]
    "exchange",           #                                    [0,5,11,13,14,15]
    "datatype",           #                                    [3,6,7,8,9]
    "medrank",            #                                    [0,5,7,8,12]
    "endyear",            #                                    [12,13,14,15]
    "highrank",           #                                    [0,5,6,12]
    "borrower",           #                                    [11,13]
    "coupon",             #                                    [13,14]
    "coupontype",         #                                    [11,13]
    "underlying",         #                                    [14,15]
    "unit",               #                                    [6,12]
    "accesstype",         #                                    [13]
    "adjustment",         #                                    [12]
    "assettype",          #                                    [4]
    "econactivity",       #                                    [12]
    "forecast",           #                                    [12]
    "fromcurrency",       #                                    [7]
    "incomedistribution", #                                    [4]
    "industrygroup",      #                                    [4]
    "issuertype",         #                                    [13]
    "ldb",                #                                    [12]
    "ldbpermission",      #                                    [12]
    "localcode",          #                                    [12]
    "priceunit",          #                                    [13]
    "region",             #                                    [14]
    "restriction",        #                                    [6]
    "sector",             #                                    [0]
)


# columns of the browse_tree_children() DataFrame, fixed so an empty walk still has a schema
TREE_COLUMNS = ("id", "text", "parent", "depth", "type", "has_children", "subset_ref", "path")

# the columns every search_dataframe()/browse_dataframe() frame has, whatever the grid's
# dynamic ones turn out to be — SearchHit's promoted attributes. Same purpose as
# TREE_COLUMNS: a zero-hit query returns an empty frame that still has a schema, so
# `df.mnemonic.tolist()` on a query that matched nothing gives [] instead of raising
# AttributeError on a (0, 0) frame.
RESULT_COLUMNS = ("series_id", "name", "mnemonic")

# columns of the category_filters() DataFrame. "filter_name" rather than "filter" because
# a column called filter shadows DataFrame.filter() under attribute access
FILTER_COLUMNS = ("filter_name", "param", "filter_label", "value", "value_label", "count", "applied")


# The two values of the nav_ldbpermission filter — the site's own record of what this
# login may actually pull. Datastream licenses data per LDB ("local database", the 3-char
# code shown as LDB in the preview panel and called "the database code" in the site's
# refusal message), and every series carries the LDB it lives in, so the flag is really a
# property of the database, not of the series.
#
# Matched EXACTLY and case-sensitively by the server: "NotEntitled" selects the blocked
# half, and *every other string* — "notentitled", "Not Entitled", a typo — behaves like
# "Entitled". That is the opposite of the usual invented-filter-value failure mode (see
# the module note): a misspelling here silently filters instead of silently no-opping, so
# always use these constants rather than a literal.
ENTITLED = "Entitled"
NOT_ENTITLED = "NotEntitled"

# Applied by default to every search.aspx request this client makes (see
# DatastreamWebClient(entitled_only=...)), so ordinary searching only ever surfaces series
# whose data this login can actually fetch. Measured on YOURDSID: it hides ~71k of the 91k
# Bond Indices, ~227k of the 400k Equity Indices and 514k Economics series, and nothing at
# all in Equities, Funds, Futures, Options, Warrants, Bonds & Convertibles, Constituent
# Lists, Investment Trusts or CDS.
#
# Both halves were validated against DatastreamPy, which refuses a series with
# "$$ER: E100,ACCESS DENIED" — three series in each of the largest hidden databases, ~90%
# of the hidden set. Entitled is safe: 17/17 pullable. NotEntitled is right everywhere
# except four Economics databases the API serves quite happily — LSEG Sovereign
# Sustainability Solutions (BRE 82,193 country ESG scores and SDG aggregates, BRC 13,203
# carbon budgets, BRS 9,286 ESG pillar CAGRs) and Reuters poll consensus forecasts
# (TRP 28,920). Outside those, what the default hides the API refuses anyway.
#
# Do NOT use the site's chart as an access check. thumbnail.aspx renders real charts for
# D01-D05/IKR/IKB/IKF (14,227 bond indices) and WDF/WDR/WDN (2,793 exchange rates,
# USDOLLR among them) that DatastreamPy denies — the Navigator's viewing licence is wider
# than the feed's. The authoritative check is a get_data() call, which batches, so it
# belongs over a shortlist rather than per hit.
DEFAULT_ENTITLED_ONLY = True


# nav_category is addressed by numeric id, but every id has a name — the sidebar's
# "Category" list, which is what a user actually reads. Scraped from the unfiltered
# search page's `data-filterid="nav_category" data-filtervalue="<id>"` links, so this is
# the site's own vocabulary, not a guess. Note that the browse tree only exposes 8 of
# these 15 (browse_tree_children() has no Funds/Futures/Options/... root), so the tree is
# not a substitute source. Ids 2 and 10 are unused. client.categories() refetches this
# live if you need to check it still holds.
CATEGORIES = {
    "Equities": "0",
    "Constituent Lists": "1",
    "Equity Indices": "3",
    "Funds": "4",
    "Investment Trusts": "5",
    "Commodities": "6",
    "Exchange Rates": "7",
    "Interest Rates": "8",
    "Bond Indices": "9",
    "Warrants": "11",
    "Economics": "12",
    "Bonds & Convertibles": "13",
    "Options": "14",
    "Futures": "15",
    "Credit Default Swaps": "32",
}


def _normalize_category_name(name: str) -> str:
    """Fold a category name to a match key, so "Bonds & Convertibles", "bonds and
    convertibles" and "Bonds&Convertibles" all land on the same entry."""
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


_CATEGORIES_BY_KEY = {_normalize_category_name(k): v for k, v in CATEGORIES.items()}


def _top_level_category(value: Any) -> str:
    """Resolve one category= value to the numeric id nav_category wants.

    Accepts a name as shown in the sidebar ("Futures", "Bonds & Convertibles"), matched
    case- and punctuation-insensitively, or the id itself ("15").

    nav_category addresses a *top-level* category, so the site silently reduces a
    browse-tree node id to its prefix ("12-4428" and "12" return identical result sets).
    Do the reduction here instead, loudly, so a drill-down that isn't really happening
    can't pass for one."""
    text = str(value).strip()

    if not re.fullmatch(r"\d+(-\d+)*", text):
        resolved = _CATEGORIES_BY_KEY.get(_normalize_category_name(text))
        if resolved is None:
            raise ValueError(
                f"unknown category {value!r}. Pass a numeric id or one of: "
                f"{', '.join(sorted(CATEGORIES))}. (client.categories() lists them live.)"
            )
        return resolved

    prefix = text.split("-")[0]
    if prefix != text:
        warnings.warn(
            f"category={text!r} is a browse-tree node id, but nav_category only addresses "
            f"top-level categories — this searches the whole of category {prefix!r}, not "
            f"that node. To search inside a tree node, pass its subset_ref as subset=.",
            stacklevel=4,
        )
    return prefix


def _nav_params(filters: dict[str, Any]) -> dict[str, str]:
    """Normalize filter kwargs into search.aspx's nav_* query params. Keys may be given
    with or without the prefix (`source=` or `nav_source=`); None values are dropped, and
    a list/tuple is joined with "|", which is how the sidebar sends a multi-value filter
    (e.g. nav_source="Bank of New York|BEC - Chile Electronic Exchange").

    A bare name is checked against NAV_FILTERS to catch typos. An explicitly nav_-prefixed
    name is passed through unchecked, so a filter this module hasn't catalogued (the
    sidebar's offer differs per category, and category_filters() can surface names not in
    NAV_FILTERS) is still reachable without editing the whitelist."""
    params: dict[str, str] = {}
    for key, value in filters.items():
        explicit = key.startswith("nav_")
        name = key.removeprefix("nav_")
        if name not in NAV_FILTERS and not explicit:
            raise TypeError(
                f"unknown filter {key!r}; expected one of {', '.join(NAV_FILTERS)}. "
                f"Pass it as nav_{name}= to send it anyway (category_filters() lists the "
                f"filters a given category really offers)."
            )
        if value is None:
            continue
        # resolve per element, not on the joined string, so category=["Futures", "Options"]
        # works — the sidebar's Category list is multi-select ("Multiple") like the rest
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if name == "category":
            values = [_top_level_category(v) for v in values]
        params[f"nav_{name}"] = "|".join(str(v) for v in values)
    return params


def parse_search_url(url: str) -> dict[str, Any]:
    """Parse a Navigator search.aspx URL — or just its query string — copied out of a
    browser into kwargs for search() / search_dataframe() / browse().

    Accepts either shape, with or without a leading "?", and decodes percent-encoding
    (%7C -> "|", %23 -> "#") the way every subset_ref and multi-value filter needs:

        url = ("https://product.datastream.com/browse/search.aspx?dsid=YOURDSID"
               "&AppGroup=DSAddin&prev=expBI%23TRCDSCR5Y"
               "&subset=exp1%7C32-300%7CBI%23TRCDSSV5%7C%7CY%7C%7C%7C&nav_activity=Active")
        kwargs = parse_search_url(url)
        # {"subset": "exp1|32-300|BI#TRCDSSV5||Y|||", "activity": "Active"}
        client.search(**kwargs)
        client.search_dataframe(**kwargs, n=200, include_preview=False)

    `dsid`, `AppGroup` and `prev` are session/navigation bookkeeping (the "expanded from"
    breadcrumb), not search parameters, and are dropped. `q` becomes `term`; `subset` and
    `page` pass straight through; `nav_<name>` becomes a bare filter kwarg `<name>=`,
    split back into a list on "|" for a multi-value filter (the reverse of what
    _nav_params does when building the URL). `page` is parsed as int when it looks like
    one, else left as the raw string so a malformed URL fails inside search() rather than
    silently here. An unrecognised param is dropped with a warning rather than raising,
    since a URL may carry site bookkeeping this module doesn't know about.
    """
    query = urlsplit(url).query or url.lstrip("?")
    parsed: dict[str, Any] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in ("dsid", "AppGroup", "prev"):
            continue
        if key == "q":
            parsed["term"] = value
        elif key == "subset":
            parsed["subset"] = value
        elif key == "page":
            parsed["page"] = int(value) if re.fullmatch(r"-?\d+", value) else value
        elif key.startswith("nav_"):
            name = key.removeprefix("nav_")
            values = value.split("|")
            parsed[name] = values if len(values) > 1 else values[0]
        else:
            warnings.warn(f"parse_search_url: ignoring unrecognised query param {key!r}={value!r}")
    return parsed


# The CFTC trader categories, taken from the browse tree's own leaves under 6-42 ("COT")
# rather than invented: {code: canonical name}. The code is the one that appears in the
# mnemonic (CFCNCSI -> NC) and in the tree's subset refs (CFTC-NC).
COT_TRADER_CATEGORIES = {
    "AM": "Asset Manager / Institutional",
    "CM": "Commercial",
    "DI": "Dealer Intermediary",
    "IT": "Index Traders",
    "LF": "Leveraged Funds",
    "MM": "Managed Money",
    "NC": "Non-Commercial",
    "NR": "Non-Reportable",
    "OR": "Other Reportable",
    "PM": "Producer / Merchant / Processor / User",
    "SW": "Swap Dealer",
    "TOT": "Total",
    "TR": "Total Reportable",
}

# zero-width characters that turn up mid-word in the source data — "Proces​sor" in
# Producer/Merchant/Processor/User — and would otherwise defeat any match on that word
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))

# "<Exchange>(<CODE>)-<the rest>". The parenthesised code is optional: most names carry it
# ("Chicago Mercantile Exchange(CME)-...") but some are bare ("CCFE-Sulfur Finland ...").
_COT_HEAD_RE = re.compile(r"\s*([^-(]+?)\s*(?:\(([^)]+)\))?\s*-\s*(.*)", re.DOTALL)

# boilerplate the "Total" report wedges between the asset and the category word:
# "...-Class III Milk Commodity futures Trading Commission(CFTC) Commitments of
# Traders(COT) Total". Lowercase "futures" is the site's own, hence the re.I.
_COT_BOILERPLATE_RE = re.compile(
    r"Commodity\s+futures\s+Trading\s+Commission\s*\([^)]*\)\s*"
    r"Commitments\s+of\s+Traders\s*\([^)]*\)",
    re.I,
)

_COT_POSITION_RE = re.compile(r"\b(Long|Short|Spreading|Spread)\b", re.I)
# the names the site never expanded ("Soyabean Oil NR LG IT") code the position too.
# Case-sensitive, like the category codes, since two letters collide too easily otherwise.
_COT_POSITION_CODES = {"LG": "Long", "SH": "Short", "SP": "Spreading"}
_COT_POSITION_CODE_RE = re.compile(rf"\b({'|'.join(_COT_POSITION_CODES)})\b")
_COT_FUTURES_ONLY_RE = re.compile(r"\bfutures\s+only\b", re.I)


def _cot_category_patterns() -> list[tuple[str, str, re.Pattern[str]]]:
    """(code, canonical name, regex) per trader category, matching the long form with
    flexible separators: the tree says "Dealer Intermediary" while the name says
    "Dealer / Intermediary", and "Producer / Merchant / Processor / User" also appears
    unspaced."""
    patterns = []
    for code, name in COT_TRADER_CATEGORIES.items():
        words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
        body = r"[\s/,\-]*".join(map(re.escape, words))
        patterns.append((code, name, re.compile(rf"\b{body}\b", re.I)))
    return patterns


_COT_CATEGORY_PATTERNS = _cot_category_patterns()
# the abbreviated fallback, for names the site never expanded ("Soyabean Oil NR LG IT").
# Case-sensitive and word-bounded on purpose: these codes are short enough that a
# case-insensitive match would collide with ordinary words in an asset name.
_COT_CODE_RE = re.compile(rf"\b({'|'.join(COT_TRADER_CATEGORIES)})\b")


@dataclass
class CotName:
    """The parts of a Commitments of Traders series name — see parse_cot_name()."""

    exchange: str | None
    exchange_code: str | None
    asset: str | None
    trader_category: str | None
    trader_category_code: str | None
    position: str | None
    futures_only: bool


def _cot_code_from_mnemonic(mnemonic: str | None) -> str | None:
    """The trader-category code a COT mnemonic carries: CFCNCSI -> NC, CNGDIXC -> DI,
    CBMTOTC -> TOT. The layout is "C" + a two-character asset code + the category code,
    which is two characters except for TOT."""
    if not mnemonic or len(mnemonic) < 5:
        return None
    text = mnemonic.strip().upper()
    if text[3:6] == "TOT":
        return "TOT"
    return text[3:5] if text[3:5] in COT_TRADER_CATEGORIES else None


def parse_cot_name(full_name: str | None, mnemonic: str | None = None) -> CotName | None:
    """
    Split a COT series' `preview()["full_name"]` into exchange / asset / trader category,
    or None if it isn't a COT series name.

        parse_cot_name("Chicago Mercantile Exchange(CME)-Feeder Cattle "
                       "Non-Commercial Short Index")
        # CotName(exchange='Chicago Mercantile Exchange', exchange_code='CME',
        #         asset='Feeder Cattle', trader_category='Non-Commercial',
        #         trader_category_code='NC', position='Short', futures_only=False)

    Pass `mnemonic` when you have it. It carries the trader-category code (CFCNCSI -> NC)
    and settles the one ambiguous shape, where the asset's last word and the category's
    first word spell a third category between them — "5YR Interest Rate Swap Dealer /
    Intermediary" (CNGDIXC) otherwise reads as "Swap Dealer" and the asset loses its
    "Swap". The client methods pass it for you.

    This is the only way to recover the underlying asset. Datastream records it nowhere
    structured: the browse tree groups COT by *trader category* rather than by asset
    (6-42 "COT" has 13 leaves, none naming a commodity), a COT series' `explorers` points
    only back at its own trader-category node, and unlike Futures and Options these
    series carry no `underlying_series_symbol`. The name is it.

    Pure string handling, so labelling a whole family costs nothing extra — a frame built
    with `include_preview=True` already has the column:

        df = client.search_dataframe(subset=NON_COMMERCIAL_NODE, n=None)
        df["asset"] = [getattr(parse_cot_name(n, m), "asset", None)
                       for n, m in zip(df.full_name, df.mnemonic)]

    Returns None when no trader category appears in the name, which is what makes it safe
    to map over mixed results: a commodity's *price* series ("CME - Feeder Cattle Index")
    names no trader category and so is rejected rather than mistaken for its COT sibling.

    Measured over 564 series sampled across all 13 trader-category nodes: 564 parsed, all
    13 categories seen, and for every one the category parsed out of the name matched the
    tree node the series was actually drawn from — an independent check, since the node
    is not an input. Of the 111 mnemonic prefixes covered, the asset agreed across every
    series sharing a prefix bar two, and both are the site naming one contract two ways
    rather than a misparse: CGS is "Goldman Sachs Commodity Index(GSCI)" in some names and
    "Standard and Poors / Goldman Sachs Commodity Index(GSCI)" in others, NGF alternates
    "Gulf #6 Fuel 3% Swap" with "Gulf £6 Fuel 3% Swap". 12 non-COT series across seven
    categories (including a commodity's own price index) were all rejected.

    The vocabulary is the site's and can drift, so treat this as a parser over scraped
    text: `asset` and `trader_category` are the checked fields, `position` and
    `futures_only` are read off a small, consistent tail vocabulary (Long / Short /
    Spreading, optionally "Futures Only") but have no equivalent cross-check.
    """
    if not full_name:
        return None
    text = full_name.translate(_ZERO_WIDTH)

    head = _COT_HEAD_RE.fullmatch(text)
    if not head:
        return None
    exchange, code, rest = head.groups()
    rest = _COT_BOILERPLATE_RE.sub(" ", rest)

    # `mnemonic` settles the one genuinely ambiguous case, where the asset's last word and
    # the category's first word form another category between them:
    # "...-5YR Interest Rate Swap Dealer / Intermediary Spreading" (CNGDIXC) reads as
    # "Swap Dealer" starting a word earlier than the real "Dealer Intermediary" does, and
    # the asset loses its "Swap". The mnemonic says DI outright, so prefer that category
    # when it is given and does appear in the name.
    wanted = _cot_code_from_mnemonic(mnemonic)
    patterns = _COT_CATEGORY_PATTERNS
    if wanted is not None:
        preferred = [p for p in patterns if p[0] == wanted and p[2].search(rest)]
        if preferred:
            patterns = preferred

    # earliest match wins, so the category terminates the asset; on a tie the longest
    # does, so "Total Reportable" isn't read as "Total" with "Reportable" left in the
    # position. Long forms are tried across the whole string before the abbreviations,
    # which are ambiguous enough to want as a fallback only.
    best: tuple[int, int, str, str] | None = None
    for cat_code, name, pattern in patterns:
        found = pattern.search(rest)
        if found and (best is None or (found.start(), -found.end()) < (best[0], -best[1])):
            best = (found.start(), found.end(), name, cat_code)
    if best is None:
        found = _COT_CODE_RE.search(rest)
        if not found:
            return None
        best = (found.start(), found.end(), COT_TRADER_CATEGORIES[found.group(1)],
                found.group(1))

    start, end, category, category_code = best
    asset = rest[:start].strip(" -,/")
    tail = rest[end:]
    spelled = _COT_POSITION_RE.search(tail)
    coded = None if spelled else _COT_POSITION_CODE_RE.search(tail)
    if spelled:
        # "Spread" and "Spreading" both occur; report the one the site mostly uses
        position = spelled.group(1).title()
        position = "Spreading" if position.startswith("Spread") else position
    else:
        position = _COT_POSITION_CODES[coded.group(1)] if coded else None
    return CotName(
        exchange=(exchange or "").strip() or None,
        exchange_code=(code or "").strip() or None,
        asset=asset or None,
        trader_category=category,
        trader_category_code=category_code,
        position=position,
        futures_only=bool(_COT_FUTURES_ONLY_RE.search(tail)),
    )


@dataclass
class SearchHit:
    """
    `fields` holds whatever extra columns the results grid showed, keyed by header
    text. The grid's schema is dynamic: an unfiltered text search shows
    Name/Symbol/Category/Market/Origin, while a category-filtered browse shows
    Name/Symbol/Hist./Source instead. Name/Symbol are always present so they're
    promoted to real attributes.
    """

    series_id: str
    name: str
    mnemonic: str
    fields: dict[str, str]


def _dedupe_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Drop repeated series_ids, keeping first-seen order. Needed because the site's
    paging overlaps (see SHOW_ALL_PAGE), and cheap insurance even where it doesn't."""
    seen: set[str] = set()
    unique = []
    for hit in hits:
        if hit.series_id not in seen:
            seen.add(hit.series_id)
            unique.append(hit)
    return unique


@dataclass
class SearchResults:
    hits: list[SearchHit]
    total_hits: int
    raw_page_data: dict[str, Any]


class LoginError(RuntimeError):
    pass


# the sign-in form's fields; their presence means we were bounced back to the logon page
_LOGIN_FORM_RE = re.compile(r'name="(?:usernameTextBox|passwordTextBox)"')


def _is_login_page(html: str) -> bool:
    """True if `html` is the logon page rather than a search page. Worth checking on every
    search response: an expired session redirects there, and the logon page parses as a
    perfectly well-formed search page with zero hits — a silent wrong answer otherwise."""
    return bool(_LOGIN_FORM_RE.search(html))


def _parse_headline_datatypes(cell) -> list[dict[str, str]]:
    """Parse the Headline Coverage cell: alternating <a title="..."> datatype-code tags
    and untitled <a>(from <date>)</a> tags carrying the preceding datatype's start date,
    e.g. PI (from Dec 1996) RI (from Jan 1998) VO PH PL (from May 2000)."""
    datatypes: list[dict[str, str]] = []
    for a in cell.find_all("a"):
        title = a.get("title", "")
        if title.startswith("Click for more"):  # the "More..." popup trigger, not a datatype
            continue
        if title:
            datatypes.append({"code": a.get_text(strip=True), "name": title})
        elif datatypes:
            datatypes[-1]["available_from"] = a.get_text(strip=True).strip("()")
    return datatypes


def _search_subset_ref(a_tag) -> str | None:
    """Pull the subset reference out of an <a> that calls Search.subset("...") or
    carries a data-subset="..." attribute (both seen in the preview fragment's
    Explorers/Related rows)."""
    if a_tag.has_attr("data-subset"):
        return a_tag["data-subset"]
    onclick = a_tag.get("onclick", "")
    match = re.search(r'Search\.subset\("([^"]+)"\)', onclick)
    return match.group(1) if match else None


def _parse_count(text: str) -> int | None:
    """A sidebar count cell: "1,234" -> 1234. Empty, or "(all)" meaning every hit shares
    that value so there's nothing to narrow, -> None."""
    match = re.fullmatch(r"([\d,]+)", text.strip())
    return int(match.group(1).replace(",", "")) if match else None


_TRAILING_COUNT_RE = re.compile(r"\s*\(([\d,]+)\)\s*$")


def _split_label_count(label: str) -> tuple[str, int | None]:
    """Split a trailing parenthesised count off a filter label:
    "Afghanistan (15,871)" -> ("Afghanistan", 15871).

    The "More filters" popup has no separate count cell — it bakes the number into the
    label text — so without this the count column comes back almost entirely <NA> while
    the number sits unparsed in value_label. Labels with no trailing count (or with
    non-numeric parentheses) are returned unchanged."""
    match = _TRAILING_COUNT_RE.search(label)
    if not match:
        return label, None
    return label[: match.start()].strip(), int(match.group(1).replace(",", ""))


def _parse_filter_sidebar(html: str) -> list[dict[str, Any]]:
    """Parse the "Add Filters" sidebar (<div id="refine">) of a search page into one record
    per offered filter value.

    Each filter is a <div id="refine-<name>"> whose <name> is a NAV_FILTERS entry, holding
    either the value currently applied (<span class="summary">, e.g. Category once you've
    browsed into one) or the values you could narrow to (<a class="valuecount"
    data-filtervalue=".." ><span class="count">..<span class="value">..). The inline list is
    capped at three; the full one lives in a sibling <table id="popup_<name>"> behind the
    "More single filters" link, so that's preferred when present and the inline counts are
    merged onto it."""
    soup = BeautifulSoup(html, "html.parser")
    refine = soup.find("div", id="refine")
    if not refine:
        return []

    records: list[dict[str, Any]] = []
    for div in refine.find_all("div", id=re.compile(r"^refine-")):
        name = div["id"].removeprefix("refine-")
        heading = div.find("h3")
        # the label is the h3's own <span>; any others belong to the Stop Filtering /
        # Multiple links in front of it
        label_spans = heading.find_all("span", recursive=False) if heading else []
        filter_label = label_spans[-1].get_text(strip=True) if label_spans else name

        inline: list[dict[str, Any]] = []
        for a in div.find_all("a"):
            cell = a.find("span", class_="value") or a.find("span", class_="summary")
            if cell is None:  # the h3's Stop Filtering / More single filters links
                continue
            count_cell = a.find("span", class_="count")
            value_label = cell.get_text(strip=True)
            inline.append(
                {
                    "value": a.get("data-filtervalue") or value_label,
                    "value_label": value_label,
                    "count": _parse_count(count_cell.get_text(strip=True)) if count_cell else None,
                    "applied": "summary" in (cell.get("class") or []),
                }
            )

        popup = soup.find("table", id=f"popup_{name}")
        if popup is not None:
            counts = {item["value"]: item["count"] for item in inline}
            param = popup.get("data-filterid") or f"nav_{name}"
            values = []
            for lbl in popup.find_all("label", attrs={"data-filtervalue": True}):
                value = lbl["data-filtervalue"]
                # the popup bakes the count into the label; prefer the inline count cell
                # when the same value also appeared in the sidebar's top-three list
                value_label, label_count = _split_label_count(lbl.get_text(strip=True))
                values.append(
                    {
                        "value": value,
                        "value_label": value_label,
                        "count": counts.get(value) if counts.get(value) is not None else label_count,
                        "applied": False,
                    }
                )
        else:
            param, values = f"nav_{name}", inline

        for item in values:
            records.append(
                {"filter_name": name, "param": param, "filter_label": filter_label, **item}
            )
    return records


def _extract_page_data(html: str) -> dict[str, Any]:
    """Extract the `var PageData = {...};` object embedded in a search.aspx page.
    Brace-matched by hand (not regex) because PageData nests objects/arrays, which
    breaks a naive non-greedy regex on large result sets."""
    marker = "var PageData = "
    start = html.find(marker)
    if start == -1:
        return {}
    start += len(marker)

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return {}


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _snake_case(text: str) -> str:
    """Normalize a grid/preview column label into a snake_case identifier, e.g.
    "Maturity Date" -> "maturity_date", "Hist." -> "hist", "categoryId" -> "category_id".
    Labels that are already one run of lowercase letters (e.g. "seriesstartdate", a raw
    API key with no word-boundary to split on) pass through unchanged."""
    text = _CAMEL_BOUNDARY_RE.sub("_", text.replace(".", ""))
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", "_", text.strip()).lower()


# preview()'s chart-panel fields that hold a single date, serialized by the site's own
# data-chart JSON rather than scraped from a labelled cell — so, unlike the labelled
# fields, their format isn't under our control and is worth normalizing on the way out.
_DATE_FIELDS = ("seriesstartdate", "latestvaluedate")


def _coerce_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Parse `_DATE_FIELDS` columns as datetimes where present, applied to every
    DataFrame-returning method (search_dataframe, categories, category_filters,
    browse_tree_children) — a no-op on the frames that never carry these columns. Dict
    returns (preview(), lookup()) are left as scraped strings.

    `errors="coerce"` rather than raising: these come from HTML scraping, so an
    unexpected format should surface as NaT for that row, not blow up the whole frame
    (see "HTML parsing is brittle" in CLAUDE.md)."""
    for col in _DATE_FIELDS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _merge_labelled(
    sections: Sequence[tuple[str | None, Sequence[tuple[str, Any]]]],
) -> dict[str, Any]:
    """Flatten sections of (label, value) pairs into one dict without letting a label
    collision drop a value.

    The site reuses labels across the preview panel's sections, and `_snake_case`
    collapses the difference: a futures contract carries "Underlying Series" in the
    symbol block (the underlying's *mnemonic*, e.g. "CORNUS2") and "Underlying series"
    in the datagrid (its *name*, e.g. "Corn No.2 Yellow U$/Bushel"). Both key to
    `underlying_series` and whichever was parsed last used to win silently.

    The last section to use a key keeps the bare key — the datagrid is the panel's
    field list and the source of most documented keys, so `underlying_series` still
    means what it did. An earlier section that wanted the same key is renamed with its
    own suffix hint (`underlying_series_symbol` for the mnemonic). With no hint, or a
    hint already taken, the fallback is `<key>_2`, `<key>_3`, ...

    A repeated label carrying the *same* value is dropped rather than renamed: LDB
    appears in both the symbol block and the datagrid of an economics series, always as
    the same string, and an `ldb_symbol` twin of it would be pure noise.
    """
    # values each section's successors will write under a given key, so an earlier
    # section can step aside — or, for an identical value, drop out entirely
    tail_values: list[dict[str, list[Any]]] = []
    claimed: dict[str, list[Any]] = {}
    for _, pairs in reversed(sections):
        tail_values.append({key: list(values) for key, values in claimed.items()})
        for key, value in pairs:
            claimed.setdefault(key, []).append(value)
    tail_values.reverse()

    result: dict[str, Any] = {}
    for (hint, pairs), later in zip(sections, tail_values):
        for key, value in pairs:
            taken = key in result or key in later
            if not taken:
                result[key] = value
                continue
            if value == result.get(key) or any(value == v for v in later.get(key, ())):
                continue  # same label, same value — nothing to preserve
            hinted = f"{key}_{hint}" if hint else None
            if hinted and hinted not in result and hinted not in later:
                result[hinted] = value
                continue
            n = 2
            while f"{key}_{n}" in result or f"{key}_{n}" in later:
                n += 1
            result[f"{key}_{n}"] = value
    return result


class _TimeoutSession(requests.Session):
    """A Session with a default timeout on every request.

    requests has no default timeout, and `timeout=` has to be passed per call — easy to
    forget at one of the several call sites here, and a single forgotten one is enough to
    hang indefinitely. Defaulting it in `request()` means no caller can omit it, while an
    explicit `timeout=` argument still wins.
    """

    def __init__(self, timeout: float | tuple[float, float] | None):
        super().__init__()
        self.timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


class DatastreamWebClient:
    def __init__(
        self,
        username: str,
        password: str,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: float | tuple[float, float] | None = DEFAULT_TIMEOUT,
        entitled_only: bool = DEFAULT_ENTITLED_ONLY,
    ):
        """`timeout` is seconds per request (connect and read), or a (connect, read)
        pair, applied to every call to the site. None disables it, which risks hanging
        forever on an unresponsive server — prefer a large number to None.

        `entitled_only` (default True) sends nav_ldbpermission=Entitled on every search,
        so searching, browsing and filtering only ever surface series this login can
        actually pull data for. Turn it off for the whole client, or override it per call
        with an explicit `ldbpermission=` (`None` for no filter, NOT_ENTITLED to look at
        exactly what you're missing). lookup() ignores it — see its docstring."""
        self.username = username
        self.password = password
        self.max_workers = max_workers
        self.timeout = timeout
        self.entitled_only = entitled_only
        self.session = _TimeoutSession(timeout)
        self.session.headers.update(DEFAULT_HEADERS)
        # keep the connection pool in step with the worker count, otherwise urllib3
        # discards the surplus connections and warns on every request
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_workers, pool_maxsize=max_workers
        )
        self.session.mount("https://", adapter)
        self._logged_in = False
        # every real request in the captured traffic sends Referer = the last
        # search.aspx page visited; not confirmed as required, but tracked and replayed
        # to keep follow-up requests faithful to what a real browser session sends
        self._referer: str | None = None

    def login(self) -> None:
        login_params = {
            "AppGroup": "DSAddin",
            "env": "PROD",
            "persisttoken": "true",
            "redirect": f"{BASE}/browse/search.aspx?AppGroup=DSAddin",
            "srcapp": "Navigator",
            "srcappver": "4.18.23",
        }

        get_resp = self.session.get(LOGIN_URL, params=login_params)
        get_resp.raise_for_status()
        soup = BeautifulSoup(get_resp.text, "html.parser")

        def hidden(name: str) -> str:
            tag = soup.find("input", {"name": name})
            return tag["value"] if tag and tag.has_attr("value") else ""

        form_data = {
            "__LASTFOCUS": "",
            "__VIEWSTATE": hidden("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hidden("__VIEWSTATEGENERATOR"),
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__EVENTVALIDATION": hidden("__EVENTVALIDATION"),
            "usernameTextBox": self.username,
            "passwordTextBox": self.password,
            "signonbutton": "Sign In",
        }

        post_resp = self.session.post(
            LOGIN_URL, params=login_params, data=form_data, allow_redirects=True
        )
        post_resp.raise_for_status()
        if "search.aspx" not in post_resp.url:
            raise LoginError(f"login did not redirect to search.aspx (ended at {post_resp.url})")

        self._logged_in = True
        self._referer = post_resp.url

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _apply_entitlement(
        self, params: dict[str, str], filters: dict[str, Any]
    ) -> dict[str, str]:
        """Add this client's nav_ldbpermission default to an already-built param dict.

        Kept separate from _nav_params so that (a) search()'s "you gave me nothing to
        search for" check still sees an empty dict, and (b) an explicit ldbpermission=
        from the caller — including `None`, which _nav_params drops, meaning "no filter" —
        always wins over the default."""
        if not self.entitled_only:
            return params
        if any(key.removeprefix("nav_") == "ldbpermission" for key in filters):
            return params
        params["nav_ldbpermission"] = ENTITLED
        return params

    def _get_search_html(self, params: dict[str, str]) -> str:
        self._ensure_login()
        params = {"AppGroup": "DSAddin", "dsid": self.username, **params}

        for attempt in (1, 2):
            resp = self.session.get(
                SEARCH_URL, params=params, headers={"Referer": self._referer}
            )
            resp.raise_for_status()
            if not _is_login_page(resp.text):
                # when pages are fetched in parallel this races, but harmlessly: every
                # candidate value is a valid search.aspx URL for the same query, differing
                # only in `page`
                self._referer = resp.url
                return resp.text
            if attempt == 1:
                # session expired mid-session (long tree walks and big DataFrames run for
                # a while); re-authenticate once and retry before giving up
                self._logged_in = False
                self.login()

        raise LoginError(
            "search.aspx returned the logon page twice; the session could not be renewed"
        )

    def _get_search_page(self, params: dict[str, str]) -> SearchResults:
        return self._parse_search_page(self._get_search_html(params))

    def search(
        self,
        term: str | None = None,
        page: int = 1,
        subset: str | None = None,
        **filters: Any,
    ) -> SearchResults:
        """
        Search the Navigator, optionally under browse filters — the two combine exactly as
        they do in the web app, where typing in the search box keeps whatever filters the
        "Add Filters" sidebar has applied:

            search("bank")                                  # free text, unfiltered
            search("bank", category="3", startyear=1996)    # free text within a subset
            search(category="3", source="Bank of New York") # filters only (= browse())
            search("sugar", category="Futures")             # category by name
            search("sugar", category=["Futures", "Options"])

        Filters are the nav_* ones listed in NAV_FILTERS, named with or without the prefix;
        pass a list for a multi-value filter. Results are paginated (15/page); pass `page`
        for subsequent pages.

        One filter is applied for you: unless the client was built with
        entitled_only=False, every search sends ldbpermission=Entitled, so hits are
        restricted to series this login can actually pull data for. Override it per call —
        `ldbpermission=None` for the unfiltered result set, `ldbpermission=NOT_ENTITLED`
        for exactly the series you're locked out of:

            search("ICE BofA", category="9").total_hits                       # 0
            search("ICE BofA", category="9", ldbpermission=None).total_hits   # 7414

        `category=` takes either the numeric id or the sidebar name — see CATEGORIES, or
        client.categories() for the live list. Matching ignores case and punctuation, so
        "Bonds & Convertibles", "bonds and convertibles" and "13" are the same filter. An
        unrecognised name raises rather than silently searching everything, unlike every
        other filter value (see the module's note on inventing filter values).

        `page=SHOW_ALL_PAGE` (-1) is the site's "Show all": the entire result set in one
        response, no pager. Prefer it to walking pages whenever you want everything —
        page N and page N+1 are not disjoint, because the server reorders tied hits
        between requests, so a walk both repeats and drops rows. search_dataframe() picks
        it for you when the result set is small enough.

        `subset` is a browse-tree leaf's `subset_ref` (the `data` field of a tree node, e.g.
        "exp1|12-602|M#UKKEY||Y|||"), which is how the site addresses a node *below* the top
        level — nav_category only ever resolves to a top-level category, so a deep tree node
        must be reached this way:

            leaves = client.browse_tree_children("12-4238", depth=2)
            ref = leaves.loc[leaves.text.str.startswith("Key Indicators"), "subset_ref"].iloc[0]
            client.search(subset=ref)            # the 73 series in that node
            client.search("debt", subset=ref)    # ...narrowed by a text query
        """
        params = _nav_params(filters)
        if term is not None:
            params["q"] = term
        if subset is not None:
            params["subset"] = subset
        if not params:
            raise TypeError("search() needs a term, a subset, at least one filter, or a mix")
        # after the emptiness check: the entitlement default is this client's, not
        # something the caller asked to search for
        self._apply_entitlement(params, filters)
        if page != 1:
            params["page"] = str(page)
        return self._get_search_page(params)

    def lookup(self, mnemonic: str, include_preview: bool = True) -> dict[str, Any] | None:
        """
        Resolve one known mnemonic to its metadata, or None if nothing matches it exactly.

        Searching a mnemonic and taking hits[0] is the commonest operation there is, but
        it's wrong often enough to matter: "TRUS10T" is unique, while a mnemonic that is
        also a substring of others ranks alongside them. This matches the mnemonic exactly
        (case-insensitively) rather than trusting rank, so a near-miss returns None instead
        of a confidently wrong neighbour.

            client.lookup("TRUK10T")["name"]   # 'UK GVT BMK BID YLD 10Y'
            client.lookup("NOTATICKER")        # None

        The result merges the grid row (series_id, name, mnemonic, plus that grid's dynamic
        columns) with preview() — datatype, frequency, source, market, timespan and the
        rest — which is what the official SDK needs. Grid columns win on a clash, as in
        search_dataframe(). Pass include_preview=False to skip the extra request.

        Unlike search()/browse(), lookup() deliberately ignores `entitled_only` and
        resolves a mnemonic whether or not this login is entitled to it — filtering here
        would turn "you have no permission for this series" into an indistinguishable
        None, which is exactly the question you're asking when you look up a ticker you
        already have. Instead the result carries `entitled`:

            client.lookup("MLGCORL")["entitled"]   # False — ICE BofA, database SI5
            client.lookup("TRUK10T")["entitled"]   # True

        That costs nothing for an entitled series (the Entitled half is searched first)
        and one extra grid request for a blocked or non-existent one.

        `entitled` is the site's nav_ldbpermission flag. True has always meant the API
        serves the series. False means the licence for its database doesn't cover you, and
        matched a DatastreamPy "ACCESS DENIED" everywhere except sovereign ESG and Reuters
        consensus forecasts — so a False outside Economics is worth believing. The
        confirmation is a get_data() call, not the site's chart, which is licensed more
        widely than the feed. See the module note on DEFAULT_ENTITLED_ONLY.
        """
        target = mnemonic.strip().upper()
        for permission in (ENTITLED, NOT_ENTITLED):
            for hit in self.search(mnemonic, ldbpermission=permission).hits:
                if hit.mnemonic.strip().upper() != target:
                    continue
                row = {
                    "series_id": hit.series_id,
                    "name": hit.name,
                    "mnemonic": hit.mnemonic,
                    "entitled": permission == ENTITLED,
                    **hit.fields,
                }
                if include_preview:
                    for key, value in self.preview(hit.series_id).items():
                        row.setdefault(key, value)
                return row
        return None

    def cot_name(self, mnemonic: str) -> CotName | None:
        """
        The parts of a Commitments of Traders series — exchange, asset, trader category —
        or None if `mnemonic` isn't one.

            client.cot_name("CFCNCSI").asset            # 'Feeder Cattle'
            client.cot_name("CFCNCSI").trader_category  # 'Non-Commercial'
            client.cot_name("CFCINDX")                  # None — that's the price index

        One lookup() plus its preview. To label many series, call parse_cot_name() on a
        frame's `full_name` column instead — that costs no extra requests.
        """
        meta = self.lookup(mnemonic)
        return parse_cot_name(meta.get("full_name"), mnemonic) if meta else None

    def cot_asset(self, mnemonic: str) -> str | None:
        """The underlying asset of a COT series — `cot_name(mnemonic).asset`, or None.

            client.cot_asset("CFCNCSI")   # 'Feeder Cattle'
        """
        parsed = self.cot_name(mnemonic)
        return parsed.asset if parsed else None

    def browse(
        self,
        nav_category: str | None = None,
        nav_source: str | None = None,
        nav_startyear: str | int | None = None,
        page: int = 1,
        **extra_nav_filters: Any,
    ) -> SearchResults:
        """Drill into the category tree with no text query, mirroring the left-hand browse
        panel. Shorthand for search(None, category=..., ...).

        `nav_category` is optional — omit it when `subset=` already scopes the query to a
        tree node, since the site sends `subset=` alone when you browse one. (Adding
        `nav_category` on top of a `subset=` is measurably a no-op, not a narrowing, so it
        is never the reason a subset frame comes back short; `n` is. See
        search_dataframe().)

        The three nav_-prefixed parameters are just the common filters spelled out; every
        other filter arrives through `**extra_nav_filters` under its bare name, as in
        search(). Both spellings of the same filter reach here as separate arguments, so
        specifying one twice (`browse(nav_source=..., source=...)`) is rejected rather
        than silently resolved."""
        filters: dict[str, Any] = dict(extra_nav_filters)
        for name, value in (
            ("category", nav_category),
            ("source", nav_source),
            ("startyear", nav_startyear),
        ):
            if value is None:
                continue
            # the bare name is what search() takes, so without this an `extra` of the same
            # filter collides with the named parameter and Python raises an opaque
            # "got multiple values for keyword argument" from inside search()
            if name in filters or f"nav_{name}" in filters:
                raise TypeError(
                    f"browse() got {name}= (or nav_{name}=) twice: once as the named "
                    f"nav_{name} parameter and once in **extra_nav_filters. Pass it once."
                )
            filters[name] = value
        return self.search(None, page=page, **filters)

    def _walk_pages(
        self,
        fetch_page: Callable[[int], SearchResults],
        wanted: int,
        page_size: int,
        first_hits: list[SearchHit],
    ) -> list[SearchHit]:
        """Page through a result set too large for page=-1, in parallel rounds.

        Deliberately not a single ceil(wanted/page_size) fan-out: because the ordering
        shifts between requests, a round comes back with duplicates, so N pages yield
        fewer than N*page_size distinct rows and a fixed page count falls short. Keep
        requesting rounds until we have enough distinct hits or a round adds nothing.
        """
        hits = list(first_hits)
        seen = {hit.series_id for hit in hits}
        next_page = 2
        while len(seen) < wanted:
            remaining = wanted - len(seen)
            rounds = min(
                self.max_workers, max(1, math.ceil(remaining / max(page_size, 1)))
            )
            pages = range(next_page, next_page + rounds)
            next_page += rounds
            added = 0
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for results in pool.map(fetch_page, pages):
                    for hit in results.hits:
                        if hit.series_id not in seen:
                            seen.add(hit.series_id)
                            hits.append(hit)
                            added += 1
            if not added:
                break  # ran off the end of the pager, or it is only returning repeats
        return hits

    def _hits_to_dataframe(
        self,
        fetch_page: Callable[[int], SearchResults],
        n: int | None,
        include_preview: bool,
    ) -> pd.DataFrame:
        n_explicit = n is not _N_DEFAULT
        if not n_explicit:
            n = DEFAULT_HITS

        # page 1 first, on its own: it logs the session in (so the workers below never
        # race to log in concurrently) and reports total_hits, which decides between the
        # three ways of getting the rest — already have them, one "Show all" request, or
        # a paged walk
        first = fetch_page(1)
        if not first.hits:
            return pd.DataFrame(columns=list(RESULT_COLUMNS))

        if n is None and first.total_hits > SHOW_ALL_MAX_HITS:
            # "all of them" is only deliverable while page=-1 is, because paging isn't a
            # stable partition (see SHOW_ALL_PAGE) — above the cap it would mean walking
            # a pager that repeats rows and still never shows all of them, for hundreds
            # of thousands of requests. Refuse rather than grind.
            raise ValueError(
                f"n=None asks for every hit, but this query has {first.total_hits}, more "
                f"than the {SHOW_ALL_MAX_HITS} that page={SHOW_ALL_PAGE} ('Show all') can "
                f"return in one request. Above that the site's paging overlaps and leaks "
                f"rows, so a complete set isn't available at all. Narrow the query with "
                f"filters, or pass an explicit n for a partial frame."
            )

        wanted = first.total_hits if n is None else min(n, first.total_hits)
        if len(first.hits) >= wanted:
            hits = _dedupe_hits(first.hits)
        elif first.total_hits <= SHOW_ALL_MAX_HITS:
            # the whole set in one request, which is the only way to get a partition of
            # the results that neither repeats nor drops rows — see SHOW_ALL_PAGE
            hits = _dedupe_hits(fetch_page(SHOW_ALL_PAGE).hits)
        else:
            hits = self._walk_pages(fetch_page, wanted, len(first.hits), first.hits)

        if len(hits) < wanted:
            warnings.warn(
                f"asked for {wanted} hits of {first.total_hits} but only {len(hits)} "
                f"distinct ones came back. The site's paging is not a stable partition "
                f"of the result set, so a multi-page walk repeats some rows and misses "
                f"others; only page={SHOW_ALL_PAGE} ('Show all') is exhaustive, and this "
                f"result set is too large ({first.total_hits} > {SHOW_ALL_MAX_HITS}) for "
                f"that. Narrow the query with filters instead of paging deeper.",
                stacklevel=3,
            )

        if n is not None and len(hits) > n:
            # rows already fetched and about to be dropped on the floor. Commonest on a
            # subset search, which is unpaginated — the whole node arrives in the first
            # response, so a 69-series node silently becomes 45 rows with no sign that
            # anything is missing. Only the default warns; see _N_DEFAULT. And only when
            # the whole result set is in hand: a paged walk overshoots `wanted` by up to a
            # round, and telling someone their 3.4M-hit search "already retrieved 54" —
            # with n=3441649 as the remedy — would be worse than saying nothing.
            if not n_explicit and len(hits) >= first.total_hits:
                warnings.warn(
                    f"this query has {first.total_hits} hits and {len(hits)} of them were "
                    f"already retrieved, but n defaults to {DEFAULT_HITS} so the frame "
                    f"keeps only the first {n}. Pass n={first.total_hits} for the rest, or "
                    f"n=None for however many there are.",
                    stacklevel=3,
                )
            hits = hits[:n]

        rows: list[dict[str, Any]] = [
            {
                "series_id": hit.series_id,
                "name": hit.name,
                "mnemonic": hit.mnemonic,
                **hit.fields,
            }
            for hit in hits
        ]
        if include_preview:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                previews = pool.map(self.preview, [hit.series_id for hit in hits])
                for row, preview in zip(rows, previews):
                    for key, value in preview.items():
                        # grid columns win on a clash (market/source/mnemonic overlap);
                        # they're consistent across every row of a given query
                        row.setdefault(key, value)
        return _coerce_date_columns(pd.DataFrame(rows))

    def search_dataframe(
        self,
        term: str | None = None,
        n: int | None = _N_DEFAULT,
        include_preview: bool = True,
        subset: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Build a DataFrame of up to `n` hits for search(). Takes the same term/filter
        combination as search(), so a text query can be run under browse filters.

        `n` defaults to DEFAULT_HITS (45) as a cost cap, and `n=None` means every hit the
        query has. The cap bites hardest on a `subset=` search, which isn't paginated —
        the node's whole series list arrives in the first response, so a 69-series node
        would be cut to 45 rows. Leaving `n` at the default now warns when it discards
        rows that were already fetched; an explicit `n` is taken as deliberate and stays
        quiet.

        How the hits are fetched depends on how many there are. Up to SHOW_ALL_MAX_HITS
        it issues the site's "Show all" (page=-1) in a single request, because paging is
        not a stable partition of the result set — the server reorders tied hits between
        requests, so pages overlap and a 7-page walk of 101 hits returns ~84 distinct.
        Above that it pages (15/page, `max_workers` in flight), de-duplicating on
        series_id and requesting further rounds until `n` distinct hits are in hand;
        if the pager runs dry first it warns rather than quietly returning a short frame.

        Columns come from the results grid (series_id, name, mnemonic, plus whatever dynamic
        columns that query's grid showed) and, if `include_preview`, everything preview()
        returns — chart params, mnemonic/RIC/LDB, and the full preview-pane datagrid (latest
        value, timespan, market, source, currency, headline datatypes, explorers, related).
        Where the two overlap (market, source, mnemonic) the grid's value is kept.

        `include_preview` costs one extra request per hit, so set it False for large `n`
        if you only need the grid columns.

        `subset` restricts the search to a browse-tree leaf, as in search().
        """
        return self._hits_to_dataframe(
            lambda page: self.search(term, page=page, subset=subset, **filters),
            n,
            include_preview,
        )

    def browse_dataframe(
        self,
        nav_category: str | None = None,
        nav_source: str | None = None,
        nav_startyear: str | int | None = None,
        n: int | None = _N_DEFAULT,
        include_preview: bool = True,
        **extra_nav_filters: Any,
    ) -> pd.DataFrame:
        """Same as search_dataframe(), but with no text query, like browse(). `nav_category`
        is optional — see browse()'s docstring for why it should be omitted when a
        `subset=` is already doing the scoping."""
        return self._hits_to_dataframe(
            lambda page: self.browse(
                nav_category,
                nav_source=nav_source,
                nav_startyear=nav_startyear,
                page=page,
                **extra_nav_filters,
            ),
            n,
            include_preview,
        )

    @staticmethod
    def _parse_search_page(html: str) -> SearchResults:
        # backstop for callers that fetched the HTML themselves; _get_search_html already
        # retries a bounced session, so reaching this means the page really is the logon one
        if _is_login_page(html):
            raise LoginError("expected a search page but got the logon page (session expired?)")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        table = soup.find("table", id="resulttable")
        if table:
            header_row = table.find("tr")
            # first two <th> are the pin/status icon columns and have no label. Sortable
            # columns append a "▼" sort-arrow glyph straight into the link text (no
            # separate tag to select around, e.g. "Name▼"), so strip it off too.
            headers = [
                _snake_case(re.sub(r"[▲▼]+$", "", th.get_text(strip=True)))
                for th in header_row.find_all("th")
            ][2:]

            for row in table.find_all("tr", id=re.compile(r"^hit_")):
                series_id = row["id"].removeprefix("hit_")
                cells = row.find_all("td")[2:]  # skip pin-icon, status-icon
                values = [cell.get_text(strip=True) for cell in cells]
                # _merge_labelled, not dict(zip(...)): two columns whose labels
                # snake_case to the same key would otherwise silently drop one
                fields = _merge_labelled([(None, list(zip(headers, values)))])
                hits.append(
                    SearchHit(
                        series_id=series_id,
                        name=fields.pop("name", ""),
                        mnemonic=fields.pop("symbol", ""),
                        fields=fields,
                    )
                )

        page_data = _extract_page_data(html)

        total_hits = page_data.get("totalHits")
        if total_hits is None:
            # fall back to the "<start>-<end> of <total>" pager text, e.g. "1-15 of 7,487,017"
            pager_match = re.search(r'id="pager".*?of ([\d,]+)', html, re.DOTALL)
            total_hits = int(pager_match.group(1).replace(",", "")) if pager_match else len(hits)

        return SearchResults(
            hits=hits,
            total_hits=total_hits,
            raw_page_data=page_data,
        )

    def categories(self) -> pd.DataFrame:
        """
        The top-level categories the site offers, live: name, id, and the number of series
        in each. One request — this is the "Category" list down the left of an unfiltered
        search page, which is where the CATEGORIES constant came from.

        `count` is the site's total and ignores `entitled_only`: the facet reports the same
        number either way. How many of them this login can read is
        search(category=...).total_hits — for Bond Indices, 20,136 of the 91,460 here.

            client.categories()
            #                    name  id     count
            #                Equities   0    338408
            #       Constituent Lists   1    353627
            #                     ...

        Any of these names is accepted by `category=`, so this is the discovery call for
        that filter — and the way to check CATEGORIES hasn't drifted from the site.
        """
        # deliberately not entitlement-filtered: measured, the Category facet on an
        # otherwise empty search page reports the same counts with and without
        # nav_ldbpermission (Bond Indices stays 91,460, not the 20,136 this login can
        # actually read), so sending it would only imply a narrowing that didn't happen.
        # For an entitled count, ask for the hits: search(category=...).total_hits.
        html = self._get_search_html({})
        soup = BeautifulSoup(html, "html.parser")
        rows: dict[str, dict[str, Any]] = {}
        for tag in soup.select('a[data-filterid="nav_category"][data-filtervalue]'):
            name = tag.select_one("span.value")
            if name is None:
                continue
            count = tag.select_one("span.count")
            rows[tag["data-filtervalue"]] = {
                "name": name.get_text(strip=True),
                "id": tag["data-filtervalue"],
                "count": _parse_count(count.get_text(strip=True)) if count else None,
            }
        df = pd.DataFrame(list(rows.values()), columns=["name", "id", "count"])
        df["count"] = df["count"].astype("Int64")
        return _coerce_date_columns(df.sort_values("name", ignore_index=True))

    def category_filters(
        self, nid: str, term: str | None = None, **filters: Any
    ) -> pd.DataFrame:
        """
        The filter values on offer for a category — what the "Add Filters" sidebar shows
        once you've browsed into it. One row per value: filter_name, param, filter_label,
        value, value_label, count, applied.

        Feed `value` back to search()/browse() under `param`'s name, e.g.

            options = client.category_filters("3-1")
            sources = options[options.filter_name == "source"]
            client.search(category="3", source=sources.value.tolist())

        `count` is how many hits that value would leave, or None when the sidebar shows
        "(all)" (every hit shares it, so it narrows nothing) or gives no count. `applied` is
        True for a filter already in force, whose row carries the applied value rather than
        an option.

        `nid` is a browse-tree node id; only its category prefix is used, since nav_category
        addresses a top-level category ("3-1" and a bare "3" both mean Equity Indices).
        Deeper tree nodes are addressed by their subset_ref instead, not by nav_category, so
        this returns the filters for the whole top-level category either way. Narrow it with
        `term` and further `filters`, which apply exactly as they do in search() — the
        sidebar's offer reflects the current query.
        """
        params = self._apply_entitlement(
            _nav_params({"category": nid.split("-")[0], **filters}), filters
        )
        if term is not None:
            params["q"] = term
        html = self._get_search_html(params)
        options = pd.DataFrame(_parse_filter_sidebar(html), columns=FILTER_COLUMNS)
        # nullable Int64, so a missing count stays <NA> instead of turning the column float
        return _coerce_date_columns(options.astype({"count": "Int64"}))

    def _tree_nodes(self, nid: str = "#") -> list[dict[str, Any]]:
        """Raw child nodes of the browse tree at `nid` ('#' = root), in jstree.js format:
        {text, id, data, a_attr, children, type}. `children` is False for a leaf or True if
        it has its own children (recurse with that node's id). Leaf nodes with type "series"
        carry a subset reference in `data` (e.g. "exp1|3-2|EIB#1||Y|||"), matching the
        Search.subset() calls used by the page's own JS."""
        self._ensure_login()
        params = {"nid": nid, "dt": "", "str": "", "_": "1"}
        if nid != "#":
            # the root call never sends `ops` in real traffic; including it there
            # returns an empty array instead of the top-level categories
            params["ops"] = "getidswithdata"
        resp = self.session.get(
            EXPLORER_LEAVES_URL,
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": self._referer},
        )
        resp.raise_for_status()
        data = resp.json()
        # root ('#') returns a flat list of nodes; any other nid wraps them as
        # [{"<nid>": [...nodes...]}]
        if nid != "#" and data and isinstance(data[0], dict) and nid in data[0]:
            return data[0][nid]
        return [node for node in data if isinstance(node, dict)]

    def browse_tree_children(self, nid: str = "#", depth: int | None = 1) -> pd.DataFrame:
        """
        Walk the browse tree below `nid` ('#' = root) and return one row per node:
        id, text, parent, depth, type, has_children, subset_ref, path.

        `depth` counts levels below `nid`: 1 (the default) is the direct children, 2 adds
        their children, and None walks the whole subtree. Each level is fetched with up to
        `max_workers` requests in flight; only nodes flagged as having children are
        expanded, so a level costs one request per parent node.

        Beware `depth=None` from the root — the full tree bottoms out in millions of series
        and is one request per branch node. Anchor it on a subtree (e.g. nid="3-1") instead.

        `subset_ref` is the leaf's Search.subset() reference; `path` is the node's
        ancestry as names, joined with "»" the way the site writes explorer paths.
        """
        if depth is not None and depth < 1:
            raise ValueError(f"depth must be >= 1 or None (whole subtree), got {depth}")
        self._ensure_login()  # log in here, so the workers below never race to do it

        rows: list[dict[str, Any]] = []
        seen: set[str] = {nid}
        frontier: list[tuple[str, tuple[str, ...]]] = [(nid, ())]
        level = 1
        while frontier and (depth is None or level <= depth):
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                batches = pool.map(lambda item: self._tree_nodes(item[0]), frontier)
                fetched = list(zip(frontier, batches))

            next_frontier: list[tuple[str, tuple[str, ...]]] = []
            for (parent_id, parent_path), nodes in fetched:
                for node in nodes:
                    node_id = node.get("id", "")
                    path = parent_path + (node.get("text", ""),)
                    has_children = bool(node.get("children"))
                    rows.append(
                        {
                            "id": node_id,
                            "text": node.get("text", ""),
                            "parent": None if parent_id == "#" else parent_id,
                            "depth": level,
                            "type": node.get("type", ""),
                            "has_children": has_children,
                            "subset_ref": node.get("data") or None,
                            "path": " » ".join(path),
                        }
                    )
                    # `seen` guards against a node reappearing under another branch, which
                    # would otherwise loop forever on depth=None
                    if has_children and node_id and node_id not in seen:
                        seen.add(node_id)
                        next_frontier.append((node_id, path))
            frontier = next_frontier
            level += 1

        return _coerce_date_columns(pd.DataFrame(rows, columns=TREE_COLUMNS))

    def preview(self, series_id: str) -> dict[str, Any]:
        """
        Fetch the hover/pin preview fragment for a hit and pull out its metadata:
        chart params, the panel's title, mnemonic/RIC/LDB, and everything in the datagrid
        (Latest Value, Timespan, Market, Source, Currency, Headline Coverage, Explorers,
        Related, ...).

        `full_name` is the panel's title: the name with Datastream's abbreviations
        expanded, which exists nowhere in the search grid. Worth pulling for anything a
        human reads —

            client.lookup("NSA0315")["name"]        # 'NYM - SUGAR #11 MAR 2015'
            client.lookup("NSA0315")["full_name"]   # 'NYMEX - Sugar #11 March 2015'

        NOTE: the "More..." datatypes link (MoreDatatypesPopup) and the full Explorers/
        Related targets are their own AJAX calls that haven't been captured — the
        `subset_ref` values below (e.g. "exp1|3-55298|EIS#84|...") are what those calls
        take as input, but the endpoint that consumes them is unconfirmed.
        """
        self._ensure_login()
        params = {
            "AppGroup": "DSAddin", "debug": "", "dforic": "", "dsid": self.username,
            "dt": "", "explorerMode": "", "exportToExcel": "", "forcedomain": "",
            "host": "", "isGrouped": "", "isLongname": "", "isShortdesc": "", "l": "",
            "lazy": "false", "multiSelect": "", "nova": "", "outboundChannel": "",
            "pattern": "", "selectDatatypes": "", "SymbolPref": "", "term": "",
            "TSDatatypesOnly": "", "version": "",
        }
        # same one-shot re-authentication as _get_search_html. Worth having here too: a
        # search_dataframe(include_preview=True) issues one of these per hit and can run
        # for a long time, and an expired session shows up on this endpoint as a bare
        # HTTP 403 rather than the logon page, so without this the whole frame dies on a
        # session that search() would have quietly renewed.
        for attempt in (1, 2):
            resp = self.session.post(
                SEARCHSTRAGGLER_URL,
                params=params,
                data={"ids": series_id},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": self._referer},
            )
            expired = resp.status_code == 403 or _is_login_page(resp.text)
            if not expired:
                break
            if attempt == 1:
                self._logged_in = False
                self.login()
        resp.raise_for_status()
        if _is_login_page(resp.text):
            raise LoginError(
                "searchstraggler.aspx returned the logon page twice; the session could "
                "not be renewed"
            )
        soup = BeautifulSoup(resp.text, "html.parser")

        chart_div = soup.find("div", attrs={"data-chart": True})
        chart_meta = json.loads(chart_div["data-chart"]) if chart_div else {}

        chart_pairs = [(_snake_case(k), v) for k, v in chart_meta.items()]

        # the panel's title line, <div><h3>..</h3></div> immediately above the symbol
        # block. This is the *unabbreviated* name, and it's the one thing in the fragment
        # with no equivalent anywhere else: the results grid carries Datastream's
        # truncated form ("NYM - SUGAR #11 MAR 2015", "UK GVT BMK BID YLD 10Y") and this
        # spells it out ("NYMEX - Sugar #11 March 2015", "United Kingdom Government
        # Benchmark Bid Yield 10 Years"). Kept as `full_name` alongside the grid's `name`
        # rather than replacing it, since the abbreviated form is what the site's own
        # result lists show.
        #
        # Anchored on .spot-symbols rather than just taking the first <h3>, so a heading
        # added elsewhere in the fragment can't quietly become the name. Measured as the
        # only <h3> present on series from all fifteen categories.
        spot = soup.find(class_="spot-symbols")
        heading = spot.find_previous("h3") if spot else soup.find("h3")
        name_pairs: list[tuple[str, Any]] = []
        if heading and heading.get_text(strip=True):
            name_pairs.append(("full_name", heading.get_text(" ", strip=True)))

        # the identifier block above the datagrid: Mnemonic, RIC, LDB, Underlying Series
        symbol_pairs: list[tuple[str, Any]] = []
        for span in soup.select(".spot-symbols .fake-inline-table"):
            label = span.find(class_="fake-th")
            value = span.find(class_="fake-td")
            if label and value:
                symbol_pairs.append(
                    (_snake_case(label.get_text(strip=True)), value.get_text(strip=True))
                )

        grid_pairs: list[tuple[str, Any]] = []
        datagrid = soup.find("table", class_="datagrid")
        if datagrid:
            for row in datagrid.find_all("tr"):
                header, cell = row.find("th"), row.find("td")
                if not header or not cell or not header.get_text(strip=True):
                    continue
                key = _snake_case(header.get_text(strip=True))
                if key == "headline_coverage":
                    grid_pairs.append(("headline_datatypes", _parse_headline_datatypes(cell)))
                elif key in ("explorers", "related"):
                    grid_pairs.append((
                        key,
                        [
                            {
                                "label": a.get_text(strip=True),
                                "subset_ref": _search_subset_ref(a),
                            }
                            for a in cell.find_all("a")
                        ],
                    ))
                else:
                    grid_pairs.append((key, cell.get_text(" ", strip=True)))

        # merged rather than assigned into one dict: the sections share labels — see
        # _merge_labelled, which keeps both "Underlying Series" (mnemonic) and
        # "Underlying series" (name) instead of letting the datagrid clobber the symbol
        return _merge_labelled(
            [
                (None, chart_pairs),
                (None, name_pairs),
                ("symbol", symbol_pairs),
                (None, grid_pairs),
            ]
        )