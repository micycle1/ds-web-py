"""Typed results returned by the client.

Each model keeps the fields the site reliably provides as attributes, and whatever else a
response carried in a `fields`/`extra` dict keyed by snake_case label — the site's schemas
vary by category (a future has a Settlement Date, a bond a Coupon), so a fixed set of
attributes can't hold everything. `ds_web.to_frame()` turns a list of any of these into a
DataFrame, flattening those dicts into columns.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .query import Query


@dataclass(frozen=True)
class Link:
    """A link to a set of series: a tree node, list, relationship ("100 Constituents",
    "192 Futures") or explorer suggestion. Search it with `client.search(link.query())`.

    `filters` are sidebar filters the site applies along with the subset (some explorer
    links carry them). `count` is the number in the label when it has one. `path` is the
    tree path for explorer links ("Equity Indices » By Source » FTSE » ...").
    """

    label: str
    subset: str
    filters: dict[str, str] = field(default_factory=dict)
    path: str | None = None
    count: int | None = None

    def query(self, **kwargs: Any) -> Query:
        """A Query for this link's series; keyword arguments refine it as in
        Query.replace()."""
        query = Query._raw(subset=self.subset, encoded=self.filters)
        return query.replace(**kwargs) if kwargs else query

    def __hash__(self) -> int:
        return hash((self.subset, self.label))


@dataclass(frozen=True)
class SearchHit:
    """One row of the results grid.

    `fields` holds the grid's other columns, which depend on the query: an unfiltered
    search shows category/market/origin, a Futures search exchange/type/settlement_date,
    and so on. `status` is the row's flags as the site words them, e.g.
    ("Active", "Major Security", "Primary Quote").
    """

    series_id: str
    name: str
    symbol: str
    """The grid's Symbol column: the DS mnemonic, or the DS code where there's none."""
    status: tuple[str, ...] = ()
    has_notes: bool = False
    fields: dict[str, str] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.series_id)

    @property
    def active(self) -> bool | None:
        """True/False from the status flags, None when the grid gave no status."""
        if not self.status:
            return None
        return "Active" in self.status


@dataclass(frozen=True)
class FilterOption:
    """One value offered by the "Add Filters" sidebar. Apply it with
    `query.replace(**{option.filter_name: option.value})`.

    `count` is how many hits the value would leave (None when the site shows "(all)" or
    no count). `applied` marks a filter already in force.
    """

    filter_name: str
    param: str
    filter_label: str
    value: str
    value_label: str
    count: int | None
    applied: bool


@dataclass(frozen=True)
class SearchPage:
    """One response of the results grid: a page of hits plus everything around it."""

    query: Query
    page: int
    hits: list[SearchHit]
    total_hits: int
    filters: list[FilterOption]
    sort_options: dict[str, str]
    """{column label: sort code} for the grid's sortable columns."""
    explorer_suggestions: list[Link]
    """The "Explore more results like these" links."""
    search_ref: str | None
    """The site's permalink for this search (see Query.from_ref)."""

    @property
    def is_complete(self) -> bool:
        """True when this page holds every hit of the query."""
        return len(self.hits) >= self.total_hits


@dataclass(frozen=True)
class Series:
    """A series' identifiers, as the site's structured hit data reports them.

    Which identifiers exist depends on the asset: equities carry ISIN/SEDOL/RIC/local
    code, economics a DS code and LDB, and so on — missing ones are None. `symbol` is
    the site's primary symbol for the series (usually the DS mnemonic, else the DS code).
    """

    series_id: str
    name: str
    symbol: str
    category_id: str | None = None
    category_name: str | None = None
    ds_mnemonic: str | None = None
    ds_code: str | None = None
    ric: str | None = None
    isin: str | None = None
    sedol: str | None = None
    local_code: str | None = None
    t1_code: str | None = None
    ldb: str | None = None
    base_date: dt.date | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.series_id)

    @property
    def mnemonic(self) -> str | None:
        """The DS mnemonic (same as `ds_mnemonic`, and as SeriesDetails.mnemonic)."""
        return self.ds_mnemonic

    @property
    def frequency(self) -> str | None:
        """The frequency code ("D", "M", "Q", ...) where the hit data gives one
        (economics series)."""
        value = self.extra.get("frequency")
        return frequency_code(str(value)) if value else None


