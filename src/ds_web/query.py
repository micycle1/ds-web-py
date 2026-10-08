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
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Union
from urllib.parse import parse_qsl, urlencode, urlsplit

from ._util import warn
from .constants import (
    CATEGORIES,
    ENTITLED,
    MAX_FILTER_VALUES,
    NAV_FILTERS,
    NOT_ENTITLED,
)

if TYPE_CHECKING:
    from .models import Link, TreeNode


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
            values = tuple(sorted(values[0], key=str) if isinstance(values[0], (set, frozenset)) else values[0])
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

    nav_category only addresses top-level categories. The site reduces a tree node id to
    its prefix without a warning ("12-4428" searches all of Economics). The library does
    the same reduction and shows a warning, so you know the drill-down did not happen.
    To search inside a node, use the node's subset (TreeNode.subset).
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
        warn(
            f"category={text!r} is a tree node id, but the category filter only addresses "
            f"top-level categories, so this searches all of category {prefix!r}. To search "
            f"inside the node, pass its subset (TreeNode.subset) as subset=."
        )
    return prefix


def _encode_filter(name: str, value: FilterValue) -> str:
    exclude = isinstance(value, Exclude)
    values: list[Any]
    if isinstance(value, Exclude):
        values = list(value.values)
    elif isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, (set, frozenset)):
        values = sorted(value, key=str)  # a stable order keeps equality and refs stable
    elif isinstance(value, Iterable):
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


def _entitled_from_permission(value: Any) -> bool | None:
    """An ldbpermission filter value as `entitled`. The site treats every string except
    NotEntitled as Entitled, so this does too."""
    if value is None or isinstance(value, bool):
        return value
    return str(value) != NOT_ENTITLED


def _merge_filters(
    encoded: dict[str, str], entitled: Any, changes: dict[str, Any]
) -> tuple[dict[str, str], Any]:
    """Apply filter keyword arguments to already-encoded filters: check the names,
    encode the values, drop the ones set to None, and route ldbpermission to entitled."""
    result = dict(encoded)
    for key, value in changes.items():
        explicit = key.startswith("nav_")
        name = key.removeprefix("nav_")
        if name == "ldbpermission":
            entitled = _entitled_from_permission(value)
            continue
        param = f"nav_{name}"
        if value is None:
            result.pop(param, None)
            continue
        # an uncatalogued name passes with the nav_ prefix, or when the query already
        # carries it (it came from the site, or was prefixed when first set)
        if name not in NAV_FILTERS and not explicit and param not in encoded:
            raise TypeError(
                f"unknown filter {key!r}; expected one of {', '.join(NAV_FILTERS)}. "
                f"Pass it as nav_{name}= to send it anyway."
            )
        result[param] = _encode_filter(name, value)
    return result, entitled


def _check_term(term: Any) -> None:
    if term is not None and not isinstance(term, str):
        raise TypeError(f"term must be a string, not {type(term).__name__}")


