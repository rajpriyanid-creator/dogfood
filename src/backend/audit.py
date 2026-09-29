"""Append-only audit log.

Each record is chained to the previous one's hash, so the log is
tamper-evident: editing an old row breaks every hash after it. This is
NOT tamper-proof — anyone with direct database access can rewrite the
whole chain, recomputing hashes as they go. It only means an
after-the-fact edit is *detectable* by recomputing the chain, not that
it is prevented.
"""
import hashlib
import json
import secrets

from auth import now_iso


def _hash_row(prev_hash: str, at: str, actor_id, actor_role, action, resource, result, detail) -> str:
    payload = json.dumps(
        [prev_hash, at, actor_id, actor_role, action, resource, result, detail],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def record(conn, action: str, result: str, actor=None, resource: str = None,
           detail: dict = None):
    """Append one audit row, chained to whatever is currently the last
    row by seq (the table's autoincrement rowid — see schema.sql).

    The read of the previous hash and the insert of the new row must be
    one atomic unit: if two callers each read "last row" before either
    has inserted, they would both compute a hash chained to the same
    predecessor, and one of the two resulting rows would silently not
    really be chained to what ended up before it. BEGIN IMMEDIATE
    acquires SQLite's write lock up front (before the SELECT), so a
    second concurrent call blocks until the first one's INSERT and
    COMMIT are done, rather than racing on the read.
    """
    at = now_iso()
    actor_id = actor["id"] if actor else None
    actor_role = actor["role"] if actor else None
    detail_json = json.dumps(detail) if detail is not None else None
    event_id = secrets.token_hex(8)

    conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute(
            "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = last["hash"] if last else "genesis"

        row_hash = _hash_row(prev_hash, at, actor_id, actor_role, action,
                              resource, result, detail_json)

        conn.execute(
            "INSERT INTO audit_events (id, at, actor_id, actor_role, action, "
            "resource, result, detail, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, at, actor_id, actor_role, action, resource, result,
             detail_json, prev_hash, row_hash),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def verify_chain(conn) -> bool:
    """Recompute the hash chain and report whether it is intact."""
    rows = conn.execute(
        "SELECT * FROM audit_events ORDER BY seq ASC"
    ).fetchall()
    prev_hash = "genesis"
    for row in rows:
        expected = _hash_row(prev_hash, row["at"], row["actor_id"],
                              row["actor_role"], row["action"], row["resource"],
                              row["result"], row["detail"])
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return False
        prev_hash = row["hash"]
    return True
