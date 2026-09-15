r"""offload_base.py - shared base for offload workers (the offloads' probe_base equivalent).

An "offload" moves BIG regeneratable bulk out of a hot place (a growing convo log, the VPS /up/
folder) into an archive or the void, and keeps a small INDEX so it never redoes finished work.
Every offload is a PROCESS worker under the Offload daemon (define_worker), the same shape as a
probe under the Probe daemon.

THE INDEX IS A DATASTORE, AND IT IS ANTI-FRAGILE BY DESIGN (Trent's rule, C:\datastores\README.md):
  - It lives in C:\datastores (big/regeneratable/NOT-backed-up), not Postgres - it's a disposable
    index of files, not a source of truth.
  - This module CREATES ITS SCHEMA ON EVERY CONNECT (CREATE TABLE IF NOT EXISTS), so you can `del`
    the .sqlite (or the whole datastore folder) any time to reclaim space and the next run just
    rebuilds it. Deleting it must NEVER crash an offload with "no such table."
  - The offloads themselves are idempotent (re-stripping an already-stripped log finds no images
    and is a clean no-op), so a lost index costs one extra scan pass, never damage.
"""
from __future__ import annotations

import os
import sqlite3
import time

# One datastore for the whole Offload daemon. Named for its owner + purpose (an index, disposable).
DATASTORE = r"C:\datastores\offload_index\offload_index.sqlite"
REGEN_CMD = r"del /q C:\datastores\offload_index\offload_index.sqlite  (the daemon rebuilds it on next run)"


def datastore() -> sqlite3.Connection:
    r"""A connection to the offload index, with the schema created if absent (self-healing).
    Caller closes. Deleting the underlying file is safe - this recreates the tables."""
    os.makedirs(os.path.dirname(DATASTORE), exist_ok=True)
    cx = sqlite3.connect(DATASTORE, timeout=20)
    cx.execute("PRAGMA journal_mode=WAL")  # concurrent read while a worker writes
    cx.executescript(
        """
        CREATE TABLE IF NOT EXISTS stripped(
            path         TEXT PRIMARY KEY,   -- the session .jsonl we stripped
            mtime        REAL,               -- its mtime AFTER we stripped it (skip if unchanged)
            size_before  INTEGER,
            size_after   INTEGER,
            blobs        INTEGER,            -- image blobs removed
            stripped_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS runs(
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            offload  TEXT,
            status   TEXT,                   -- ok | error
            detail   TEXT,
            ts       TEXT
        );
        """
    )
    cx.commit()
    return cx


def record_run(offload: str, status: str, detail: str = "") -> None:
    """Append one run to the bounded run-log in the datastore (ring buffer, last 200)."""
    try:
        cx = datastore()
        cx.execute("INSERT INTO runs(offload,status,detail,ts) VALUES(?,?,?,?)",
                   (offload, status, detail, time.strftime("%Y-%m-%d %H:%M:%S")))
        cx.execute("DELETE FROM runs WHERE id <= (SELECT COALESCE(MAX(id),0) FROM runs) - 200")
        cx.commit()
        cx.close()
    except Exception:
        pass  # the run-log is a convenience; never let it break the actual offload
