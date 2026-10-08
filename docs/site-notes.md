# How the Navigator site works

These notes describe `https://product.datastream.com/browse/` (Navigator 4.19).
Maintainers can use them when the site changes. All data here was
**measured on the live site** unless the text says otherwise. Code comments describe
what the library does. This file gives the reasons and the evidence.

The site is a server-rendered ASP.NET WebForms app with jQuery on top. There is no JSON
API as such: the results grid is HTML embedded in `search.aspx`, and the popups are AJAX
calls returning HTML fragments or JSON. The site's own JavaScript (`static/js/search.js`,
`common.js`, `static/tree/explorertree.js`) defines the site behavior. If the library
fails, download the JavaScript and read it.

## Endpoints

| Endpoint | Method | Used by | Returns |
|---|---|---|---|
| `/dsws/1.0/DSLogon.aspx` | GET + POST | sign-in (WebForms viewstate form) | redirect to `search.aspx` |
| `search.aspx` | GET | `search`, `count`, `filters`, `categories`, `snapshot`, `search_datatypes`, `datatype_categories` | full HTML page |
| `hitdata.aspx` | POST | `search_all`, `series` | JSON hit objects |
| `resolveLegacySelections.aspx` | POST | `resolve`, `lookup` | JSON hit objects |
| `expandmnemonics.aspx` | POST | `constituents` | `{constituents: [...], error?}` |
| `searchstraggler.aspx` | POST | `details`, `details_many` | HTML fragment per series |
| `moredatatypes.aspx` | GET | `datatypes` | HTML table |
| `datatypedefinition.aspx` | POST | `datatype_definition` | HTML (help inside `input.dthelp@value`) |
| `notes.aspx` | POST | `notes` | HTML fragment |
| `ndor.aspx` | POST | `release_dates` | HTML table |
| `thumbnail.aspx` | GET | `chart` | PNG |
| `excelexport.aspx` | GET | `export`, `search_frame` | CSV / XLSX / XLS file |
| `explorerleaves.aspx` | GET | `tree`, `tree_search` | JSON (jstree nodes) |
| `multiexplorerencoding.aspx` | GET | `combine_explorers` | a JSON *string* (`"mex1|a|b"`) |
| `recentsearches.aspx` | POST | `describe` | HTML links |
| `usercreateddata.aspx` | POST | `save_list`, `refresh_user_data` | `{isError, message, overwrite?, lstdetails?}` |

### Not implemented, on purpose

- `deleteuserentity.aspx`: deletes user-created series. It is destructive, and the
  site's own UI gates it behind a confirm. Use the site.
- `selectionusage.aspx`, `logthis.aspx`: usage telemetry and support logging.
- `t1storechart.aspx`, `dfochartdatarequest.aspx`: these hand charts to the Thomson ONE
  and Excel add-in hosts, so they only make sense inside those hosts.
- `columnexplorer.aspx`: the column-view rendering of the same browse tree that
  `explorerleaves.aspx` serves as JSON. `tree()` covers it.
- `novaimage.aspx`: an image proxy for the dark theme.
- `uci/createindices.aspx` (Save As UCI): starts the separate User Created Indices
  wizard app.
- The "My Selections" basket: client-side state in the browser. `save_list()` is the
  part that reaches the server.

## Parameter conventions

The page holds a form, `#inputs`, with three fieldsets: identity (`dsid`, `AppGroup`),
mode (`host`, `isLongname`, `dt`, …) and query (`q`, `subset`, `nav_*`, `s`, `page`, …).
The JS serializes it four different ways depending on the endpoint, and some endpoints
care. `Session.params()` reproduces the variants:

- **page**: `search.aspx` and `excelexport.aspx`. Identity, then non-empty inputs.
- **mode**: popups and lookups. Identity plus *every* mode field, empty or not.
- **progress**: user-data endpoints. Identity plus `host` only.
- **none**: explorer endpoints. The session cookie alone.

### The `host` parameter enables structured data

`hitdata.aspx` and `resolveLegacySelections.aspx` return `[{}, {}, ...]`, one empty
object per hit, to the standalone site, whose JS never reads them. They fill the objects
in only when `host` names a host application. `host=unity` returns the most identifiers:
DS mnemonic, DS code, RIC, ISIN, SEDOL, local code, T1 code and LDB. `dfo`, `afo` and
`advance` return fewer. `multiSelect=true` alone does nothing.

`resolveLegacySelections.aspx` without a host answers HTTP 500.

## Size limits (none of them reported by the site)

