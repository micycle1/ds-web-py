"""Parsers for the site's responses. Pure functions of the response text, so they can be
tested against saved fixtures without a login.

The site is server-rendered ASP.NET WebForms, so most of these read HTML. They are written
to degrade rather than fail on markup drift: an unrecognised cell becomes a plain text
field, a missing optional element becomes None. Only when the core structure is gone
(no results table where one must be) do they raise ParseError.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from .errors import ParseError
from .models import (
    Category,
    Datatype,
    DatatypeCoverage,
    DatatypeDefinition,
    FilterOption,
    Link,
    Note,
    ReleaseDate,
    SearchHit,
    SearchPage,
    Series,
    SeriesDetails,
    Snapshot,
    SnapshotColumn,
    SnapshotRow,
    TreeNode,
)
from .query import Query

# --- shared helpers ------------------------------------------------------------------

_LOGIN_FORM_RE = re.compile(r'name="(?:usernameTextBox|passwordTextBox)"')
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_SORT_ARROWS_RE = re.compile(r"[▲▼]+$")
_SUBSET_CALL_RE = re.compile(r'Search\.subset(?:WithFilters)?\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?')
_NDOR_RE = re.compile(r'Popups\.showNdor\(\s*\d+\s*,\s*"([^"]+)"')
_SORT_CALL_RE = re.compile(r'Search\.sort\(\s*"([^"]*)"')
_LEADING_COUNT_RE = re.compile(r"^\s*([\d,]+)\b")
_TRAILING_COUNT_RE = re.compile(r"\s*\(([\d,]+)\)\s*$")
_BRACKET_COUNT_RE = re.compile(r"\s*\[([\d,]+)\]\s*$")


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def is_login_page(html: str) -> bool:
    """True if `html` is the sign-in page. An expired session gets redirected there, and
    the sign-in page would otherwise parse as a valid search page with zero hits."""
    return bool(_LOGIN_FORM_RE.search(html))


def snake_case(text: str) -> str:
    """Normalize a column/row label: "Maturity Date" -> "maturity_date", "Hist." ->
    "hist", "categoryId" -> "category_id"."""
    text = _CAMEL_BOUNDARY_RE.sub("_", text.replace(".", ""))
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", "_", text.strip()).lower()


def _unique_key(key: str, taken: dict[str, Any]) -> str:
    if key not in taken:
        return key
    n = 2
    while f"{key}_{n}" in taken:
        n += 1
    return f"{key}_{n}"


def _int(text: str) -> int | None:
    return int(text.replace(",", "")) if text else None


def _clean_text(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()


def extract_page_data(html: str) -> dict[str, Any]:
    """The `var PageData = {...};` object embedded in a search page. Brace-matched by
    hand because it nests objects that defeat a non-greedy regex."""
    marker = "var PageData = "
    start = html.find(marker)
    if start == -1:
        return {}
    start += len(marker)
    depth, in_string, escape = 0, False, False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _parse_filter_string(text: str) -> dict[str, str]:
    """The nav_* filters in the second argument of Search.subsetWithFilters:
    "&nav_source=X&nav_frequency=Y&forcenavfilters=true" (the last is a UI flag)."""
    filters: dict[str, str] = {}
    for part in text.split("&"):
        key, _, value = part.partition("=")
        if key.startswith("nav_"):
            filters[key] = value
    return filters


def link_from_anchor(a: Tag) -> Link | None:
    """A Link from an <a> that navigates to a subset (Search.subset(...),
    Search.subsetWithFilters(...), or data-subset); None for any other anchor."""
    subset, filters = None, {}
    if a.has_attr("data-subset"):
        subset = a["data-subset"]
    else:
        match = _SUBSET_CALL_RE.search(a.get("onclick", ""))
        if match:
            subset = match.group(1)
            filters = _parse_filter_string(match.group(2) or "")
    if not subset:
        return None
    label = _clean_text(a)
    count = _LEADING_COUNT_RE.match(label)
    holder = a.find_parent("div", attrs={"title": True})
    return Link(
        label=label,
        subset=subset,
        filters=filters,
        path=re.sub(r"\s+", " ", holder["title"]).strip() if holder else None,
        count=_int(count.group(1)) if count else None,
    )


def _subset_links(tag: Tag) -> list[Link]:
    return [link for a in tag.find_all("a") if (link := link_from_anchor(a)) is not None]


def _parse_date(text: str, *formats: str) -> dt.date | None:
    text = text.strip()
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_dmy(text: str) -> dt.date | None:
    """A dd/mm/yyyy date, as the site's tables and exports write them."""
    return _parse_date(text, "%d/%m/%Y")


