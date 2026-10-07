"""Building search requests: `Query`, filter values, search references and criteria.

A Query is everything that decides *which* series a search returns — text, browse
filters, a tree subset, sort order, entitlement — independent of how the hits are then
fetched (a page, every hit, an export, a values grid). Every client method that runs a
search takes one, or the same arguments spelled inline:

    q = Query("sugar", category="Futures", exchange=["ICE Futures U.S."])
    client.search(q)
    client.search("sugar", category="Futures")     # same thing
    client.search_all(q.replace(sort="N"))
"""
from __future__ import annotations

import base64
import re
import warnings
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .constants import (
    CATEGORIES,
    ENTITLED,
    MAX_FILTER_VALUES,
    NAV_FILTERS,
    NOT_ENTITLED,
)


class _Default:
    """Sentinel: "use the client's setting" (for `entitled`)."""

    _instance: _Default | None = None

    def __new__(cls) -> _Default:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DEFAULT"

    def __reduce__(self) -> str:
        return "DEFAULT"


DEFAULT: Any = _Default()


class Exclude:
    """A filter value that removes series instead of selecting them — the sidebar's
    "Exclude Selected":

        Query("bank", category="Equities", market=Exclude("United States", "Japan"))
    """

    __slots__ = ("values",)

    def __init__(self, *values: Any):
        if len(values) == 1 and isinstance(values[0], (list, tuple, set, frozenset)):
            values = tuple(values[0])
        if not values:
            raise ValueError("Exclude() needs at least one value")
        self.values = tuple(str(v) for v in values)

    def __repr__(self) -> str:
        return f"Exclude({', '.join(map(repr, self.values))})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Exclude) and other.values == self.values

    def __hash__(self) -> int:
        return hash(("Exclude", self.values))


FilterValue = str | int | Iterable[Any] | Exclude | None


def _normalize_category_name(name: str) -> str:
    """Fold a category name to a match key: "Bonds & Convertibles", "bonds and
    convertibles" and "Bonds&Convertibles" are the same key."""
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


_CATEGORIES_BY_KEY = {_normalize_category_name(k): v for k, v in CATEGORIES.items()}


def resolve_category(value: Any) -> str:
    """Resolve a category name or id to the numeric id nav_category takes.

    nav_category only addresses top-level categories, and the site silently reduces a
    tree node id to its prefix ("12-4428" searches all of Economics). That reduction is
    done here with a warning, so a drill-down that isn't happening can't pass for one —
    use the node's subset (TreeNode.subset) to search inside it.
    """
    text = str(value).strip()
    if not re.fullmatch(r"\d+(-\d+)*", text):
        resolved = _CATEGORIES_BY_KEY.get(_normalize_category_name(text))
        if resolved is None:
            raise ValueError(
                f"unknown category {value!r}; pass a numeric id or one of: "
                f"{', '.join(sorted(CATEGORIES))}"
            )
        return resolved
    prefix = text.split("-")[0]
    if prefix != text:
        warnings.warn(
            f"category={text!r} is a tree node id, but the category filter only addresses "
            f"top-level categories, so this searches all of category {prefix!r}. To search "
            f"inside the node, pass its subset (TreeNode.subset) as subset=.",
            stacklevel=4,
        )
    return prefix


def _encode_filter(name: str, value: FilterValue) -> str:
    exclude = isinstance(value, Exclude)
    if exclude:
        values: list[Any] = list(value.values)  # type: ignore[union-attr]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = [value]
    if not values:
        raise ValueError(f"filter {name!r} was given no values")
    if len(values) > MAX_FILTER_VALUES:
        raise ValueError(
            f"filter {name!r} has {len(values)} values; the site accepts at most "
            f"{MAX_FILTER_VALUES}"
        )
    if name == "category":
        values = [resolve_category(v) for v in values]
    return ("-" if exclude else "") + "|".join(str(v) for v in values)


def _decode_filter(raw: str) -> FilterValue:
    exclude = raw.startswith("-")
    values = (raw[1:] if exclude else raw).split("|")
    if exclude:
        return Exclude(*values)
    return values if len(values) > 1 else values[0]


