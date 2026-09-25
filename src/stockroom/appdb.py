"""app.duckdb: the only database the running app writes. It holds PO drafts and their decisions.

Tools may INSERT drafts with status PENDING_APPROVAL. Only `stockroom.approvals` (a human-driven
CLI, later the UI) may change a draft's status. There is deliberately no tool for it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import duckdb

from stockroom.config import APP_DB

APP_PATH: Path = APP_DB  # tests point this at a temp file
LOCK_TIMEOUT_S = 10.0
# Who is drafting. "agent" for the CLI and evals; the web API sets "web:<session>" around a turn so each
# visitor sees only their own drafts. A ContextVar so concurrent sessions never mix.
ACTOR: ContextVar[str] = ContextVar("stockroom_actor", default="agent")
_initialised: set[str] = set()
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS po_drafts (
    draft_id      VARCHAR PRIMARY KEY,
    created_at    TIMESTAMP NOT NULL,
    created_by    VARCHAR NOT NULL,
    store         VARCHAR NOT NULL,
    supplier_id   VARCHAR NOT NULL,
    status        VARCHAR NOT NULL CHECK (status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED')),
    n_lines       INTEGER,
    total_cases   INTEGER,
    total_units   INTEGER,
    est_cost      DOUBLE,
    below_supplier_minimum BOOLEAN,
    model_version VARCHAR,
    params        VARCHAR,
    decided_at    TIMESTAMP,
    decided_by    VARCHAR,
    decision_note VARCHAR
);
CREATE TABLE IF NOT EXISTS po_draft_lines (
    draft_id VARCHAR, sku VARCHAR, on_hand INTEGER, on_order INTEGER, lead_time_days INTEGER,
    review_days INTEGER, forecast_units DOUBLE, safety_units DOUBLE, target_units DOUBLE,
    case_pack INTEGER, case_pack_imputed BOOLEAN, order_cases INTEGER, order_units INTEGER,
    unit_cost DOUBLE, est_cost DOUBLE
);
"""


@contextmanager
def app_session(timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open app.duckdb for one unit of work, then close it.

    DuckDB lets only one process hold a writable file. A long-lived process (the MCP server, later the
    web API) that kept a connection open would lock out `stockroom.approvals`, the human side of the
    workflow. So every write opens, works and closes; a busy file is retried for up to `timeout_s`.
    """
    key = str(APP_PATH)
    with _lock:
        deadline = time.monotonic() + timeout_s
        delay = 0.05
        while True:
            try:
                APP_PATH.parent.mkdir(parents=True, exist_ok=True)
                con = duckdb.connect(key, config={"enable_external_access": False})
                break
            except duckdb.IOException as e:
                if "lock" not in str(e).lower() or time.monotonic() > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        try:
            if key not in _initialised:
                con.execute(SCHEMA)
                _initialised.add(key)
            yield con
        finally:
            con.close()


def close_all() -> None:
    """Forget which files have their schema created (tests point APP_PATH at fresh files)."""
    with _lock:
        _initialised.clear()