# --- the results grid ----------------------------------------------------------------


def _parse_grid(table: Tag) -> tuple[list[dict[str, Any]], list[tuple[str, list[Tag]]]]:
    """Headers (label, key, sort code, datatype) and (series_id, cells) rows of a grid.
    Header and cell lists line up index for index; the leading unlabelled columns are
    the pin and status icons."""
    header_row = table.find("tr")
    headers = []
    for th in header_row.find_all("th", recursive=False) if header_row else []:
        label = _SORT_ARROWS_RE.sub("", th.get_text(strip=True)).strip()
        anchor = th.find("a")
        sort = _SORT_CALL_RE.search((anchor.get("onclick", "") if anchor else "") or th.get("onclick", "") or "")
        headers.append({
            "label": label,
            "key": snake_case(label),
            "sort": sort.group(1) if sort else None,
            "datatype": anchor.get("data-dt") if anchor is not None else None,
        })
    rows = []
    for tr in table.find_all("tr", id=re.compile(r"^hit_")):
        rows.append((tr["id"].removeprefix("hit_"), tr.find_all("td", recursive=False)))
    return headers, rows


def _status_flags(cell: Tag) -> tuple[str, ...]:
    for span in cell.find_all("span", class_="lgc"):
        classes = span.get("class") or []
        title = span.get("title", "")
        if title and "pin" not in classes and "note" not in classes:
            return tuple(part.strip() for part in title.split(",") if part.strip())
    return ()


def parse_hits(table: Tag) -> list[SearchHit]:
    headers, rows = _parse_grid(table)
    hits = []
    for series_id, cells in rows:
        name = mnemonic = ""
        status: tuple[str, ...] = ()
        has_notes = False
        fields: dict[str, str] = {}
        for header, cell in zip(headers, cells):
            key = header["key"]
            if not key:
                status = status or _status_flags(cell)
                continue
            if key == "name":
                name = cell.get_text(strip=True)
                has_notes = cell.find("span", class_="note") is not None
            elif key == "symbol":
                mnemonic = cell.get_text(strip=True)
            else:
                fields[_unique_key(key, fields)] = cell.get_text(" ", strip=True)
        hits.append(SearchHit(series_id, name, mnemonic, status, has_notes, fields))
    return hits


def parse_search_page(html: str, query: Query, page: int) -> SearchPage:
    if is_login_page(html):
        raise ParseError("expected a search page but got the sign-in page")
    soup = soup_of(html)
    table = soup.find("table", id="resulttable")
    hits = parse_hits(table) if table else []
    page_data = extract_page_data(html)

    total = page_data.get("totalHits")
    if total is None:
        pager = soup.find(id="pager")
        match = re.search(r"of ([\d,]+)", pager.get_text(" ", strip=True)) if pager else None
        total = _int(match.group(1)) if match else len(hits)

    sort_options = {}
    if table:
        for header in _parse_grid(table)[0]:
            if header["sort"]:
                sort_options[header["label"] or "Status"] = header["sort"]

    suggestions_box = soup.find(id="explorersuggestion")
    suggestions = _subset_links(suggestions_box) if suggestions_box else []

    return SearchPage(
        query=query,
        page=page,
        hits=hits,
        total_hits=int(total),
        filters=parse_filter_sidebar(soup),
        sort_options=sort_options,
        explorer_suggestions=suggestions,
        search_ref=(page_data.get("state") or {}).get("search", {}).get("persist") or None,
    )


