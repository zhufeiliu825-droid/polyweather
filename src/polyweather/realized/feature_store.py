"""`DATA_DIR` — the local data directory (data/), the one symbol the live tree imports here.
(The MetarObs feature-persistence pipeline that once lived here — obs_row / write_jsonl /
write_sqlite — fed the archived build_history; removed as dead. Re-add if the SQLite-index TODO
in DATA.md is built.)"""
from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
