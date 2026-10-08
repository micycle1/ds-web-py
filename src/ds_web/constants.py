"""The site's fixed vocabulary: endpoints, categories, filter names and size limits.

Everything here was read off the live site (see docs/site-notes.md for how), not
invented. Where a value can drift, the client has a method that fetches it live —
noted alongside.
"""
from __future__ import annotations

BASE_URL = "https://product.datastream.com"
BROWSE_URL = f"{BASE_URL}/browse/"
LOGIN_URL = f"{BASE_URL}/dsws/1.0/DSLogon.aspx"

# Rows per page of the results grid. The site has no page-size parameter.
PAGE_SIZE = 15

# Hard caps on how many rows one request can return, measured on the live site.
# The HTML grid's "Show all" (page=-1) and the values grid (page=-2) both stop at
# SHOW_ALL_CAP; the JSON hit list (hitdata.aspx) and the file export stop at
# BULK_CAP. Neither cap is reported anywhere — the response is just shorter than
# totalHits — so the client compares against these to tell truncation from a short set.
SHOW_ALL_CAP = 2000
BULK_CAP = 12000

# How many series ids one preview (searchstraggler.aspx) request takes. The site's own
# page sends a whole grid page at once; 200 is comfortably inside what it accepts.
DETAILS_BATCH_SIZE = 200

# Values of the nav_ldbpermission filter: whether this login is licensed for the
# database (LDB) a series lives in. The server matches "NotEntitled" exactly and
# treats *any other string* as Entitled, so always use these constants.
ENTITLED = "Entitled"
NOT_ENTITLED = "NotEntitled"

# Top-level series categories: {sidebar name: nav_category id}. Ids 2 and 10 are
# unused. client.categories() fetches the live list with counts.
CATEGORIES: dict[str, str] = {
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

# Datatype categories for the datatype search realm: {name: subset}. The catalogue is
# the #dtcat <select> of search.aspx?dt=true. "All Datatypes" is the root.
DATATYPE_CATEGORIES: dict[str, str] = {
    "All Datatypes": "dtx1|0001_0001",
    "Bonds & Convertibles": "dtx1|0001_0001_0001",
    "Bond Indices": "dtx1|0001_0001_0002",
    "Credit Default Swaps": "dtx1|0001_0001_0003",
    "Commodities": "dtx1|0001_0001_0004",
    "Economics": "dtx1|0001_0001_0005",
    "Equities": "dtx1|0001_0001_0006",
    "Equity Indices": "dtx1|0001_0001_0007",
    "Exchange Rates": "dtx1|0001_0001_0008",
    "Futures": "dtx1|0001_0001_0009",
    "Interest Rates": "dtx1|0001_0001_0010",
    "Investment Trusts": "dtx1|0001_0001_0011",
    "Options": "dtx1|0001_0001_0012",
    "Funds": "dtx1|0001_0001_0013",
    "User Created Indices": "dtx1|0001_0001_0014",
    "Warrants": "dtx1|0001_0001_0015",
    "User Created Time Series": "dtx1|0001_0001_0016",
}

# The nav_* filters the "Add Filters" sidebar offers somewhere. Which ones a given
# query offers depends on its category and on the filters already applied —
# client.filters(query) returns the live set. A filter a query doesn't offer is
# ignored by the site rather than rejected, so this list exists to catch typos;
# pass an uncatalogued one with its nav_ prefix to skip the check.
NAV_FILTERS: tuple[str, ...] = (
    "accesstype", "activity", "additionalfilters", "adjustment", "assettype",
    "borrower", "category", "coupon", "coupontype", "currency", "datatype",
    "econactivity", "endyear", "exchange", "forecast", "frequency", "fromcurrency",
    "hasric", "highrank", "incomedistribution", "industrygroup", "issuertype", "ldb",
    "ldbpermission", "localcode", "market", "medrank", "priceunit", "region",
    "restriction", "sector", "source", "startyear", "type", "underlying", "unit",
)

# Most values one multi-value filter accepts (the site's own popup enforces this).
MAX_FILTER_VALUES = 25
