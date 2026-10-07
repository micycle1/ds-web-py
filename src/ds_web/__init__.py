"""Unofficial client for the Datastream Navigator web app (product.datastream.com/browse):
search, browse, look up and describe Datastream series and datatypes — the discovery side
that the official Datastream API (DatastreamPy) doesn't expose.

    from ds_web import DatastreamWebClient, Query

    with DatastreamWebClient("USER", "PASSWORD") as ds:
        page = ds.search("sugar", category="Futures")
        ds.search_all(Query("uk cpi", category="Economics"))
        ds.resolve(["VOD", "GB00BH4HKS39", "TRUK10T"])
        ds.details("173737703").fields["latest_value"]
"""
from .client import ALL, DatastreamWebClient
from .constants import (
    BULK_CAP,
    CATEGORIES,
    DATATYPE_CATEGORIES,
    ENTITLED,
    NAV_FILTERS,
    NOT_ENTITLED,
    PAGE_SIZE,
    SHOW_ALL_CAP,
)
from .errors import (
    DatastreamWebError,
    LoginError,
    ParseError,
    ResultSetTooLargeError,
    ServerError,
    TruncatedResultsWarning,
)
from .frames import to_frame
from .models import (
    Category,
    Datatype,
    DatatypeCoverage,
    DatatypeDefinition,
    FilterOption,
    Link,
    Note,
    ReleaseDate,
    SavedList,
    SearchHit,
    SearchPage,
    Series,
    SeriesDetails,
    Snapshot,
    SnapshotColumn,
    SnapshotRow,
    TreeNode,
)
from .query import CRITERIA_FIELDS, Exclude, Query, criteria

__version__ = "0.2.0"

__all__ = [
    "ALL",
    "BULK_CAP",
    "CATEGORIES",
    "CRITERIA_FIELDS",
    "DATATYPE_CATEGORIES",
    "ENTITLED",
    "NAV_FILTERS",
    "NOT_ENTITLED",
    "PAGE_SIZE",
    "SHOW_ALL_CAP",
    "Category",
    "DatastreamWebClient",
    "DatastreamWebError",
    "Datatype",
    "DatatypeCoverage",
    "DatatypeDefinition",
    "Exclude",
    "FilterOption",
    "Link",
    "LoginError",
    "Note",
    "ParseError",
    "Query",
    "ReleaseDate",
    "ResultSetTooLargeError",
    "SavedList",
    "SearchHit",
    "SearchPage",
    "Series",
    "SeriesDetails",
    "ServerError",
    "Snapshot",
    "SnapshotColumn",
    "SnapshotRow",
    "TreeNode",
    "TruncatedResultsWarning",
    "criteria",
    "to_frame",
]
