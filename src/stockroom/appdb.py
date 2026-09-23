"""app.duckdb: the only database the running app writes. It holds PO drafts and their decisions.

Tools may INSERT drafts with status PENDING_APPROVAL. Only `stockroom.approvals` (a human-driven
CLI, later the UI) may change a draft's status. There is deliberately no tool for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from stockroom.config import APP_DB

APP_PATH: Path = APP_DB  # tests point this at a temp file
_conns: dict[str, duckdb.DuckDBPyConnection] = {}
_lock = threading.Lock()

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


def app_db() -> duckdb.DuckDBPyConnection:
    key = str(APP_PATH)
    with _lock:
        if key not in _conns:
            APP_PATH.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(key, config={"enable_external_access": False})
            con.execute(SCHEMA)
            _conns[key] = con
        return _conns[key]


def close_all() -> None:
    with _lock:
        for c in _conns.values():
            c.close()
        _conns.clear()
