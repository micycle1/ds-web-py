"""DataFrame conversion. pandas is an optional dependency: `pip install ds-web[pandas]`."""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from .models import SearchPage, Snapshot

if TYPE_CHECKING:
    import pandas as pd

# dict-valued attributes whose entries become columns of their own
_FLATTEN = ("fields", "extra", "symbols", "values")


def _pandas() -> Any:
    try:
        import pandas
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("DataFrame output needs pandas: pip install 'ds-web[pandas]'") from exc
    return pandas


def _row(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict") and callable(item.to_dict):
        return dict(item.to_dict())
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        row: dict[str, Any] = {}
        nested: list[dict[str, Any]] = []
        for f in dataclasses.fields(item):
            value = getattr(item, f.name)
            if f.name in _FLATTEN and isinstance(value, dict):
                nested.append(value)
            else:
                row[f.name] = value
        for extra in nested:
            for key, value in extra.items():
                row.setdefault(key, value)
        return row
    if isinstance(item, dict):
        return dict(item)
    raise TypeError(f"can't make a DataFrame row from {type(item).__name__}")


def frame_from_rows(rows: list[dict[str, Any]], first: Sequence[str] = ()) -> pd.DataFrame:
    """A DataFrame from row dicts: `first` columns lead (and exist even with no rows),
    date objects become datetime64."""
    pandas = _pandas()
    columns = list(first) + sorted({k for row in rows for k in row} - set(first), key=lambda k: _order(rows, k))
    frame = pandas.DataFrame(rows, columns=columns)
    for column in frame.columns:
        sample = frame[column].dropna()
        if len(sample) and all(isinstance(v, dt.date) for v in sample.head(50)):
            frame[column] = pandas.to_datetime(frame[column], errors="coerce")
    return frame


def _order(rows: list[dict[str, Any]], key: str) -> int:
    """Columns in the order they first appear across rows."""
    for i, row in enumerate(rows):
        if key in row:
            return i * 10_000 + list(row).index(key)
    return 0


def to_frame(items: Iterable[Any] | Snapshot | SearchPage) -> pd.DataFrame:
    """Any list of results — Series, SearchHit, SeriesDetails, Datatype, TreeNode,
    FilterOption, ... — or a SearchPage/Snapshot, as a DataFrame. Dict attributes
    (`fields`, `extra`, `symbols`, `values`) are spread into columns of their own."""
    if isinstance(items, Snapshot):
        labels = {c.datatype: c.label for c in items.columns}
        rows = [
            {"series_id": r.series_id, "name": r.name, "mnemonic": r.mnemonic, **{labels.get(k, k): v for k, v in r.values.items()}}
            for r in items.rows
        ]
        return frame_from_rows(rows, first=("series_id", "name", "mnemonic"))
    if isinstance(items, SearchPage):
        return frame_from_rows([_row(hit) for hit in items.hits])
    return frame_from_rows([_row(item) for item in items])