| What | Cap | Evidence |
|---|---|---|
| `search.aspx?page=-1` ("Show all") | **2,000** rows | `gold` in Equities: 4,145 hits, 2,000 rows |
| `search.aspx?page=-2` (values preview) | 2,000 rows | `FTSE` in Equity Indices: 60,370 hits, 2,000 rows |
| `hitdata.aspx` (all hits) | **12,000** | `sugar`: 50,877 hits, 12,000 returned, all distinct |
| `excelexport.aspx` | 12,000 | same query, 12,000 rows |
| `searchstraggler.aspx` ids per request | ≥ 200 | 200 previews in 1.6 s |
| filter values per `nav_*` | 25 | enforced by the site's JS |

The library before 0.2 assumed "Show all" worked up to 5,000. Result sets of 2,001–5,000
hits were truncated to 2,000 without a warning. The cause of the missing rows was
wrongly given as paging.

## Paging is not a partition

Hits are ranked by relevance with no tiebreak, so a block of equally-scoring series comes
back in a different order on every request, and page boundaries slide through it. Walking
pages 1..N therefore both repeats rows and skips others. Measured on `sugar` + NYMEX:
101 hits, a serial 7-page walk returned 84 distinct series (17 seen twice, 17 never), while
"Show all" returned all 101 exactly once. Running the walk serially doesn't help. The
library never walks pages. It uses the bulk endpoints and refuses (`ResultSetTooLargeError`)
when even those can't cover the query.

Joining two bulk responses for the same query (hit data and export) by symbol is exact
when the set fits under the cap. At the cap, tie-shuffling changes *which* 12,000 come
back: ~180 of 12,000 differed between the two requests for `sugar`.

## Entitlement (`nav_ldbpermission`)

Datastream licenses data per LDB ("local database", the 3-character code in a series'
details). Every series lives in one LDB, so entitlement is really a property of the
database. `nav_ldbpermission` filters on it:

- `NotEntitled` selects the blocked half. **Any other string**, including
  `notentitled`, `Not Entitled` or a typo, behaves like `Entitled`. A misspelling
  applies the Entitled filter and gives no error. For this reason the library uses
  `entitled=True/False/None` and not the raw value.
- On the account the library was developed against, the default hid ~71k of 91k Bond
  Indices, ~227k of 400k Equity Indices and 514k Economics series, and nothing in
  Equities, Funds, Futures, Options, Warrants, Bonds & Convertibles, Constituent Lists,
  Investment Trusts or CDS.
- The filter was checked against DatastreamPy, which refuses a series with
  `$$ER: E100,ACCESS DENIED`. Three series were tried in each of the largest hidden
  databases (~90% of the hidden set).
  - **Entitled** was always right: 17/17 pullable.
  - **NotEntitled** was right except for four Economics databases the API serves anyway:
    BRE, BRC and BRS (LSEG Sovereign Sustainability: country ESG scores, carbon budgets,
    ESG pillar CAGRs) and TRP (Reuters poll consensus forecasts).
- **Do not use the chart as an access check.** `thumbnail.aspx` draws real charts for
  series that DatastreamPy denies (D01–D05/IKR/IKB/IKF bond indices; WDF/WDR/WDN exchange
  rates, including USDOLLR). The Navigator licence permits more series than the data
  feed permits.
- The Category facet's counts ignore entitlement. They are the same with or without the
  filter.
- **Criteria queries ignore all filters**, entitlement included:
  `MNEM:or(TRUK10T,VOD,MLGCORL)` returns all three under both `Entitled` and
  `NotEntitled`. So `entitlement()` checks with a plain text search instead.

## Activity filters

The site's "Active series only" checkbox sets **both** `nav_activity=Active` and
`nav_econactivity=Active`. They are different facets:

- `activity` applies to securities. `sugar` in Futures goes from 1,679 hits to 152.
- `econactivity` applies to economic series, where `Dead` is the other value.
- Together they give zero hits for non-economic queries: Futures + both = 0. The library
  therefore has no `active_only` option. Use the facet that applies.
- For Economics, `activity=Active` and `econactivity=Active` give different counts
  (195,359 and 317,094 for `cpi`).

## Filters by category

Which categories offered each filter in the "Add Filters" sidebar, measured by calling
`filters(category=...)` on all fifteen. The sidebar also narrows as filters are added,
so this list can change. Use it for guidance only. The site ignores a filter that a query
does not offer. It does not reject it.