class Query:
    """What to search for. Immutable; derive variants with `replace()`.

    term:      free text, which may include criteria (see `criteria()`)
    subset:    restrict to a browse-tree node, list or relationship — a TreeNode.subset,
               a Link.subset from series details, or `client.combine_explorers()`
    sort:      a sort code (SearchPage.sort_options lists the ones a grid offers);
               sorting needs every hit on one page, so a sorted search is fetched whole
    entitled:  True = only series this login can pull data for, False = only those it
               can't, None = no entitlement filter; left alone, the client's setting.
               An `ldbpermission` filter, as the sidebar offers it, sets this too.
    **filters: the sidebar's nav_* filters (see NAV_FILTERS), by bare name or with the
               nav_ prefix — which also skips the name check, for filters NAV_FILTERS
               doesn't list. A value may be a string, a list (any of), or Exclude(...).
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
        _check_term(term)
        encoded, entitled = _merge_filters({}, entitled, filters)
        self._init(term, subset, sort, entitled, encoded)

    def _init(self, term: Any, subset: Any, sort: Any, entitled: Any, encoded: dict[str, str]) -> None:
        object.__setattr__(self, "term", term)
        object.__setattr__(self, "subset", subset)
        object.__setattr__(self, "sort", sort)
        object.__setattr__(self, "entitled", entitled)
        object.__setattr__(self, "_filters", encoded)

    @classmethod
    def _raw(
        cls,
        term: str | None = None,
        subset: str | None = None,
        sort: str | None = None,
        entitled: Any = DEFAULT,
        encoded: dict[str, str] | None = None,
    ) -> Query:
        """A Query from filters already in the site's encoding — from a URL, a link in a
        response, or a pickle — taken as they are."""
        query = cls.__new__(cls)
        query._init(term, subset, sort, entitled, dict(encoded or {}))
        return query

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Query is immutable; use replace()")

    def __reduce__(self) -> tuple[Any, ...]:
        # default slots pickling (and copy) restores state through __setattr__, which refuses
        return (_restore_query, (self.term, self.subset, self.sort, self.entitled, dict(self._filters)))

    @property
    def filters(self) -> dict[str, FilterValue]:
        """The filters as given (category resolved to its id), keyed by bare name."""
        return {k.removeprefix("nav_"): _decode_filter(v) for k, v in self._filters.items()}

    def replace(self, **changes: Any) -> Query:
        """A copy with some fields changed. Filters merge into the existing ones; set a
        filter to None to drop it."""
        fields: dict[str, Any] = {"term": self.term, "subset": self.subset, "sort": self.sort}
        entitled = changes.pop("entitled", self.entitled)
        filters = {}
        for key, value in changes.items():
            if key in fields:
                fields[key] = value
            else:
                filters[key] = value
        _check_term(fields["term"])
        encoded, entitled = _merge_filters(self._filters, entitled, filters)
        return Query._raw(fields["term"], fields["subset"], fields["sort"], entitled, encoded)

    def is_empty(self) -> bool:
        """True when the query selects nothing in particular (entitlement aside)."""
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
        `searchref=` is decoded; anything unrecognised is dropped with a warning.
        Filter values are kept exactly as the URL encodes them."""
        query_string = urlsplit(url).query or url.lstrip("?")
        pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(query_string, keep_blank_values=True):
            if key == "searchref":
                # a reference is itself an encoded query string: splice it in
                pairs.extend(parse_qsl(_decode_ref(value), keep_blank_values=True))
            else:
                pairs.append((key, value))

        fields: dict[str, Any] = {"term": None, "subset": None, "sort": None, "entitled": DEFAULT}
        encoded: dict[str, str] = {}
        for key, value in pairs:
            if key in _URL_BOOKKEEPING:
                continue
            elif key == "q":
                fields["term"] = value
            elif key == "subset":
                fields["subset"] = value or None
            elif key == "s":
                fields["sort"] = value or None
            elif key == "nav_ldbpermission":
                if value:
                    fields["entitled"] = _entitled_from_permission(value)
            elif key.startswith("nav_"):
                if value:
                    encoded[key] = value
            else:
                warn(f"Query.from_url: ignoring unrecognised parameter {key}={value!r}")
        return cls._raw(encoded=encoded, **fields)

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


def _restore_query(term: Any, subset: Any, sort: Any, entitled: Any, encoded: dict[str, str]) -> Query:
    return Query._raw(term, subset, sort, entitled, encoded)


def encode_ref(params: dict[str, str]) -> str:
    """search.aspx parameters as a search reference (base64 of the query string)."""
    return base64.b64encode(urlencode(sorted(params.items())).encode()).decode()


def _decode_ref(ref: str) -> str:
    # a ref read out of a query string has had its "+"s turned into spaces
    text = ref.replace(" ", "+").strip()
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


QueryLike = Union[Query, str, None, "Link", "TreeNode"]


def as_query(query: QueryLike = None, **kwargs: Any) -> Query:
    """What client methods accept as a query: a Query, a search term, or something that
    names a set of series — a Link from a details panel, or a TreeNode — plus keyword
    arguments to build or adjust it."""
    from .models import Link, TreeNode

    if isinstance(query, Query):
        return query.replace(**kwargs) if kwargs else query
    if isinstance(query, Link):
        return query.query(**kwargs)
    if isinstance(query, TreeNode):
        if not query.subset:
            raise ValueError(f"tree node {query.id} ({query.text!r}) holds no series itself; search its children")
        return Query(subset=query.subset, **kwargs)
    if query is not None and not isinstance(query, str):
        raise TypeError(f"expected a Query, search term, Link or TreeNode, not {type(query).__name__}")
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
