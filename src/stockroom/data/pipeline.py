"""Build everything: ground truth -> messy raw warehouse -> cleaned core layer.

    python -m stockroom.data.pipeline            # full rebuild
    python -m stockroom.data.pipeline --core     # re-run cleaning only
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

from stockroom.config import WAREHOUSE_DB

SQL_DIR = Path(__file__).parent / "sql"


def build_core(db_path: Path = WAREHOUSE_DB) -> None:
    con = duckdb.connect(str(db_path))
    con.execute((SQL_DIR / "core.sql").read_text())
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", action="store_true", help="only rebuild the core (cleaned) layer")
    args = ap.parse_args()

    t0 = time.time()
    if not args.core:
        from stockroom.data.dirt import build_warehouse
        from stockroom.data.truth import build_truth

        print("1/3 ground truth ...", flush=True)
        build_truth()
        print("2/3 raw warehouse + dirt injection ...", flush=True)
        build_warehouse()
    print("3/3 core layer ...", flush=True)
    build_core()

    con = duckdb.connect(str(WAREHOUSE_DB), read_only=True)
    print(con.sql("SELECT issue_type, rows_affected, finding FROM core.dq_issues"))
    con.close()
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