def _parse_count(text: str) -> int | None:
    """A sidebar count: "1,234" -> 1234; "(all)" or empty -> None."""
    match = re.fullmatch(r"([\d,]+)", text.strip())
    return _int(match.group(1)) if match else None


def parse_filter_sidebar(soup: BeautifulSoup) -> list[FilterOption]:
    """The "Add Filters" sidebar: one FilterOption per value offered or applied.

    Each filter is a <div id="refine-<name>"> listing up to three values inline; the full
    list lives in <table id="popup_<name>">, whose labels carry the count in their text
    ("Afghanistan (15,871)"). The popup is preferred when present, with inline counts
    merged onto it."""
    refine = soup.find("div", id="refine")
    if not refine:
        return []
    options: list[FilterOption] = []
    for div in refine.find_all("div", id=re.compile(r"^refine-")):
        name = div["id"].removeprefix("refine-")
        heading = div.find("h3")
        label_spans = heading.find_all("span", recursive=False) if heading else []
        filter_label = label_spans[-1].get_text(strip=True) if label_spans else name

        inline = []
        for a in div.find_all("a"):
            cell = a.find("span", class_="value") or a.find("span", class_="summary")
            if cell is None:
                continue
            count_cell = a.find("span", class_="count")
            value_label = cell.get_text(strip=True)
            inline.append({
                "value": a.get("data-filtervalue") or value_label,
                "value_label": value_label,
                "count": _parse_count(count_cell.get_text(strip=True)) if count_cell else None,
                "applied": "summary" in (cell.get("class") or []),
            })

        popup = soup.find("table", id=f"popup_{name}")
        if popup is not None:
            counts = {item["value"]: item["count"] for item in inline}
            param = popup.get("data-filterid") or f"nav_{name}"
            values = []
            for lbl in popup.find_all("label", attrs={"data-filtervalue": True}):
                value = lbl["data-filtervalue"]
                text = lbl.get_text(strip=True)
                match = _TRAILING_COUNT_RE.search(text)
                value_label = text[: match.start()].strip() if match else text
                label_count = _int(match.group(1)) if match else None
                values.append({
                    "value": value,
                    "value_label": value_label,
                    "count": counts[value] if counts.get(value) is not None else label_count,
                    "applied": False,
                })
        else:
            param, values = f"nav_{name}", inline

        options.extend(
            FilterOption(filter_name=name, param=param, filter_label=filter_label, **item)
            for item in values
        )
    return options


def parse_categories(html: str) -> list[Category]:
    soup = soup_of(html)
    found: dict[str, Category] = {}
    for tag in soup.select('a[data-filterid="nav_category"][data-filtervalue]'):
        name = tag.select_one("span.value")
        if name is None:
            continue
        count = tag.select_one("span.count")
        cid = tag["data-filtervalue"]
        found[cid] = Category(
            name=name.get_text(strip=True),
            id=cid,
            count=_parse_count(count.get_text(strip=True)) if count else None,
        )
    return sorted(found.values(), key=lambda c: c.name)


def parse_datatype_categories(html: str) -> dict[str, str]:
    """The datatype realm's category <select>: {name: subset}."""
    select = soup_of(html).find("select", id="dtcat")
    if select is None:
        raise ParseError("datatype search page has no category selector (#dtcat)")
    return {o.get_text(strip=True): o["value"] for o in select.find_all("option") if o.get("value")}


# --- values snapshot (page=-2) -------------------------------------------------------


def _number(text: str | None) -> float | str | None:
    if text is None:
        return None
    text = text.strip()
    if text in ("", "-"):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return text