class Query:
    """What to search for. Immutable; derive variants with `replace()`.

    term:      free text, which may include criteria (see `criteria()`)
    subset:    restrict to a browse-tree node, list or relationship — a TreeNode.subset,
               a Link.subset from series details, or `client.combine_explorers()`
    sort:      a sort code (SearchPage.sort_options lists the ones a grid offers);
               sorting needs every hit on one page, so a sorted search is fetched whole
    entitled:  True = only series this login can pull data for, False = only those it
               can't, None = no entitlement filter; left alone, the client's setting
    **filters: the sidebar's nav_* filters (see NAV_FILTERS), by bare name or with the
               nav_ prefix. A value may be a string, a list (any of), or Exclude(...).
               `category` also accepts sidebar names ("Futures", "Bond Indices").
    """

    __slots__ = ("_filters", "entitled", "sort", "subset", "term")

    term: str | None
    subset: str | None
    sort: str | None
    entitled: bool | None
    _filters: dict[str, str]

    def __init__(
        self,
        term: str | None = None,
        *,
        subset: str | None = None,
        sort: str | None = None,
        entitled: bool | None = DEFAULT,
        **filters: FilterValue,
    ):
        encoded: dict[str, str] = {}
        for key, value in filters.items():
            explicit = key.startswith("nav_")
            name = key.removeprefix("nav_")
            if name == "ldbpermission":
                raise TypeError("use entitled=True/False/None instead of an ldbpermission filter")
            if name not in NAV_FILTERS and not explicit:
                raise TypeError(
                    f"unknown filter {key!r}; expected one of {', '.join(NAV_FILTERS)}. "
                    f"Pass it as nav_{name}= to send it anyway."
                )
            if value is None:
                continue
            encoded[f"nav_{name}"] = _encode_filter(name, value)
        object.__setattr__(self, "term", term)
        object.__setattr__(self, "subset", subset)
        object.__setattr__(self, "sort", sort)
        object.__setattr__(self, "entitled", entitled)
        object.__setattr__(self, "_filters", encoded)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Query is immutable; use replace()")

    @property
    def filters(self) -> dict[str, FilterValue]:
        """The filters as given (category resolved to its id), keyed by bare name."""
        return {k.removeprefix("nav_"): _decode_filter(v) for k, v in self._filters.items()}

    def replace(self, **changes: Any) -> Query:
        """A copy with some fields changed. Filters merge into the existing ones; set a
        filter to None to drop it."""
        filters: dict[str, Any] = self.filters
        fields: dict[str, Any] = {
            "term": self.term,
            "subset": self.subset,
            "sort": self.sort,
            "entitled": self.entitled,
        }
        for key, value in changes.items():
            if key in fields:
                fields[key] = value
            else:
                filters[key.removeprefix("nav_")] = value
        return Query(**fields, **filters)

    def is_empty(self) -> bool:
        return self.term is None and self.subset is None and not self._filters

    def to_params(self, entitled_only: bool | None) -> dict[str, str]:
        """The search.aspx query parameters for this query (paging excluded).
        `entitled_only` is the client's setting, used when this query leaves `entitled`
        alone: True filters to entitled series, False/None doesn't filter."""
        params: dict[str, str] = dict(self._filters)
        entitled = (True if entitled_only else None) if self.entitled is DEFAULT else self.entitled
        if entitled is True:
            params["nav_ldbpermission"] = ENTITLED
        elif entitled is False:
            params["nav_ldbpermission"] = NOT_ENTITLED
        if self.term is not None:
            params["q"] = self.term
        if self.subset is not None:
            params["subset"] = self.subset
        if self.sort is not None:
            params["s"] = self.sort
        return params

    # --- search references (the site's permalinks) -------------------------------

    def to_ref(self) -> str:
        """This query as a Navigator search reference — the base64 string the site's
        "Search Reference" box shows, which can be pasted back into the site.

        Entitlement is included only if set explicitly on this query."""
        return encode_ref(self.to_params(entitled_only=None))

    @classmethod
    def from_ref(cls, ref: str) -> Query:
        """Decode a search reference (from the site, or `to_ref()`)."""
        return cls.from_url("?" + _decode_ref(ref))

    @classmethod
    def from_url(cls, url: str) -> Query:
        """Parse a Navigator search.aspx URL copied out of a browser (or just its query
        string). Session bookkeeping (dsid, AppGroup, prev, ...) is dropped; a
        `searchref=` is decoded; anything unrecognised is dropped with a warning."""
        query_string = urlsplit(url).query or url.lstrip("?")
        pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(query_string, keep_blank_values=True):
            if key == "searchref":
                # a reference is itself an encoded query string: splice it in
                pairs.extend(parse_qsl(_decode_ref(value), keep_blank_values=True))
            else:
                pairs.append((key, value))

        fields: dict[str, Any] = {}
        filters: dict[str, str] = {}
        for key, value in pairs:
            if key in _URL_BOOKKEEPING:
                continue
            elif key == "q":
                fields["term"] = value
            elif key == "subset":
                fields["subset"] = value
            elif key == "s":
                fields["sort"] = value or None
            elif key == "nav_ldbpermission":
                fields["entitled"] = value == ENTITLED
            elif key.startswith("nav_"):
                filters[key] = value
            else:
                warnings.warn(f"Query.from_url: ignoring unrecognised parameter {key}={value!r}", stacklevel=2)
        decoded: dict[str, Any] = {k: _decode_filter(v) for k, v in filters.items() if v != ""}
        return cls(**fields, **decoded)

    # --- value semantics ---------------------------------------------------------

    def _key(self) -> tuple[Any, ...]:
        return (self.term, self.subset, self.sort, self.entitled, tuple(sorted(self._filters.items())))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Query) and other._key() == self._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        parts = [repr(self.term)] if self.term is not None else []
        for name in ("subset", "sort", "entitled"):
            value = getattr(self, name)
            if value is not DEFAULT and (value is not None or name == "entitled"):
                parts.append(f"{name}={value!r}")
        parts += [f"{k}={v!r}" for k, v in self.filters.items()]
        return f"Query({', '.join(parts)})"


