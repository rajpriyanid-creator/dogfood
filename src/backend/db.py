"""SQLite connection helpers for Verdict Ledger.

One file, one connection factory, foreign keys always on. Nothing fancy.
"""
import os
import sqlite3

DB_PATH = os.environ.get("VERDICT_LEDGER_DB", "/data/verdict_ledger.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema(conn=None):
    owns = conn is None
    conn = conn or get_connection()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    if owns:
        conn.close()


def is_seeded(conn) -> bool:
    """True only if a seed transaction fully committed.

    Checking "does any event exist" (the old approach) is unsafe: if the
    process crashed partway through scripts/seed.py after inserting the
    fixture event's rows but before finishing the live event, a later
    boot would see events > 0, conclude seeding was already done, and
    leave the database in that half-seeded state forever. Instead,
    seed.py writes this marker in the same transaction as its very last
    insert, so the marker's presence is equivalent to "the whole seed
    transaction committed."
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'seed_complete'"
    ).fetchone()
    return row is not None and row["value"] == "true"


def mark_seed_complete(conn):
    """Write the seed-complete marker. Callers must call this as the
    final statement of the same transaction that performs the seed
    inserts, and commit once, so the marker can never be visible without
    every preceding insert also being visible.
    """
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('seed_complete', 'true') "
        "ON CONFLICT(key) DO UPDATE SET value = 'true'"
    )
