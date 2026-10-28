"""Shared fixtures: one fresh simulation and dataset per test session, built from scenario.yaml."""
from pathlib import Path

import duckdb
import pytest
import yaml

from generator.generate import Simulation, write_parquet
from generator.schema import TABLES, TRUTH_TABLES

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "scenario.yaml").read_text())


def simulate(seed):
    sim = Simulation(CFG, seed)
    sim.run()
    return sim


@pytest.fixture(scope="session")
def sim():
    return simulate(CFG["seed"])


@pytest.fixture(scope="session")
def dataset(sim, tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    write_parquet(sim.rows, TABLES, out / "raw")
    write_parquet(sim.truth, TRUTH_TABLES, out / "truth")
    return out


@pytest.fixture(scope="session")
def con(dataset):
    """DuckDB with schemas raw.* (Stripe-shaped) and truth.* (simulation state)."""
    db = duckdb.connect()
    for schema, tables in (("raw", TABLES), ("truth", TRUTH_TABLES)):
        db.execute(f"CREATE SCHEMA {schema}")
        for table in tables:
            db.execute(f"CREATE VIEW {schema}.{table} AS SELECT * FROM '{dataset / schema / table}.parquet'")
    yield db
    db.close()
