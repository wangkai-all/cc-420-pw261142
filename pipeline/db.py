"""SQLite access layer.

The platform intentionally keeps storage simple: one SQLite file shared by the
three services (rules / processor / reports) via ``DATAQ_DB``.  Append-only
tables carry the audit trail; the only mutable state is the ``run_*`` control
tables used by the worker.

Environment variables
---------------------
DATAQ_DB          path to the sqlite file (default: <repo>/data/dataq.db)
DATAQ_DB_TIMEOUT  busy timeout in seconds (default 30)
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "dataq.db"

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def db_path() -> Path:
    return Path(os.environ.get("DATAQ_DB", str(DEFAULT_DB))).expanduser()


def connect(path: Path | str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=float(os.environ.get("DATAQ_DB_TIMEOUT", "30")))
    else:
        conn = sqlite3.connect(str(p), timeout=float(os.environ.get("DATAQ_DB_TIMEOUT", "30")))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session(path: Path | str | None = None, *, readonly: bool = False, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(path, readonly=readonly)
    try:
        if immediate and not readonly:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | str | None = None, *, reset: bool = False) -> Path:
    """Create/migrate the schema.  With ``reset`` the file is truncated first."""
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if reset and p.exists():
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with session(p) as conn:
        conn.executescript(sql)
    return p


def table_counts(path: Path | str | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    with session(path, readonly=True) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            out[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    return out
