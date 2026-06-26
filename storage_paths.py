"""
storage_paths.py — resolves where MUTABLE runtime data lives.

Local dev: defaults to the repo's own data/ directory (same place the static
reference CSVs already live) — nothing changes for local development.

Production: set the AURIC_DATA_DIR environment variable to a persistent-disk
mount path (e.g. Render's "Persistent Disk" feature). Without this, anything
the app writes at runtime — user accounts, saved schedules, per-day aircraft
assignments, ops-edited flight routes, uploaded passenger lists — lives
inside the git checkout directory, which most PaaS platforms (including
Streamlit Community Cloud) wipe on every redeploy or restart.

Static reference data that's only ever edited by a developer commit
(airstrips.csv, fleet.csv) is NOT covered by this — those stay in the repo's
own data/ directory regardless of AURIC_DATA_DIR.
"""
from __future__ import annotations
import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))

SEED_DIR = os.path.join(_HERE, "data")
DATA_DIR = os.environ.get("AURIC_DATA_DIR") or SEED_DIR


def seed_if_missing(filename: str) -> str:
    """Copy {SEED_DIR}/{filename} to {DATA_DIR}/{filename} on first boot of a
    fresh persistent volume, so a new deploy starts with sensible defaults
    (e.g. the currently-configured flight routes) instead of empty. Once the
    file exists in DATA_DIR, ops' own edits there always win — this never
    overwrites it again. A no-op when DATA_DIR == SEED_DIR (local dev).

    Returns the resolved path in DATA_DIR.
    """
    dst = os.path.join(DATA_DIR, filename)
    if DATA_DIR == SEED_DIR:
        return dst
    src = os.path.join(SEED_DIR, filename)
    if not os.path.exists(dst) and os.path.exists(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
    return dst