def parse_snapshot(html: str) -> Snapshot:
    soup = soup_of(html)
    table = soup.find("table", id="resulttable")
    if table is None:
        raise ParseError("values preview page has no results table")
    headers, rows = _parse_grid(table)
    columns = [
        SnapshotColumn(label=h["label"], datatype=h["datatype"])
        for h in headers
        if h["datatype"] and h["datatype"] not in ("NAME", "MNEM")
    ]
    parsed = []
    for series_id, cells in rows:
        name = mnemonic = ""
        values: dict[str, float | str | None] = {}
        for header, cell in zip(headers, cells):
            datatype = header["datatype"]
            if datatype == "NAME" or (not datatype and header["key"] == "name"):
                name = cell.get_text(strip=True)
            elif datatype == "MNEM" or (not datatype and header["key"] == "symbol"):
                mnemonic = cell.get_text(strip=True)
            elif datatype:
                marked = cell.find(attrs={"sortvalue": True})
                values[datatype] = _number(marked["sortvalue"] if marked else cell.get_text(strip=True))
        parsed.append(SnapshotRow(series_id, name, mnemonic, values))
    total = extract_page_data(html).get("totalHits", len(parsed))
    return Snapshot(columns=columns, rows=parsed, total_hits=int(total))


# --- structured hit data (hitdata.aspx / resolveLegacySelections / expandmnemonics) ---

_SERIES_KEYS = {
    "DS Mnemonic": "ds_mnemonic",
    "DS Code": "ds_code",
    "RIC": "ric",
    "ISIN": "isin",
    "SEDOL": "sedol",
    "Local Code": "local_code",
    "T1 Code": "t1_code",
    "LDB": "ldb",
    "_categoryID": "category_id",
    "_categoryName": "category_name",
}
# bookkeeping the site's JS uses for its basket, not series metadata
_SERIES_DROP = {"docid", "Name", "_key", "_dskey", "_clicked", "Base Date", "preserveKey", "knownSymbol"}


def parse_series(obj: dict[str, Any]) -> Series | None:
    """A Series from one hit object; None for the empty {} the site returns for a hit it
    can't describe."""
    docid = obj.get("docid")
    if not docid:
        return None
    known = {attr: obj[key] for key, attr in _SERIES_KEYS.items() if obj.get(key) not in (None, "")}
    extra = {
        snake_case(k.lstrip("_")): v
        for k, v in obj.items()
        if k not in _SERIES_KEYS and k not in _SERIES_DROP
    }
    return Series(
        series_id=str(docid),
        name=obj.get("Name", ""),
        symbol=obj.get("_key") or obj.get("_dskey") or "",
        base_date=_parse_date(obj.get("Base Date") or "", "%b %d %Y"),
        extra=extra,
        **known,
    )


def parse_series_list(data: Any) -> list[Series]:
    if not isinstance(data, list):
        raise ParseError(f"expected a JSON list of hits, got {type(data).__name__}")
    return [s for obj in data if isinstance(obj, dict) and (s := parse_series(obj)) is not None]


# --- series details (searchstraggler.aspx) -------------------------------------------


def _parse_datatype_cell(cell: Tag) -> list[DatatypeCoverage]:
    """A datatype row: <a title=name>CODE</a> tags, each optionally followed by an
    untitled <a>(from Mon YYYY)</a> giving its start; a trailing "More..." popup link."""
    found: list[dict[str, str | None]] = []
    for a in cell.find_all("a"):
        title = a.get("title", "")
        if title.startswith("Click for more") or a.has_attr("onclick"):
            continue
        if title:
            found.append({"code": a.get_text(strip=True), "name": title, "available_from": None})
        elif found:
            text = a.get_text(strip=True).strip("()")
            found[-1]["available_from"] = text.removeprefix("from ").strip() or None
    return [DatatypeCoverage(**d) for d in found]  # type: ignore[arg-type]


