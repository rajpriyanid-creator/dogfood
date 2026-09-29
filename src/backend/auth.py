"""Authentication and session handling.

Identity always comes from the server-side session record, never from a
client-supplied user id, role, or judge id. A request carries only a
session token (as a cookie or a bearer token); everything else is looked
up here.
"""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

PBKDF_ITERATIONS = 120_000

# Sessions created through a normal login expire after this long. This
# is a self-hosted default, not a security-critical constant - a
# hackathon's judging window is short, so a day is generous rather than
# tight. Demo/bootstrap sessions created directly by scripts/seed.py
# pass expires_at=None deliberately (see create_session's docstring),
# since those need to keep working across container restarts for
# .dogfood.toml and the acceptance checker.
DEFAULT_SESSION_LIFETIME = timedelta(hours=24)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str):
    if ts is None:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                              PBKDF_ITERATIONS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_digest = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                              PBKDF_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hex_digest)


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 digest of a raw session token.
    This is the value stored in sessions.token_hash — the raw token
    is never written to the database.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


def new_session_token() -> str:
    return secrets.token_hex(20)


def create_session(conn, user_id: str, lifetime: timedelta = DEFAULT_SESSION_LIFETIME,
                   _fixed_raw_token: str = None):
    """Create a session. Returns the raw token (for the cookie/response).

    Pass lifetime=None for a session that never expires - this is only
    used by scripts/seed.py for the fixed demo tokens
    (the checker-provided seeded tokens) that .dogfood.toml and
    the acceptance checker rely on staying valid across restarts.
    A normal interactive login always gets a real expiry.

    _fixed_raw_token: for seed.py only — allows inserting a known token
    whose hash is stored. External callers must never set this.
    """
    token = _fixed_raw_token if _fixed_raw_token is not None else new_session_token()
    token_h = hash_token(token)
    expires_at = (
        (datetime.now(timezone.utc) + lifetime).strftime("%Y-%m-%dT%H:%M:%SZ")
        if lifetime is not None else None
    )
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (token_h, user_id, now_iso(), expires_at),
    )
    conn.commit()
    return token


def revoke_session(conn, raw_token: str):
    """Mark a session as revoked (logout). The row is kept, not
    deleted, so a revoked session's prior existence remains visible for
    audit purposes - only its ability to authenticate is removed.
    """
    conn.execute(
        "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (now_iso(), hash_token(raw_token)),
    )
    conn.commit()


def resolve_identity(conn, raw_token: str):
    """Return the users row for a session token, or None.

    This is the single place identity is derived. A caller must never
    trust a role, user id, or judge id sent by the client instead of
    this lookup. A session that has expired or been revoked (logged
    out) resolves to None exactly as if the token didn't exist at all -
    this is the audited HIGH fix: previously a session, once created,
    authenticated forever and logout never actually invalidated
    anything server-side (it only cleared the browser's cookie, so the
    same token presented via a raw header would still work).
    """
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT u.*, s.expires_at, s.revoked_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (hash_token(raw_token),),
    ).fetchone()
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None:
        if datetime.now(timezone.utc) >= _parse_iso(row["expires_at"]):
            return None
    return row


def extract_token_from_request(request) -> str:
    """Pull the session token out of a cookie or an Authorization/Cookie
    header, whichever the client sent. Supports the header form
    `.dogfood.toml` uses ("Cookie: session=...") as well as a normal
    browser cookie and a bearer token, since the client identity must
    always resolve through the same session table either way.
    """
    cookie_val = request.cookies.get("session")
    if cookie_val:
        return cookie_val
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""