| filter | categories (ids) |
|---|---|
| category | all |
| activity | 0,1,3,4,5,6,8,9,11,13,14,15,32 |
| currency | 0,3,4,5,6,7,8,9,11,13,15,32 |
| startyear (base date, equals-or-before) | 0,3,4,5,6,7,8,9,11,12,13,32 |
| market | 0,1,3,4,5,8,9,11,12,13,32 |
| type | 0,1,6,9,11,12,13,14,15,32 |
| frequency | 3,4,6,7,8,9,12,32 |
| hasric | 0,3,4,5,11,12,13,14 |
| source | 1,3,6,7,8,9,12,32 |
| exchange | 0,5,11,13,14,15 |
| datatype | 3,6,7,8,9 |
| medrank | 0,5,7,8,12 |
| endyear | 12,13,14,15 |
| highrank | 0,5,6,12 |
| borrower, coupontype | 11,13 |
| coupon | 13,14 |
| underlying | 14,15 |
| unit | 6,12 |
| accesstype, issuertype, priceunit | 13 |
| adjustment, econactivity, forecast, ldb, ldbpermission, localcode | 12 |
| assettype, incomedistribution, industrygroup | 4 |
| fromcurrency | 7 |
| region | 14 |
| restriction | 6 |
| sector | 0 |

`category` only addresses top-level categories. The site reduces a tree node id such as
`12-4428` to its prefix and searches the whole category. Inside a node, use the node's
subset.

## Subsets

`subset=` restricts a search to a set of series. Encodings seen:

- `exp1|<node id>|<list code>|<series id?>|Y|||`: a browse-tree node or list
  (`TreeNode.subset`, explorer links).
- `rel1|<cat>|<rel type>|<rel id>|<series id>|<name>|<total>|<shown>|<series id>`:
  a relationship from a series' details ("100 Constituents", "192 Futures",
  "8 Lists", "2 Peers").
- `mex1|<node>|<node>...`: several tree nodes at once (`combine_explorers`).
- `dtx1|<category path>`: a datatype category (datatype search only).

Adding `nav_category` to a subset search changes nothing. The subset decides.

## Sorting

`s=<code>` sorts on the server, but only together with `page=-1`. On a paged request the
site ignores it. The codes depend on the grid's columns. They are read from each header's
`Search.sort("…")` link and exposed as `SearchPage.sort_options`. Seen: `N` (name),
`S` (history start), `D` (status), `EXC`, `CCY`, `TYP`, `END`, `MKT`, `R` (ranking).

## Search references

A search reference ("searchref") is base64 of the search's query string
(`cT1zdWdhcg==` → `q=sugar`). `search.aspx?searchref=…` replays one, and the site's search
box accepts a pasted reference. `PageData.state.search.persist` holds the current one.

## Fielded search syntax

The text box understands `FIELD:op(v1,v2,…)`, alone or mixed with free text. Fields:
`DESC MNEM DSCD DSSRC SRMNEM SEDOL ISIN LOC RIC`. Ops: `or`, `and`, `not`, and
`and(…),not(…)` combined. Values take `*` wildcards at either end. A comma inside a value
is written `%2C`.

## Resolve quirk: duplicate series fail the request

`resolveLegacySelections.aspx` answers HTTP 500 ("Unexpected logged as …") when two
symbols in one request resolve to **the same series**: `VOD` and `VOD.L`, or `VOD` and
`vod`. It looks like a server-side dictionary key collision. The library sends each
case-insensitive symbol once. If a batch fails, it splits the batch in two and retries
until the two colliding symbols are in different requests. The response omits unknown
symbols. It lists the others in request order.

## Session expiry

How an expired session shows up varies by endpoint:

- `search.aspx`: answers 200 with the sign-in page, which would parse as a zero-hit
  search.
- `searchstraggler.aspx` and other AJAX endpoints: answer 403.
- The JS also honours an `X-Redirect-XHR-Caller` header.

`Session` treats all of these as expiry and signs in again once. A generation counter
makes sure that only one worker signs in again when many workers see the same expiry.
Test: a 1,000-series `details_many` after the cookies were deleted signed in again
exactly once. `explorerleaves.aspx` does not need a session.

## Other observations

- Response encoding is UTF-8 throughout. "…" and "©" in notes are real characters.
- Dates come in four formats: `dd/mm/yyyy` (tables, export), `Mon d yyyy` (hit data
  "Base Date"), `dd-Mon-yyyy` (release calendar) and ISO (chart parameters).
- `-` is the site's placeholder for "no value", in grids, details and exports alike.
- The export ends every line with a trailing comma, so the CSV has an empty last column.
- Do **not** send `ops` to `explorerleaves.aspx` at the root. If you do, the site returns
  `[]`.
- Tree search (`ops=getidswithdatabysearchstr`) steps through matches with
  `searchedId=<current match>&searchDirection=true` and an **empty** `parentId`. Sending
  the parent pins it on the same match. `last` is `1` at the final match and `-1` at the
  first.
- `isLongname=true` gives the grid long, mixed-case names. `isGrouped=true` groups hits by
  category. `isShortdesc=true` factors out a common name prefix.
- `debug=true` adds a diagnostics panel: version, DSID, data source, symbologies, timings.
- A `dataSource` cookie selects the search back end. Only `opensearch` was offered.
