from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "control_tower.duckdb"
TRUTH_PATH = DATA_DIR / "truth.json"


def _load(name):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def assumptions():
    return _load("assumptions.yaml")


@lru_cache
def reason_codes():
    return _load("reason_codes.yaml")


@lru_cache
def metrics_registry():
    return _load("metrics.yaml")


def flatten(d, prefix=""):
    """Flatten nested config into dotted name -> scalar pairs (lists kept as strings)."""
    out = {}
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, name + "."))
        else:
            out[name] = v
    return out