def parse_notes(root: Tag | str) -> list[Note]:
    tag = soup_of(root) if isinstance(root, str) else root
    notes = []
    for div in tag.find_all("div", class_="note"):
        heading = div.find("h4")
        title = heading.get_text(" ", strip=True) if heading else ""
        body = [child for child in div.children if child is not heading]
        html = "".join(str(c) for c in body).strip()
        text = "\n".join(
            line for line in (re.sub(r"[ \t]+", " ", s).strip() for s in soup_of(html).get_text("\n").splitlines()) if line
        )
        notes.append(Note(title=title, text=text, html=html))
    return notes


def _parse_detail(spot: Tag, series_id: str) -> SeriesDetails:
    chart_div = spot.find("div", attrs={"data-chart": True})
    try:
        chart = json.loads(chart_div["data-chart"]) if chart_div else {}
    except json.JSONDecodeError:
        chart = {}
    chart_datatypes = [
        td["data-id"].split("|", 1)[1]
        for td in spot.select('tr.chartcontrols td[data-group="datatype"][data-id]')
        if "|" in td["data-id"]
    ]

    symbols_block = spot.find(class_="spot-symbols")
    heading = symbols_block.find_previous("h3") if symbols_block else spot.find("h3")
    full_name = heading.get_text(" ", strip=True) if heading and heading.get_text(strip=True) else None

    symbols: dict[str, str] = {}
    for span in spot.select(".spot-symbols .fake-inline-table"):
        label, value = span.find(class_="fake-th"), span.find(class_="fake-td")
        if label and value:
            symbols[_unique_key(snake_case(label.get_text(strip=True)), symbols)] = value.get_text(strip=True)

    # read before the row loop below, which strips popup links like this one out of cells
    ndor = next(
        (m for a in spot.find_all("a", onclick=True) if (m := _NDOR_RE.search(a["onclick"]))), None
    )

    fields: dict[str, str] = {}
    urls: dict[str, str] = {}
    datatypes: dict[str, list[DatatypeCoverage]] = {}
    links: dict[str, list[Link]] = {}
    notes: list[Note] = []
    grid = spot.find("table", class_="datagrid")
    for row in grid.find_all("tr") if grid else []:
        header, cell = row.find("th"), row.find("td")
        if not header or not cell or not header.get_text(strip=True):
            continue
        key = snake_case(header.get_text(strip=True))
        if "spot-datatypes" in (cell.get("class") or []):
            datatypes[_unique_key(key, datatypes)] = _parse_datatype_cell(cell)
        elif cell.find("div", class_="note"):
            notes.extend(parse_notes(cell))
        elif subset_links := _subset_links(cell):
            links.setdefault(key, []).extend(subset_links)
        else:
            external = cell.find("a", href=re.compile(r"^https?://"))
            if external:
                urls[key] = external["href"]
            for popup in cell.find_all("a", onclick=True):
                popup.extract()  # "More..." popup triggers aren't part of the value
            text = _clean_text(cell)
            if text and text != "-":  # the site's placeholder for "no value"
                fields[_unique_key(key, fields)] = text

    return SeriesDetails(
        series_id=series_id,
        full_name=full_name,
        symbols=symbols,
        fields=fields,
        urls=urls,
        datatypes=datatypes,
        links=links,
        notes=notes,
        chart=chart,
        chart_datatypes=chart_datatypes,
        release_frequency=ndor.group(1) if ndor else None,
    )


def parse_details(html: str) -> dict[str, SeriesDetails]:
    """Every series in a (batched) details response, keyed by series id."""
    soup = soup_of(html)
    found = {}
    for spot in soup.find_all("div", class_="spot-wrapper", id=re.compile(r"^spot_")):
        series_id = spot["id"].removeprefix("spot_").split("_")[0]
        found[series_id] = _parse_detail(spot, series_id)
    return found


# --- datatypes ------------------------------------------------------------------------


