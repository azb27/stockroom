"""SQL safety, tested one layer at a time.

Layer 1 (sql_guard) must reject every attack below on its own.
Layer 2 (the locked connection) must block the dangerous ones even with the guard bypassed.
"""

from __future__ import annotations

import pytest

from stockroom.tools import sql_guard
from stockroom.tools.base import warehouse

ALLOWED = [
    "SELECT * FROM core.dim_sku LIMIT 5",
    "select dept, sum(revenue) from core.sales_enriched group by 1",
    "WITH t AS (SELECT sku, sum(units) u FROM core.fact_sales_daily GROUP BY 1) SELECT * FROM t ORDER BY u DESC",
    "SELECT 1 UNION ALL SELECT 2",
    "FROM core.dim_store",
    "SELECT s.sku FROM core.dim_sku s WHERE s.sku IN (SELECT sku FROM core.inventory WHERE on_hand_units = 0)",
    "SELECT * FROM warehouse.core.dim_store",
    "SELECT sku, units, lag(units) OVER (PARTITION BY sku ORDER BY date) FROM core.fact_sales_daily LIMIT 3",
]

REJECTED = {
    "write": "DROP TABLE core.dim_sku",
    "insert": "INSERT INTO core.dim_store VALUES ('X', 'y')",
    "update": "UPDATE core.dim_sku SET status = 'X'",
    "multi_statement": "SELECT 1; DROP TABLE core.dim_sku",
    "raw_schema": "SELECT * FROM raw.pos_sales_lines",
    "raw_in_subquery": "SELECT * FROM core.dim_sku WHERE sku IN (SELECT sku_code FROM raw.erp_sku_master)",
    "raw_in_cte": "WITH x AS (SELECT * FROM raw.price_book) SELECT * FROM x",
    "unqualified_table": "SELECT * FROM dim_sku",
    "other_catalog": "SELECT * FROM gt.truth.sales",
    "attach": "ATTACH 'data/ground_truth.duckdb' AS gt",
    "read_csv": "SELECT * FROM read_csv('data/m5/calendar.csv')",
    "read_csv_auto": "SELECT * FROM read_csv_auto('/etc/passwd')",
    "file_path_from": "SELECT * FROM 'data/ground_truth.duckdb'",
    "parquet": "SELECT * FROM read_parquet('x.parquet')",
    "glob": "SELECT * FROM glob('*')",
    "nested_sql_query_fn": "SELECT * FROM query('SELECT * FROM raw.stores')",
    "query_table": "SELECT * FROM query_table('raw.stores')",
    "scalar_io_fn": "SELECT read_text('data/dirt_manifest.json')",
    "getenv": "SELECT getenv('ANTHROPIC_API_KEY')",
    "metadata_fn": "SELECT * FROM duckdb_tables()",
    "pragma": "PRAGMA database_list",
    "set": "SET enable_external_access = true",
    "copy": "COPY core.dim_sku TO 'out.csv'",
    "install": "INSTALL httpfs",
    "create": "CREATE TABLE core.x AS SELECT 1",
    "empty": "   ",
    "garbage": "SELEC * FRM",
}


@pytest.mark.parametrize("sql", ALLOWED)
def test_guard_allows_reads_of_core(sql):
    sql_guard.check(sql)


@pytest.mark.parametrize("name", list(REJECTED))
def test_guard_rejects(name):
    with pytest.raises(sql_guard.GuardError):
        sql_guard.check(REJECTED[name])


def test_guard_schema_allowlist_is_configurable_for_ablation():
    sql_guard.check("SELECT * FROM raw.stores", frozenset({"raw"}))
    with pytest.raises(sql_guard.GuardError):
        sql_guard.check("SELECT * FROM core.dim_store", frozenset({"raw"}))


LAYER2 = {
    "attach": "ATTACH 'data/ground_truth.duckdb' AS gt (READ_ONLY)",
    "read_csv": "SELECT * FROM read_csv('data/m5/calendar.csv')",
    "copy": "COPY core.dim_store TO 'leak.csv'",
    "create": "CREATE TABLE core.x AS SELECT 1",
    "insert": "INSERT INTO core.dim_store VALUES ('X', 'y')",
    "unlock": "SET enable_external_access = true",
    "install": "INSTALL httpfs",
}


@pytest.mark.parametrize("name", list(LAYER2))
def test_connection_blocks_even_without_guard(name):
    with pytest.raises(Exception):  # noqa: B017 - any duckdb error means blocked
        warehouse().cursor().execute(LAYER2[name])
