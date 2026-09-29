"""Shared request-handling helpers: identity resolution, role guards,
JSON error responses.

The one rule this file exists to enforce: authorization is decided here,
on the server, from the session table — never from a client-supplied
role, user id, or judge id. Every sensitive route below wraps its logic
in one of these decorators or calls current_identity() explicitly and
checks the result itself.
"""
import functools

from flask import g, request, jsonify

from auth import extract_token_from_request, resolve_identity
from db import get_connection


def open_db():
    if "db" not in g:
        g.db = get_connection()
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def current_identity():
    """Resolve the requester's identity from their session token.
    Returns a sqlite3.Row (users row) or None. Cached per-request.
    """
    if not hasattr(g, "_identity_resolved"):
        token = extract_token_from_request(request)
        g._identity = resolve_identity(open_db(), token)
        g._identity_resolved = True
    return g._identity


def error(message, status=400, **extra):
    body = {"error": message}
    body.update(extra)
    resp = jsonify(body)
    resp.status_code = status
    return resp


def require_role(*roles):
    """Decorator: 401 if not authenticated, 403 if authenticated but not
    one of the allowed roles. Admin is implicitly allowed everywhere
    organizer is, since admin is a superset role.
    """
    allowed = set(roles)
    if "organizer" in allowed:
        allowed.add("admin")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            identity = current_identity()
            if identity is None:
                return error("authentication required", 401)
            if identity["role"] not in allowed:
                return error("forbidden", 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def require_auth(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if current_identity() is None:
            return error("authentication required", 401)
        return fn(*args, **kwargs)
    return wrapped