def parse_datatypes(html: str) -> list[Datatype]:
    soup = soup_of(html)
    result = []
    for tbody in soup.find_all("tbody"):
        category = tbody.get("data-cat")
        for tr in tbody.find_all("tr", attrs={"data-mnem": True}):
            cells = tr.find_all("td")
            since_text = cells[-1].get_text(strip=True) if cells else ""
            result.append(Datatype(
                mnemonic=tr["data-mnem"],
                name=tr.get("data-name", ""),
                time_series=tr.get("data-ts") == "true",
                since=_parse_date(since_text, "%d/%m/%Y"),
                category_id=category,
            ))
    return result


def parse_datatype_definition(html: str, mnemonic: str, category_id: str) -> DatatypeDefinition:
    soup = soup_of(html)
    holder = soup.find("input", class_="dthelp")
    inner = holder["value"] if holder is not None and holder.has_attr("value") else html
    inner_soup = soup_of(inner)
    for tag in inner_soup.find_all(["script", "style"]):
        tag.decompose()
    lines = (re.sub(r"[ \t\xa0]+", " ", s).strip() for s in inner_soup.get_text("\n").splitlines())
    return DatatypeDefinition(
        mnemonic=mnemonic,
        category_id=category_id,
        text="\n".join(line for line in lines if line),
        html=inner,
    )


# --- release calendar -----------------------------------------------------------------


def parse_release_dates(html: str) -> list[ReleaseDate]:
    table = soup_of(html).find("table", class_="datagrid")
    result = []
    for tr in table.find_all("tr") if table else []:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 3:
            result.append(ReleaseDate(
                date=_parse_date(cells[0], "%d-%b-%Y", "%d/%m/%Y"),
                time=cells[1],
                period=cells[2],
            ))
    return result


# --- browse tree ----------------------------------------------------------------------


def tree_children(data: Any, nid: str) -> list[dict[str, Any]]:
    """Child nodes from an explorerleaves.aspx response. The root ('#') returns a flat
    list; any other node wraps it as [{"<nid>": [...]}]."""
    if not isinstance(data, list):
        raise ParseError(f"expected a JSON list of tree nodes, got {type(data).__name__}")
    if nid != "#" and data and isinstance(data[0], dict) and nid in data[0]:
        return data[0][nid]
    return [node for node in data if isinstance(node, dict) and "id" in node]


def tree_node(raw: dict[str, Any], parent: str | None, depth: int, parent_path: tuple[str, ...]) -> TreeNode:
    text = raw.get("text", "")
    size = _BRACKET_COUNT_RE.search(text)
    return TreeNode(
        id=raw.get("id", ""),
        text=text,
        parent=parent,
        depth=depth,
        type=raw.get("type", ""),
        has_children=bool(raw.get("children")),
        subset=raw.get("data") or None,
        path=" » ".join(parent_path + (text,)),
        size=_int(size.group(1)) if size else None,
    )


def tree_path(data: Any, roots: list[dict[str, Any]]) -> list[TreeNode]:
    """The path a tree search returns, as nodes from the root down to the match.

    The response is [{nodeid: [its children]}, ...], target first and a top-level node
    last; each node's own record is found among its parent's children, and the top-level
    one among `roots` (the tree's first level, which the response doesn't repeat)."""
    if isinstance(data, dict):
        data = data.get("path", [])
    if not isinstance(data, list):
        raise ParseError("unexpected tree search response")
    steps = [next(iter(item.items())) for item in data if isinstance(item, dict) and item]
    steps.reverse()  # top-level node first
    nodes: list[TreeNode] = []
    path: tuple[str, ...] = ()
    parent: str | None = None
    siblings: list[dict[str, Any]] = roots
    for depth, (node_id, children) in enumerate(steps, start=1):
        raw = next((n for n in siblings if n.get("id") == node_id), {"id": node_id, "text": node_id})
        node = tree_node(raw, parent, depth, path)
        nodes.append(node)
        path, parent, siblings = path + (node.text,), node_id, children or []
    return nodes
