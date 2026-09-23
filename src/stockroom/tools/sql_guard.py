"""Layer 1 of SQL safety: parse the query and allow only a single read of approved schemas.

Rules:
  * exactly one statement, and it must be a query (SELECT / set operation / WITH ... SELECT)
  * every table reference is `<allowed_schema>.<table>` or a CTE defined in the same query
  * no table functions (read_csv, query, glob, ...) and no file paths in FROM
  * no functions known to reach outside the database or run nested SQL

Layer 2 is the connection itself (see base.LOCKED_CONFIG). Each layer is tested alone.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

BLOCKED_FUNC = re.compile(
    r"^(read_\w*|glob|sniff_csv|parquet_\w*|query|query_table|getenv|pragma_\w*|duckdb_\w*|"
    r"sqlite_\w*|postgres_\w*|mysql_\w*|iceberg_\w*|delta_\w*|http\w*|json_execute_serialized_sql|"
    r"current_setting|which_secret|load_extension|install_extension)$",
    re.IGNORECASE,
)


class GuardError(ValueError):
    pass


def check(sql: str, allowed_schemas: frozenset[str] = frozenset({"core"})) -> str:
    """Return the normalised SQL if it is safe to run, else raise GuardError with a reason."""
    if not sql or not sql.strip():
        raise GuardError("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="duckdb") if s is not None]
    except ParseError as e:
        raise GuardError(f"could not parse SQL: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise GuardError("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise GuardError(f"only SELECT queries are allowed (got {stmt.key.upper()})")

    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    for tbl in stmt.find_all(exp.Table):
        if not isinstance(tbl.this, exp.Identifier):
            raise GuardError("table functions are not allowed in FROM (e.g. read_csv, query, glob)")
        schema, catalog, name = tbl.db.lower(), tbl.catalog.lower(), tbl.name.lower()
        if not schema and not catalog and name in cte_names:
            continue
        if catalog not in ("", "warehouse"):
            raise GuardError(f"database '{tbl.catalog}' is not accessible")
        if schema not in allowed_schemas:
            allowed = ", ".join(sorted(f"{s}.*" for s in allowed_schemas))
            raise GuardError(f"table '{tbl.sql(dialect='duckdb')}' is not accessible; only {allowed} tables")

    for fn in stmt.find_all(exp.Func):
        name = fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()
        if name and BLOCKED_FUNC.match(name):
            raise GuardError(f"function '{name}' is not allowed")
    for node in stmt.find_all(exp.Command, exp.Pragma, exp.Set, exp.Copy, exp.Attach, exp.Detach):
        raise GuardError(f"{node.key.upper()} is not allowed")

    return stmt.sql(dialect="duckdb")
