# ds-web

An unofficial Python client for the **Datastream Navigator** web app
(`product.datastream.com/browse`). It covers the discovery side of Datastream that the
official API (DatastreamPy) doesn't expose:

- searching and browsing series
- resolving identifiers
- reading series metadata, datatypes, notes and release calendars
- listing index constituents
- exporting result sets

Once you know the mnemonics, fetch the data itself with DatastreamPy.

```python
from ds_web import DatastreamWebClient, Query, Exclude

with DatastreamWebClient("USERNAME", "PASSWORD") as ds:
    page = ds.search("sugar", category="Futures", exchange=Exclude("ICE Futures U.S."))
    page.total_hits, page.hits[0].symbol

    ds.resolve(["VOD", "GB00BH4HKS39", "TRUK10T"])   # any identifier -> Series
    ds.constituents("LFTSE100")                      # index/list members, with ISINs
    ds.details(ds.lookup("UKCONPRCF").series_id)     # the series details panel
    ds.search_frame(Query("uk cpi", category="Economics"))   # a DataFrame
```

## Install

```bash
pip install -e ".[pandas]"
```

The `pandas` extra is only needed for DataFrame output (`search_frame`, `to_frame`).
Python 3.10+.

Credentials can be passed in, or read from `DS_WEB_USERNAME` and `DS_WEB_PASSWORD`.
The client signs in on the first request. It signs in again automatically when the
session expires. The client is thread-safe.

## Concepts

**Queries.** Everything that selects series is a `Query`: free text, sidebar filters, a
browse-tree subset, a sort order and an entitlement setting. Every search method takes
either a `Query` or the same arguments inline.

```python
q = Query("gold", category="Equities", market=["United States", "Canada"], sort="N")
ds.search(q)
ds.search_all(q.replace(market=None))   # derive variants; None drops a filter
Query.from_url("https://product.datastream.com/browse/search.aspx?q=gold&nav_category=0")
Query.from_ref("cT1zdWdhcg==")          # the site's "search reference" permalinks
```

- **Filters** use the sidebar's names (`ds_web.NAV_FILTERS`). `category` also accepts
  names such as `"Futures"` or `"Bond Indices"`.
- A list means "any of" these values. `Exclude(...)` removes the values instead.
- `ds.filters(query)` lists what the sidebar offers for a query, with counts. Apply
  an option with `query.replace(**{opt.param: opt.value})`.
- **Fielded search** goes through `criteria()`:
  `Query(criteria("ISIN", ["GB00BH4HKS39", "US0378331005"]))`, or
  `criteria("MNEM", "VOD*")` for a wildcard.

**Entitlement.** By default every search is limited to series your login can pull data
for, using the site's LDB-permission flag. Change this per client with
`DatastreamWebClient(entitled_only=False)`, or per query:

- `entitled=False` returns only the series you cannot access
- `entitled=None` turns the filter off

Lookups by symbol (`resolve`, `lookup`, `series`) are never filtered.
`ds.entitlement("MLGCORL")` asks about one series.

**Result-set size.** The site limits the number of rows it returns. You cannot use paging
to get more rows than the limit: tied hits change order between requests, so a page walk
repeats some rows and misses others. The library therefore never walks pages.

| Method | Returns | Cap |
|---|---|---|
| `search(q)` | one page of the grid | 15 rows |
| `search(q, page=ALL)` | the grid, all on one page | 2,000 rows (warns beyond) |
| `search_all(q)` | `Series` with identifiers, rank order | 12,000 (raises beyond, unless `limit=`) |
| `search_frame(q)` | DataFrame: identifiers + grid columns + full name | 12,000 |
| `export(q, format=...)` | the site's CSV/XLSX export | 12,000 |
| `snapshot(q)` | latest values grid | 2,000 rows (warns beyond) |

## What it can do

| Area | Methods |
|---|---|
| Search | `search`, `count`, `filters`, `categories`, `search_all`, `search_frame`, `export`, `snapshot` (latest values and % changes for every hit), `search_ref`, `describe` |
| Symbols | `resolve` (mnemonic, DS code, RIC, ISIN, SEDOL, local code → `Series`), `lookup`, `series` (by id), `entitlement` |
| Series | `details` / `details_many` (full name, identifiers, latest value, timespan, category-specific fields, headline datatypes, notes, explorer and relationship links), `datatypes` (every datatype with its start date), `notes`, `release_dates`, `chart` (PNG), `constituents` |
| Datatypes | `search_datatypes`, `lookup_datatypes`, `datatype_definition`, `datatype_categories` |
| Browse tree | `tree` (walk to any depth), `tree_search` (find nodes by name), `combine_explorers` (one subset spanning several nodes) |
| User data | `save_list` (create or overwrite an `L#` list), `refresh_user_data` |

Results are typed dataclasses (`SearchHit`, `Series`, `SeriesDetails`, `Datatype`,
`TreeNode`, …). `ds_web.to_frame(results)` turns any list of them (or the dicts that `resolve` and
`details_many` return) into a DataFrame.
Links in details (`d.links["contains"]`, `d.links["derivatives"]`, explorer links) and
tree nodes can be given directly to any search method: `ds.search(link)`,
`ds.search_all(node)`.

Commitments of Traders series record their asset only in the series name.
`ds_web.cot.parse_cot_name()` extracts it.

`docs/site-notes.md` describes how the site works internally (limits, unusual behavior,
the entitlement evidence) and which site features the library does not include.

## Tests

```bash
pytest                                     # offline, against saved responses in tests/fixtures
DS_WEB_USERNAME=... DS_WEB_PASSWORD=... pytest -m live   # against the real site
```

The live tests verify the limits, parameter names and response formats that the library
uses. If a live test fails, the site probably changed.
