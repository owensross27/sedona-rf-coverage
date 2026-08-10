"""Sole reader of config.yml. Everything else imports from here."""
import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_CFG_PATH = Path(os.environ.get("RFC_CONFIG", REPO_ROOT / "config.yml"))

with open(_CFG_PATH) as f:
    CFG = yaml.safe_load(f)

STATE = CFG["state"]
RF = CFG["rf"]
CLUTTER = CFG["clutter"]
GRID = CFG["grid"]
DEMAND = CFG["demand"]
SITING = CFG["siting"]
BUILDINGS = CFG["buildings"]
DQ = CFG["dq"]
SOURCES = CFG["sources"]

DATA_DIR = REPO_ROOT / "data"


def scope(name: str | None = None) -> dict:
    """Resolve a scope tier (demo|mvp|state) to its county list."""
    name = name or os.environ.get("SCOPE", "demo")
    if name not in CFG["scopes"]:
        raise ValueError(f"unknown scope {name!r}; one of {list(CFG['scopes'])}")
    return {"name": name, **CFG["scopes"][name]}


def clutter_loss_lut() -> "list[float]":
    """WorldCover class code -> excess loss dB, as a 256-entry lookup table.

    Returned as a flat array indexed by the raw uint8 raster value so the
    propagation kernel can convert clutter classes to decibels with a single
    numpy fancy-index instead of a dict lookup per sample. Classes absent from
    config.yml (including WorldCover's 0 = no data) contribute no loss.
    """
    lut = [0.0] * 256
    for code, loss in CLUTTER.items():
        lut[int(code)] = float(loss)
    return lut