@dataclass(frozen=True)
class DatatypeCoverage:
    """A datatype listed in a series' details, with the month its history starts when
    the site gives one (as the first of that month). `mnemonic` and `since` are aliases
    of `code` and `available_from`, matching Datatype."""

    code: str
    name: str
    available_from: dt.date | None = None

    @property
    def mnemonic(self) -> str:
        return self.code

    @property
    def since(self) -> dt.date | None:
        return self.available_from


@dataclass(frozen=True)
class Note:
    """A note on a series: methodology, revisions, source links."""

    title: str
    text: str
    html: str


@dataclass(frozen=True)
class SeriesDetails:
    """The series details panel (what the site shows when you pin a hit).

    symbols:    the identifier block — mnemonic, ric, ldb, t1_code, code, isin, ... —
                keyed by snake_case label
    fields:     every other labelled row (market, source, currency, latest_value,
                timespan, unit, settlement_date, ...), as displayed
    urls:       external links attached to a row, e.g. the source's website
    datatypes:  the datatype rows ("headline_coverage", "ibes_aggregate", ...)
    links:      rows linking to other series: explorers, contains, constituent_of,
                related, derivatives, peers, ...
    chart:      the parameters the site's chart thumbnail is drawn with
    release_frequency: set when the series has a release calendar (client.release_dates)

    Shortcuts for what most people want, the same whatever the category:
        identifiers   mnemonic (= ds_mnemonic), ds_code, ric, isin, sedol, local_code,
                      t1_code, ldb — each None when the series has none
        market        "United Kingdom" (economics series give it with the currency)
        currency      "United Kingdom Pound" (None for exchange rates: see the
                      from_currency / to_currency fields)
        frequency     the code: "D", "W", "M", "Q", ...
        timespan      as the site shows it; start_date / latest_date as dates
        latest_value  the number, e.g. 109.45 (fields["latest_value"] keeps the unit)
    """

    series_id: str
    full_name: str | None
    symbols: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    datatypes: dict[str, list[DatatypeCoverage]] = field(default_factory=dict)
    links: dict[str, list[Link]] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    chart: dict[str, Any] = field(default_factory=dict)
    chart_datatypes: list[str] = field(default_factory=list)
    release_frequency: str | None = None

    def __hash__(self) -> int:
        return hash(self.series_id)

    # --- identifiers, named as on Series ----------------------------------------

    @property
    def mnemonic(self) -> str | None:
        return self.symbols.get("mnemonic")

    @property
    def ds_mnemonic(self) -> str | None:
        return self.mnemonic

    @property
    def ds_code(self) -> str | None:
        return self.symbols.get("code")

    @property
    def ric(self) -> str | None:
        return self.symbols.get("ric")

    @property
    def isin(self) -> str | None:
        return self.symbols.get("isin")

    @property
    def sedol(self) -> str | None:
        return self.symbols.get("sedol")

    @property
    def local_code(self) -> str | None:
        return self.symbols.get("local_code")

    @property
    def t1_code(self) -> str | None:
        return self.symbols.get("t1_code")

    @property
    def ldb(self) -> str | None:
        return self.symbols.get("ldb") or self.fields.get("ldb")

    # --- the usual descriptive fields, normalized across categories ---------------

    @property
    def market(self) -> str | None:
        market, _ = _split_market(self.fields.get("market"))
        return market

    @property
    def currency(self) -> str | None:
        if self.fields.get("currency"):
            return self.fields["currency"]
        _, currency = _split_market(self.fields.get("market"))
        return currency

    @property
    def timespan(self) -> str | None:
        return self.fields.get("timespan")

    @property
    def frequency(self) -> str | None:
        """The frequency code: "D", "W", "M", "Q", ..."""
        code = self.chart.get("frequency") or self.release_frequency
        if code:
            return str(code)
        tail = (self.timespan or "").rpartition(",")[2].strip()  # "Jan 1988 - Oct 2026, Monthly"
        return frequency_code(tail) if tail else None

    @property
    def latest_value(self) -> float | None:
        match = re.match(r"\s*(-?[\d,]*\.?\d+(?:[eE][-+]?\d+)?)", self.fields.get("latest_value") or "")
        return float(match.group(1).replace(",", "")) if match else None

    @property
    def headline_datatypes(self) -> list[DatatypeCoverage]:
        return self.datatypes.get("headline_coverage", [])

    @property
    def start_date(self) -> dt.date | None:
        return _iso_date(self.chart.get("seriesstartdate"))

    @property
    def latest_date(self) -> dt.date | None:
        return _iso_date(self.chart.get("latestvaluedate"))

    def to_dict(self) -> dict[str, Any]:
        """One flat dict, as used for DataFrame rows. Identifier labels that collide
        with a field get a `_symbol` suffix (a future's "Underlying Series" is the
        underlying's mnemonic in the identifier block and its name in the fields)."""
        row: dict[str, Any] = {"series_id": self.series_id, "full_name": self.full_name}
        for key, value in self.fields.items():
            row[key] = value
        for key, value in self.symbols.items():
            if key in row and row[key] != value:
                row[f"{key}_symbol"] = value
            else:
                row[key] = value
        for key, value in self.urls.items():
            row[f"{key}_url"] = value
        for key, coverage in self.datatypes.items():
            row[key] = [d.code for d in coverage]
        for key, links in self.links.items():
            row[key] = [link.label for link in links]
        row["market"] = self.market
        row["currency"] = self.currency
        row["start_date"] = self.start_date
        row["latest_date"] = self.latest_date
        row["frequency"] = self.frequency
        return row


