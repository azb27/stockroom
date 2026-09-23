"""Shared plumbing for tools: the result envelope and the locked-down warehouse connection."""

from __future__ import annotations

import datetime as dt
import decimal
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from stockroom.config import AS_OF_DATE, WAREHOUSE_DB

# Defence in depth, layer 2 (layer 1 is sql_guard). Even if a query slipped past the
# parser, this connection cannot read files, attach other databases, load extensions,
# write anything, or change these settings.
LOCKED_CONFIG = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "lock_configuration": True,
}

_conns: dict[str, duckdb.DuckDBPyConnection] = {}
_lock = threading.Lock()


def warehouse(db_path: Path = WAREHOUSE_DB) -> duckdb.DuckDBPyConnection:
    """A shared read-only, locked connection. Callers should use `.cursor()` per query."""
    key = str(db_path)
    with _lock:
        if key not in _conns:
            if not db_path.exists():
                raise FileNotFoundError(f"{db_path} not found. Run `python -m stockroom.data.pipeline`.")
            _conns[key] = duckdb.connect(key, read_only=True, config=LOCKED_CONFIG)
        return _conns[key]


class ToolError(Exception):
    """A user-facing error: the message is returned to the model so it can recover."""


@dataclass
class ToolResult:
    data: Any
    caveats: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


def jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    if isinstance(x, decimal.Decimal):
        return float(x)
    if isinstance(x, float):
        return round(x, 4)
    return x


def as_of() -> dt.date:
    return dt.date.fromisoformat(AS_OF_DATE)


def store_day_caveats(
    cur: duckdb.DuckDBPyConnection,
    start: dt.date | None = None,
    end: dt.date | None = None,
    store: str | None = None,
) -> list[str]:
    """Plain-English caveats for store-days that are not normal trading days in a window."""
    where = ["status <> 'open'"]
    params: list[Any] = []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    if store:
        where.append("store = ?")
        params.append(store)
    rows = cur.execute(
        f"""SELECT status,
                   CASE WHEN status = 'closed_all_stores'
                        THEN string_agg(DISTINCT CAST(date AS VARCHAR), ', ' ORDER BY CAST(date AS VARCHAR))
                        ELSE string_agg(store || ' ' || CAST(date AS VARCHAR), ', ' ORDER BY date, store) END
            FROM core.store_day_status WHERE {" AND ".join(where)} GROUP BY status""",
        params,
    ).fetchall()
    out = []
    for status, days in rows:
        if status == "missing_data":
            out.append(
                f"MISSING DATA (POS outage) for {days}: sales there are UNKNOWN, not zero. "
                "Totals covering these days are understated; say so."
            )
        elif status == "closed_all_stores":
            out.append(f"All stores closed (chain holiday) on {days}: near-zero sales are expected.")
    return out
