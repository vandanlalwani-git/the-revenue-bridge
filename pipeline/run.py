"""Build the SQL marts from <data>/raw in DuckDB and export them to <data>/marts.

Usage:  python -m pipeline.run [--data data]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from generator.schema import TABLES

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql"
MARTS = ["calendar", "mrr_month_end", "customer_mrr", "mrr_movements", "mrr_drivers", "mrr_bridge",
         "mrr_bridge_drivers"]


def connect(data: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    for table in TABLES:
        con.execute(f"CREATE VIEW raw.{table} AS SELECT * FROM '{data / 'raw' / table}.parquet'")
    return con


def build(con: duckdb.DuckDBPyConnection, upto: str | None = None) -> None:
    """Run sql/*.sql in name order; `upto="10"` stops after the files numbered 10."""
    for path in sorted(SQL_DIR.glob("*.sql")):
        if upto is not None and path.name[:2] > upto:
            break
        con.execute(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    con = connect(args.data)
    build(con)
    out = args.data / "marts"
    out.mkdir(exist_ok=True)
    for table in MARTS:
        con.execute(f"COPY {table} TO '{out / table}.parquet' (FORMAT parquet)")
    print(con.sql("SELECT * FROM mrr_bridge_usd WHERE month >= '2025-01' ORDER BY month"))


if __name__ == "__main__":
    main()
