"""Single source of truth for paths and dataset parameters."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("STOCKROOM_DATA_DIR", ROOT / "data"))
RAW_M5_DIR = DATA_DIR / "m5"

# ground_truth.duckdb -> clean M5 subset + synthetic attributes. Tests and evals only.
#                  The agent must NEVER be given a connection to this file.
# warehouse.duckdb -> what the "customer" handed over (raw.*) plus our cleaned
#                  semantic layer (core.*). This is the only DB tools may touch.
TRUTH_DB = DATA_DIR / "ground_truth.duckdb"
WAREHOUSE_DB = DATA_DIR / "warehouse.duckdb"
DIRT_MANIFEST = DATA_DIR / "dirt_manifest.json"
# forecast.duckdb -> batch-scored forecasts + model metadata (written by training, read-only to tools)
# app.duckdb      -> PO drafts and traces (the only file the running app writes)
FORECAST_DB = DATA_DIR / "forecast.duckdb"
APP_DB = Path(os.environ.get("STOCKROOM_APP_DB", DATA_DIR / "app.duckdb"))  # override isolates demo runs
MODEL_DIR = ROOT / "models"

M5_HF_REPO = "denephew/M5_Forecasting"
M5_FILES = ("calendar.csv", "sales_train_evaluation.csv", "sell_prices.csv")

STATE = "CA"
START_DATE = "2014-05-24"  # a Saturday = start of a Walmart week
AS_OF_DATE = "2016-05-22"  # last day of M5 evaluation data; the agent's "today"
HORIZON_DAYS = 28  # forecast horizon; the event/SNAP calendar is known this far ahead

SEED = 27

# ---- agent (P4) --------------------------------------------------------------------------------
MODEL = os.environ.get("STOCKROOM_MODEL", "claude-sonnet-5")
CHEAP_MODEL = os.environ.get("STOCKROOM_CHEAP_MODEL", "claude-haiku-4-5-20251001")
EFFORT = os.environ.get("STOCKROOM_EFFORT", "medium")  # low | medium | high | xhigh | max
RUNS_DIR = ROOT / "runs"
# empty-result filter hints in run_sql (added after the P5 eval found silent case-mismatch zeros)
SQL_HINTS = os.environ.get("STOCKROOM_SQL_HINTS", "1") != "0"