def encode_ref(params: dict[str, str]) -> str:
    """search.aspx parameters as a search reference (base64 of the query string)."""
    return base64.b64encode(urlencode(sorted(params.items())).encode()).decode()


def _decode_ref(ref: str) -> str:
    text = ref.strip()
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode()
    except ValueError as exc:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise ValueError(f"not a search reference: {ref!r}") from exc


# params of a copied URL that are session/UI state rather than part of the search
_URL_BOOKKEEPING = frozenset({
    "dsid", "AppGroup", "prev", "page", "explorerMode", "treesearch", "allowduplicates",
    "host", "SymbolPref", "forcedomain", "debug", "selectDatatypes", "multiSelect",
    "isGrouped", "isShortdesc", "isLongname", "TSDatatypesOnly", "dt", "nova", "l",
    "outboundChannel", "version", "pattern", "exportToExcel", "dforic",
})


QueryLike = Query | str | None


def as_query(query: QueryLike = None, **kwargs: Any) -> Query:
    """Accept a Query, or the arguments to build one, from a client method."""
    if isinstance(query, Query):
        return query.replace(**kwargs) if kwargs else query
    return Query(query, **kwargs)


# --- criteria: the site's fielded search syntax --------------------------------------

# Fields the text box understands as FIELD:op(values). DESC is the name; the rest are
# identifiers. Criteria select series directly, so a query that is *only* criteria
# ignores the sidebar filters (entitlement included).
CRITERIA_FIELDS = ("DESC", "DSCD", "DSSRC", "MNEM", "SRMNEM", "SEDOL", "ISIN", "LOC", "RIC")

_CRITERIA_OPS = {"any": "or", "all": "and", "none": "not"}


def criteria(field: str, values: str | Iterable[str], match: str = "any") -> str:
    """Build a fielded search term, for use as (part of) a Query's term:

        criteria("ISIN", ["GB00BH4HKS39", "US0378331005"])   # 'ISIN:or(GB00BH4HKS39,US0378331005)'
        criteria("MNEM", "VOD*")                             # mnemonics starting VOD
        criteria("DESC", ["*sugar*", "*brazil*"], match="all")

    `match` is "any", "all" or "none". Values may use * as a wildcard at either end.
    """
    field = field.upper()
    if field not in CRITERIA_FIELDS:
        raise ValueError(f"unknown criteria field {field!r}; expected one of {', '.join(CRITERIA_FIELDS)}")
    if match not in _CRITERIA_OPS:
        raise ValueError(f"match must be one of {', '.join(_CRITERIA_OPS)}, not {match!r}")
    items = [values] if isinstance(values, str) else list(values)
    if not items:
        raise ValueError("criteria() needs at least one value")
    encoded = ",".join(str(v).strip().strip('"').replace(",", "%2C") for v in items)
    return f"{field}:{_CRITERIA_OPS[match]}({encoded})"
