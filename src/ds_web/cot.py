"""Commitments of Traders (COT) series names.

Datastream carries the CFTC's COT reports as ordinary series, but records the underlying
asset nowhere structured — the browse tree groups COT series by trader category, not by
asset, and they carry no underlying-series link. The name is the only place it lives, so
this module parses it.

    from ds_web.cot import parse_cot_name
    parse_cot_name(ds.details(series_id).full_name, mnemonic)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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

# Zero-width characters occur inside words in the source data, for example "Proces​sor" in
# Producer/Merchant/Processor/User. They prevent a match on that word unless removed.
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
    Split a COT series' full name (`SeriesDetails.full_name`, or the `full_name` column
    of `search_frame()`) into exchange / asset / trader category, or None if it isn't a
    COT series name.

        parse_cot_name("Chicago Mercantile Exchange(CME)-Feeder Cattle "
                       "Non-Commercial Short Index")
        # CotName(exchange='Chicago Mercantile Exchange', exchange_code='CME',
        #         asset='Feeder Cattle', trader_category='Non-Commercial',
        #         trader_category_code='NC', position='Short', futures_only=False)

    Pass `mnemonic` when you have it. It carries the trader-category code (CFCNCSI -> NC)
    and settles the one ambiguous shape, where the asset's last word and the category's
    first word spell a third category between them — "5YR Interest Rate Swap Dealer /
    Intermediary" (CNGDIXC) otherwise reads as "Swap Dealer" and the asset loses its
    "Swap".

    This is the only way to recover the underlying asset. Datastream records it nowhere
    structured: the browse tree groups COT by *trader category* rather than by asset
    (6-42 "COT" has 13 leaves, none naming a commodity), a COT series' `explorers` points
    only back at its own trader-category node, and unlike Futures and Options these
    series carry no `underlying_series_symbol`. The name is it.

    Pure string handling, so labelling a whole family costs nothing extra —
    `search_frame()` already has the full names (from the site's export):

        df = ds.search_frame(subset=NON_COMMERCIAL_NODE)
        df["asset"] = [getattr(parse_cot_name(n, m), "asset", None)
                       for n, m in zip(df.full_name, df.symbol)]

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

    The vocabulary belongs to the site and can change. This function parses scraped
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
