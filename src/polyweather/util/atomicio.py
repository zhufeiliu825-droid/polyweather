"""Atomic file writes — write a temp file in the same directory then os.replace() (an atomic rename on
the same filesystem), so a concurrent reader never sees a torn/half-written file. The 24/7 daemon reads
city_profiles.json on every analysis tick and load_stations() reads stations.json at startup, while the
scheduled maintenance jobs (profile_cities / verify_new) rewrite those same files — without this, a reader
could hit a partial write and raise JSONDecodeError. Mirrors the daemon's own save_state discipline
(audit r9 SYS-1)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_text_atomic(path, text: str) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)              # atomic rename — a reader sees either the old file or the new, never a torn one


def dump_json_atomic(path, obj: Any, **kwargs) -> None:
    """json.dumps(obj, **kwargs) written atomically."""
    write_text_atomic(path, json.dumps(obj, **kwargs))