@dataclass(frozen=True)
class Datatype:
    """A datatype available for a series. `since` is when its history starts (None for
    a static datatype, or when the site doesn't say). `code` and `available_from` are
    aliases, matching DatatypeCoverage."""

    mnemonic: str
    name: str
    time_series: bool
    since: dt.date | None
    category_id: str | None = None

    @property
    def code(self) -> str:
        return self.mnemonic

    @property
    def available_from(self) -> dt.date | None:
        return self.since


@dataclass(frozen=True)
class DatatypeDefinition:
    mnemonic: str
    category_id: str
    text: str
    html: str


@dataclass(frozen=True)
class ReleaseDate:
    """An upcoming release of an economic series."""

    date: dt.date | None
    time: str
    period: str


@dataclass(frozen=True)
class TreeNode:
    """A node of the browse tree. Nodes with a `subset` hold series — search them with
    `client.search(subset=node.subset)` — and `size` is how many, when the label says."""

    id: str
    text: str
    parent: str | None
    depth: int
    type: str
    has_children: bool
    subset: str | None
    path: str
    size: int | None = None


@dataclass(frozen=True)
class Category:
    name: str
    id: str
    count: int | None


@dataclass(frozen=True)
class SnapshotColumn:
    label: str
    datatype: str
    """The datatype expression the column holds, e.g. "PI" or "PCH#(X(PI),1Y)"."""


@dataclass(frozen=True)
class SnapshotRow:
    series_id: str
    name: str
    symbol: str
    values: dict[str, float | str | None]
    """{datatype expression: latest value}; numbers as floats, "NA"-style text as is."""


@dataclass(frozen=True)
class Snapshot:
    """The site's values preview: latest values of a set of datatypes for every hit."""

    columns: list[SnapshotColumn]
    rows: list[SnapshotRow]
    total_hits: int


@dataclass(frozen=True)
class SavedList:
    """The result of saving a user list."""

    mnemonic: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


_FREQUENCY_CODES = {
    "daily": "D", "weekly": "W", "monthly": "M", "quarterly": "Q", "annual": "Y", "yearly": "Y",
}


def frequency_code(value: str) -> str:
    """A frequency as its code: "Monthly" -> "M"; a code, or a word without one, as is
    (codes upper-cased)."""
    text = value.strip()
    code = _FREQUENCY_CODES.get(text.lower())
    if code:
        return code
    return text.upper() if len(text) == 1 else text

# economics series give market as "United Kingdom (United Kingdom Pound)"
_MARKET_WITH_CURRENCY = re.compile(r"^(.*?)\s*\(([^()]+)\)$")


def _split_market(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = _MARKET_WITH_CURRENCY.match(value)
    return (match.group(1), match.group(2)) if match else (value, None)


def _iso_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
