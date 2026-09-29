"""VERDICT LEDGER — main Flask application.

Single deployable service: server-rendered HTML for humans, JSON API
under /api for the acceptance checker and for the frontend's own
fetch() calls. No CDN, no external assets — everything the frontend
needs ships from /static.
"""
import csv
import hashlib
import io
import json
import os
import secrets
import sys
import math

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

import audit
from auth import (
    create_session, extract_token_from_request, hash_password, now_iso,
    revoke_session, verify_password,
)
from core import close_db, current_identity, error, open_db, require_auth, require_role
from db import get_connection, init_schema, is_seeded
from events import (
    event_status, submissions_are_open, scoring_is_open, can_run_normalization,
    parse_iso, configuration_is_frozen
)
from assignment import build_assignments
from normalization import run_normalization

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.teardown_appcontext(close_db)


def ensure_seeded():
    init_schema()
    conn = get_connection()
    if not is_seeded(conn):
        conn.close()
        import subprocess
        subprocess.run([sys.executable,
                         os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "seed.py")],
                        check=True)
    else:
        conn.close()


import time

# ---------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------

_rate_limits = {}

def check_rate_limit(action_key, max_requests=5, window_seconds=60):
    now = time.time()
    # cleanup occasionally
    if len(_rate_limits) > 1000:
        for k in list(_rate_limits.keys()):
            if _rate_limits.get(k, {}).get("expires", 0) < now:
                _rate_limits.pop(k, None)
                
    entry = _rate_limits.get(action_key)
    if not entry or entry["expires"] < now:
        _rate_limits[action_key] = {"count": 1, "expires": now + window_seconds}
        return True, 0
        
    if entry["count"] >= max_requests:
        return False, int(entry["expires"] - now)
        
    entry["count"] += 1
    return True, 0

# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        allowed, retry_after = check_rate_limit(f"login:{request.remote_addr}", max_requests=10)
        if not allowed:
            return error("too many attempts", 429, headers={"Retry-After": str(retry_after)})

    if request.method == "GET":
        return render_template("login.html", error=None)
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    db = open_db()
    user = db.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        audit.record(db, "login", "denied", detail={"email": email})
        return render_template("login.html", error="Invalid email or password."), 401
    token = create_session(db, user["id"])
    audit.record(db, "login", "ok", actor=user)
    resp = redirect(url_for("dashboard"))
    resp.set_cookie("session", token, httponly=True, samesite="Lax")
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    db = open_db()
    identity = current_identity()
    token = extract_token_from_request(request)
    if token:
        revoke_session(db, token)
        audit.record(db, "logout", "ok", actor=identity)
    resp = redirect(url_for("gallery"))
    resp.delete_cookie("session")
    return resp


@app.route("/dashboard")
@require_auth
def dashboard():
    identity = current_identity()
    if identity["role"] in ("organizer", "admin"):
        return redirect(url_for("organizer_room"))
    if identity["role"] == "judge":
        return redirect(url_for("judge_home"))
    return redirect(url_for("participant_home"))


# ---------------------------------------------------------------------
# Public gallery  (T1 — no auth required)
# ---------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("gallery"))

@app.route("/projects")
def gallery():
    db = open_db()
    q = request.args.get("q", "").strip()
    track = request.args.get("track", "").strip()
    event_id = request.args.get("event", "").strip()

    sql = ("SELECT p.*, t.name AS team_name, tr.name AS track_name, ev.name AS event_name "
           "FROM projects p "
           "JOIN teams t ON t.id = p.team_id "
           "LEFT JOIN tracks tr ON tr.id = p.track_id "
           "JOIN events ev ON ev.id = p.event_id "
           "WHERE p.status = 'submitted'")
    params = []
    if q:
        sql += " AND (p.title LIKE ? OR p.summary LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if track:
        sql += " AND p.track_id = ?"
        params.append(track)
    if event_id:
        sql += " AND p.event_id = ?"
        params.append(event_id)
    # Stable ordering is generic and independent of any particular fixture.
    sql += " ORDER BY p.id ASC"

    projects = db.execute(sql, params).fetchall()
    tracks = db.execute("SELECT DISTINCT tr.id, tr.name FROM tracks tr").fetchall()
    events = db.execute("SELECT id, name FROM events").fetchall()

    return render_template("gallery.html", projects=projects, tracks=tracks,
                            events=events, q=q, selected_track=track,
                            selected_event=event_id, identity=current_identity())


def _can_view_project(db, project, identity):
    """A submitted project is public. A draft is visible only to the
    team that owns it, or an organizer/admin — never to an anonymous
    visitor or an unrelated account, regardless of whether they know
    the project's id. This is the fix for the audited defect: the old
    version of this route had no visibility check at all, so a draft's
    id (predictable or leaked) was enough to read it.
    """
    if project["status"] == "submitted":
        return True
    if identity is None:
        return False
    if identity["role"] in ("organizer", "admin"):
        return True
    member = db.execute(
        "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
        (project["team_id"], identity["id"]),
    ).fetchone()
    return member is not None


@app.route("/projects/<project_id>")
def project_detail(project_id):
    db = open_db()
    identity = current_identity()
    project = db.execute(
        "SELECT p.*, t.name AS team_name, tr.name AS track_name "
        "FROM projects p JOIN teams t ON t.id=p.team_id "
        "LEFT JOIN tracks tr ON tr.id=p.track_id WHERE p.id=?",
        (project_id,),
    ).fetchone()
    if not project:
        return error("not found", 404)
    if not _can_view_project(db, project, identity):
        # 404, not 403: a draft's existence is not confirmed to a
        # caller who isn't allowed to see it either way.
        audit.record(db, "project_viewed", "denied", actor=identity,
                     resource=project_id, detail={"reason": "draft, not owner"})
        return error("not found", 404)

    duplicate_of = db.execute(
        "SELECT id, title FROM projects WHERE title=? AND id != ? AND event_id=? "
        "AND status='submitted'",
        (project["title"], project_id, project["event_id"]),
    ).fetchall()

    can_edit = False
    if identity is not None and identity["role"] == "participant" and project["status"] == "draft":
        can_edit = db.execute(
            "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
            (project["team_id"], identity["id"]),
        ).fetchone() is not None
    return render_template("project_detail.html", project=project,
                            duplicate_of=duplicate_of, identity=identity, can_edit=can_edit)


# ---------------------------------------------------------------------
# Submissions  (T1 — deadline enforced server-side)
# ---------------------------------------------------------------------

def _wants_json(req) -> bool:
    """True for JSON request bodies and for clients that explicitly
    prefer application/json over HTML (curl -H 'Accept: application/json',
    fetch() with that header). Browsers send Accept: text/html first,
    so they never match."""
    if req.is_json:
        return True
    best = req.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and req.accept_mimetypes["application/json"] > \
        req.accept_mimetypes["text/html"]


def _validate_repo_url(repo_url):
    """Validate that repo_url uses an allowed scheme (http or https only).
    Returns an error string if invalid, or None if valid (or empty).

    Uses proper URL parsing so scheme comparison is case-insensitive
    and a string that starts with 'http' but has an unexpected scheme
    is still caught. The field is optional, so an empty/None value is
    always accepted.
    """
    if not repo_url or not repo_url.strip():
        return None  # optional field; empty is fine
    from urllib.parse import urlparse
    try:
        parsed = urlparse(repo_url.strip())
    except Exception:
        return "repo_url is not a valid URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return ("repo_url must use http:// or https:// "
                "with a hostname")
    return None


def _parse_project_payload(request):
    """Read title/summary/repo_url/track_id from either a JSON body or a
    form post, uniformly."""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return (payload.get("title"), payload.get("summary"),
                payload.get("repo_url"), payload.get("track_id"))
    return (request.form.get("title"), request.form.get("summary"),
            request.form.get("repo_url"), request.form.get("track_id"))


def _validate_track_for_event(db, track_id, event_id):
    """A track_id must both exist and belong to the same event as the
    project being created/edited. This is the audited HIGH fix: without
    this check, a client could submit a project in event A with a
    track_id that actually belongs to event B, silently corrupting
    track-based grouping, filtering, and judge track-eligibility for
    both events. Returns None if track_id is empty (tracks are
    optional), or an error message string if invalid.
    """
    if not track_id:
        return None
    track = db.execute(
        "SELECT event_id FROM tracks WHERE id=?", (track_id,)
    ).fetchone()
    if track is None:
        return "unknown track"
    if track["event_id"] != event_id:
        return "track does not belong to this event"
    return None


def _teams_for_participant(db, identity):
    return db.execute(
        "SELECT t.* FROM teams t JOIN team_members tm ON tm.team_id=t.id "
        "WHERE tm.user_id=? ORDER BY t.event_id, t.id",
        (identity["id"],),
    ).fetchall()


def _team_for_participant(db, identity, event_id=None):
    teams = _teams_for_participant(db, identity)
    if event_id:
        return next((t for t in teams if t["event_id"] == event_id), None)
    return teams[0] if len(teams) == 1 else None


@app.route("/projects/new", methods=["GET", "POST"])
@require_role("participant")
def submit_project():
    """GET renders the draft editor. POST creates a new draft (never a
    submitted project directly) — see /projects/<id>/submit for the
    separate, explicit transition to 'submitted'. This split is the
    audited T1 fix: the old version of this route inserted a row with
    status='submitted' in a single step, so there was no way to save a
    draft, come back later, edit it, and only then submit it.
    """
    db = open_db()
    identity = current_identity()
    team = _team_for_participant(db, identity, request.args.get("event", "").strip())

    if request.method == "GET":
        tracks = []
        if team:
            tracks = db.execute(
                "SELECT id, name FROM tracks WHERE event_id=?", (team["event_id"],)
            ).fetchall()
        return render_template("submit_project.html", team=team, tracks=tracks,
                                identity=identity, error=None, project=None)

    if not team:
        return error("you must belong to a team to create a project", 400)

    event = db.execute("SELECT * FROM events WHERE id=?", (team["event_id"],)).fetchone()

    # A closed (or not-yet-open) event accepts no new projects, draft or
    # otherwise. DOGFOOD's T1 requirement is that "the deadline actually
    # stops submissions", and its acceptance check POSTs to the submit
    # route as a participant on the closed fixture event and expects a
    # 4xx. Creating a draft is the first step of submitting, so it is
    # deadline-gated exactly like editing and submitting are. (An
    # earlier revision of this route allowed draft creation after close
    # on the theory that teams should still be able to save work; the
    # official checker correctly failed that as a deadline bypass.)
    if not submissions_are_open(event):
        audit.record(db, "project_created", "denied", actor=identity,
                     resource=team["event_id"],
                     detail={"reason": "submissions_closed",
                             "event_status": event_status(event)})
        return error("submissions are not open for this event "
                     f"(status: {event_status(event)})", 403)

    title, summary, repo_url, track_id = _parse_project_payload(request)
    if not title:
        return error("title is required", 400)

    url_error = _validate_repo_url(repo_url)
    if url_error:
        return error(url_error, 400)

    track_error = _validate_track_for_event(db, track_id, team["event_id"])
    if track_error:
        return error(track_error, 400)

    project_id = f"prj_{secrets.token_hex(6)}"
    db.execute(
        "INSERT INTO projects (id, event_id, team_id, track_id, title, summary, "
        "repo_url, status, submitted_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', NULL, ?, ?)",
        (project_id, team["event_id"], team["id"], track_id or None, title, summary,
         repo_url, now_iso(), now_iso()),
    )
    db.commit()
    audit.record(db, "project_created", "ok", actor=identity, resource=project_id)

    if request.is_json:
        return jsonify({"id": project_id, "title": title, "status": "draft"}), 201
    return redirect(url_for("project_detail", project_id=project_id))


def _load_own_draft(db, identity, project_id):
    """Fetch a project the caller's team owns and that is still a draft.
    Returns (project, error_response) - exactly one is None.
    """
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return None, error("not found", 404)
    member = db.execute(
        "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
        (project["team_id"], identity["id"]),
    ).fetchone()
    if member is None:
        # 404, not 403: don't confirm the id belongs to someone else's
        # project.
        return None, error("not found", 404)
    if project["status"] != "draft":
        return None, error("this project has already been submitted and can no longer be edited", 409)
    return project, None


@app.route("/projects/<project_id>/edit", methods=["GET", "POST"])
@require_role("participant")
def edit_project(project_id):
    db = open_db()
    identity = current_identity()
    project, err = _load_own_draft(db, identity, project_id)
    if err:
        return err

    event = db.execute("SELECT * FROM events WHERE id=?", (project["event_id"],)).fetchone()

    if request.method == "GET":
        tracks = db.execute(
            "SELECT id, name FROM tracks WHERE event_id=?", (project["event_id"],)
        ).fetchall()
        return render_template("submit_project.html", team=None, tracks=tracks,
                                identity=identity, error=None, project=project)

    if not submissions_are_open(event):
        audit.record(db, "project_edited", "denied", actor=identity,
                     resource=project_id, detail={"reason": "submissions_closed"})
        return error("submissions are closed for this event; the draft can no longer be edited", 403)

    title, summary, repo_url, track_id = _parse_project_payload(request)
    if not title:
        return error("title is required", 400)
    url_error = _validate_repo_url(repo_url)
    if url_error:
        return error(url_error, 400)
    track_error = _validate_track_for_event(db, track_id, project["event_id"])
    if track_error:
        return error(track_error, 400)

    db.execute(
        "UPDATE projects SET title=?, summary=?, repo_url=?, track_id=?, updated_at=? "
        "WHERE id=?",
        (title, summary, repo_url, track_id or None, now_iso(), project_id),
    )
    db.commit()
    audit.record(db, "project_edited", "ok", actor=identity, resource=project_id)

    if request.is_json:
        return jsonify({"id": project_id, "title": title, "status": "draft"}), 200
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<project_id>/submit", methods=["POST"])
@require_role("participant")
def submit_draft(project_id):
    """The explicit draft -> submitted transition. Server-enforced
    deadline: a request made after submissions_close fails even if the
    draft was created well before the deadline and even if the client
    UI still shows a submit button (which it should not, once the
    server-derived status says closed - but the enforcement lives here,
    not there).
    """
    db = open_db()
    identity = current_identity()
    project, err = _load_own_draft(db, identity, project_id)
    if err:
        return err

    event = db.execute("SELECT * FROM events WHERE id=?", (project["event_id"],)).fetchone()
    if not submissions_are_open(event):
        audit.record(db, "project_submitted", "denied", actor=identity,
                     resource=project_id, detail={"reason": "submissions_closed"})
        return error("submissions are closed for this event", 403)

    track_error = _validate_track_for_event(db, project["track_id"], project["event_id"])
    if track_error or not project["track_id"]:
        audit.record(db, "project_submitted", "denied", actor=identity,
                     resource=project_id, detail={"reason": "track_required"})
        return error("a valid track is required before submitting a project", 400)

    db.execute(
        "UPDATE projects SET status='submitted', submitted_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), project_id),
    )
    db.commit()
    audit.record(db, "project_submitted", "ok", actor=identity, resource=project_id)

    # Response format follows what the client asked for, not a guess
    # about its Content-Type: a browser's "Submit project" button is a
    # <form> with no fields, so it posts an empty body with NO
    # Content-Type at all - indistinguishable by mimetype from a bare
    # API call. So: JSON only if the request is JSON or explicitly
    # accepts application/json; anything else (a browser) gets the
    # redirect back to the project page.
    if _wants_json(request):
        return jsonify({"id": project_id, "status": "submitted"}), 200
    return redirect(url_for("project_detail", project_id=project_id))


# ---------------------------------------------------------------------
# Participant UI
# ---------------------------------------------------------------------

@app.route("/team")
@require_role("participant")
def participant_home():
    db = open_db()
    identity = current_identity()
    teams = _teams_for_participant(db, identity)
    selected_event_id = request.args.get("event", "").strip()
    if not selected_event_id and len(teams) == 1:
        selected_event_id = teams[0]["event_id"]
    team = next((t for t in teams if t["event_id"] == selected_event_id), None)
    projects = []
    event = None
    if team:
        projects = db.execute("SELECT * FROM projects WHERE team_id=?", (team["id"],)).fetchall()
        event = db.execute("SELECT * FROM events WHERE id=?", (team["event_id"],)).fetchone()
    open_events = [e for e in db.execute(
        "SELECT * FROM events ORDER BY created_at DESC"
    ).fetchall() if submissions_are_open(e)]
    return render_template("participant_home.html", team=team, projects=projects,
                            event=event, identity=identity,
                            status=event_status(event) if event else None,
                            open_now=submissions_are_open(event) if event else False,
                            open_events=open_events, teams=teams,
                            selected_event_id=selected_event_id)


@app.route("/team/create", methods=["POST"])
@require_role("participant")
def create_team():
    db = open_db()
    identity = current_identity()
    name = request.form.get("name", "").strip()
    event_id = request.form.get("event_id", "").strip()
    if not name:
        return error("team name required", 400)
    if not event_id:
        return error("event_id is required", 400)
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return error("unknown event", 404)
    if not submissions_are_open(event):
        audit.record(db, "team_created", "denied", actor=identity, resource=event_id,
                     detail={"reason": "event not open", "event_status": event_status(event)})
        return error("this event is not open for team changes "
                     f"(status: {event_status(event)})", 403)
    already = db.execute(
        "SELECT t.id FROM teams t JOIN team_members tm ON tm.team_id=t.id "
        "WHERE tm.user_id=? AND t.event_id=?", (identity["id"], event_id),
    ).fetchone()
    if already:
        return error("you are already on a team for this event", 409)

    team_id = f"tm_{secrets.token_hex(6)}"
    db.execute(
        "INSERT INTO teams (id, event_id, name, owner_id, invite_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (team_id, event_id, name, identity["id"], secrets.token_hex(6), now_iso()),
    )
    db.execute(
        "INSERT INTO team_members (team_id, user_id, joined_at) VALUES (?, ?, ?)",
        (team_id, identity["id"], now_iso()),
    )
    db.commit()
    audit.record(db, "team_created", "ok", actor=identity, resource=team_id)
    return redirect(url_for("participant_home"))


@app.route("/team/join", methods=["POST"])
@require_role("participant")
def join_team():
    allowed, retry_after = check_rate_limit(f"join:{request.remote_addr}", max_requests=10)
    if not allowed:
        return error("too many attempts", 429, headers={"Retry-After": str(retry_after)})

    db = open_db()
    identity = current_identity()
    invite_code = request.form.get("invite_code", "").strip()
    team = db.execute("SELECT * FROM teams WHERE invite_code=?", (invite_code,)).fetchone()
    if not team:
        audit.record(db, "team_joined", "denied", actor=identity,
                     detail={"reason": "invalid invite code"})
        return error("invalid invite code", 404)

    event = db.execute("SELECT * FROM events WHERE id=?", (team["event_id"],)).fetchone()
    if not submissions_are_open(event):
        audit.record(db, "team_joined", "denied", actor=identity, resource=team["id"],
                     detail={"reason": "event not open", "event_status": event_status(event)})
        return error("this event is not open for team changes "
                     f"(status: {event_status(event)})", 403)

    # One team per participant per event. Without this a participant
    # could sit on several teams of the same event, and
    # _team_for_participant() would silently pick whichever was newest -
    # so a submission could land under a team the user didn't intend.
    already = db.execute(
        "SELECT t.id FROM teams t JOIN team_members tm ON tm.team_id=t.id "
        "WHERE tm.user_id=? AND t.event_id=?", (identity["id"], team["event_id"]),
    ).fetchone()
    if already and already["id"] != team["id"]:
        audit.record(db, "team_joined", "denied", actor=identity, resource=team["id"],
                     detail={"reason": "already on another team for this event"})
        return error("you are already on a team for this event", 409)

    db.execute(
        "INSERT OR IGNORE INTO team_members (team_id, user_id, joined_at) VALUES (?, ?, ?)",
        (team["id"], identity["id"], now_iso()),
    )
    db.commit()
    audit.record(db, "team_joined", "ok", actor=identity, resource=team["id"])
    return redirect(url_for("participant_home"))


# ---------------------------------------------------------------------
# Judge UI + API  (T2 — backend-enforced isolation)
# ---------------------------------------------------------------------

@app.route("/judge")
@require_role("judge")
def judge_home():
    db = open_db()
    identity = current_identity()
    event_rows = db.execute(
        "SELECT DISTINCT e.* FROM events e "
        "LEFT JOIN assignments a ON a.event_id=e.id AND a.judge_id=? "
        "LEFT JOIN tracks t ON t.event_id=e.id "
        "LEFT JOIN judge_track_eligibility jte ON jte.track_id=t.id "
        "WHERE a.id IS NOT NULL OR jte.user_id=? ORDER BY e.created_at, e.id",
        (identity["id"], identity["id"]),
    ).fetchall()
    selected_event_id = request.args.get("event", "").strip()
    if not selected_event_id and len(event_rows) == 1:
        selected_event_id = event_rows[0]["id"]
    selected_event = next((e for e in event_rows if e["id"] == selected_event_id), None)
    if selected_event is None:
        return render_template("judge_home.html", assignments=[], events=event_rows,
                               selected_event=None, identity=identity)
    assignments = db.execute(
        "SELECT a.*, p.title, p.summary, p.repo_url, p.event_id, "
        "  (SELECT COUNT(*) FROM scores sc WHERE sc.judge_id=a.judge_id AND sc.project_id=a.project_id) AS scored "
        "FROM assignments a JOIN projects p ON p.id = a.project_id "
        "WHERE a.judge_id=? AND a.event_id=? ORDER BY p.id",
        (identity["id"], selected_event_id),
    ).fetchall()
    return render_template("judge_home.html", assignments=assignments,
                           events=event_rows, selected_event=selected_event,
                           identity=identity)


@app.route("/judge/review/<project_id>", methods=["GET", "POST"])
@require_role("judge")
def judge_review(project_id):
    db = open_db()
    identity = current_identity()

    assignment = db.execute(
        "SELECT * FROM assignments WHERE judge_id=? AND project_id=?",
        (identity["id"], project_id),
    ).fetchone()
    if not assignment:
        audit.record(db, "score_submitted", "denied", actor=identity,
                     resource=project_id, detail={"reason": "not assigned"})
        return error("you are not assigned to this project", 403)

    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    event = db.execute("SELECT * FROM events WHERE id=?", (project["event_id"],)).fetchone()

    existing_score = db.execute(
        "SELECT * FROM scores WHERE judge_id=? AND project_id=?",
        (identity["id"], project_id),
    ).fetchone()

    # Rubric-version pinning (audited CRITICAL fix): an existing score
    # must keep being edited against the *same* rubric_version_id it was
    # originally created under, not whatever the current latest rubric
    # version happens to be. Without this, editing an old score after an
    # organizer publishes a new rubric would silently recompute
    # raw_weighted using the new weights/criteria while the row's own
    # rubric_version_id kept pointing at the old version - the stored
    # score and the rubric it claims to be scored under would disagree.
    # A brand-new score always uses the current latest version, since
    # there is no prior version to stay pinned to.
    if existing_score:
        rubric_version = db.execute(
            "SELECT * FROM rubric_versions WHERE id=?",
            (existing_score["rubric_version_id"],),
        ).fetchone()
    else:
        rubric_version = db.execute(
            "SELECT * FROM rubric_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (project["event_id"],),
        ).fetchone()

    if not rubric_version:
        return error("this event has no rubric configured", 409)

    criteria = db.execute(
        "SELECT * FROM rubric_criteria WHERE rubric_version_id=?", (rubric_version["id"],)
    ).fetchall()

    if request.method == "GET":
        existing_values = {}
        if existing_score:
            rows = db.execute(
                "SELECT criterion_key, value FROM score_criteria WHERE score_id=?",
                (existing_score["id"],),
            ).fetchall()
            existing_values = {r["criterion_key"]: r["value"] for r in rows}
        return render_template("judge_review.html", project=project, criteria=criteria,
                                existing_score=existing_score, existing_values=existing_values,
                                identity=identity)

    # Judging-state enforcement (audited P0 fix): scoring is only
    # permitted once submissions have closed and before results are
    # published/archived. This blocks both "scoring before there's
    # anything final to judge" and "scoring forever after publish."
    if not scoring_is_open(event):
        audit.record(db, "score_submitted" if not existing_score else "score_updated",
                     "denied", actor=identity, resource=project_id,
                     detail={"reason": "judging window closed", "event_status": event_status(event)})
        return error("scoring is not open for this event right now "
                     f"(status: {event_status(event)})", 403)

    # Validate every criterion the pinned rubric version actually
    # requires is present (audited HIGH fix: previously, a missing
    # criterion was silently treated as absent from the weighted sum
    # rather than rejected, which would score an incomplete review too
    # low instead of refusing it outright).
    values = {}
    missing = []
    for c in criteria:
        raw = request.form.get(f"criterion_{c['key']}")
        if raw is None or raw == "":
            missing.append(c["key"])
            continue
        try:
            val = float(raw)
        except ValueError:
            return error(f"invalid value for {c['key']}", 400)
        if not (c["min_value"] <= val <= c["max_value"]):
            return error(f"{c['key']} must be between {c['min_value']} and {c['max_value']}", 400)
        values[c["key"]] = val
    if missing:
        return error("missing required criteria: " + ", ".join(missing), 400)

    comment = request.form.get("comment", "").strip() or None
    weights = {c["key"]: c["weight"] for c in criteria}
    raw_weighted = sum(weights[k] * v for k, v in values.items())

    score_id = existing_score["id"] if existing_score else f"score_{secrets.token_hex(6)}"
    if existing_score:
        db.execute(
            "UPDATE scores SET comment=?, raw_weighted=?, updated_at=? WHERE id=?",
            (comment, raw_weighted, now_iso(), score_id),
        )
        db.execute("DELETE FROM score_criteria WHERE score_id=?", (score_id,))
        action = "score_updated"
    else:
        db.execute(
            "INSERT INTO scores (id, assignment_id, judge_id, project_id, "
            "rubric_version_id, comment, raw_weighted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (score_id, assignment["id"], identity["id"], project_id,
             rubric_version["id"], comment, raw_weighted, now_iso(), now_iso()),
        )
        action = "score_submitted"

    for key, val in values.items():
        db.execute(
            "INSERT INTO score_criteria (score_id, criterion_key, value) VALUES (?, ?, ?)",
            (score_id, key, val),
        )
    db.commit()
    audit.record(db, action, "ok", actor=identity, resource=project_id)
    return redirect(url_for("judge_home"))


@app.route("/api/judge/scores")
@require_role("judge")
def api_judge_own_scores():
    """A judge's own scores. Identity comes from the session, never from
    a query parameter — see api_judge_peer_scores for what happens when
    the client tries to ask for someone else's."""
    db = open_db()
    identity = current_identity()
    requested_judge = request.args.get("judge")

    # The identity that matters is the session's, not the query string.
    # If a judge parameter is present and does not match the caller,
    # this is exactly the peer-access attempt the checker probes for:
    # refuse it here too, not only on the /api/judges/<id>/scores form.
    if requested_judge and requested_judge != identity["id"]:
        audit.record(db, "score_read", "denied", actor=identity,
                     resource=requested_judge, detail={"reason": "peer scores"})
        return error("you may only read your own scores", 403)

    rows = db.execute(
        "SELECT s.*, p.title AS project_title FROM scores s "
        "JOIN projects p ON p.id = s.project_id "
        "JOIN assignments a ON a.id = s.assignment_id "
        "  AND a.judge_id = s.judge_id AND a.project_id = s.project_id "
        "  AND a.event_id = p.event_id "
        "WHERE s.judge_id=?",
        (identity["id"],),
    ).fetchall()
    result = []
    for r in rows:
        crit = db.execute(
            "SELECT criterion_key, value FROM score_criteria WHERE score_id=?", (r["id"],)
        ).fetchall()
        result.append({
            "project_id": r["project_id"],
            "project_title": r["project_title"],
            "raw_weighted": r["raw_weighted"],
            "comment": r["comment"],
            "criteria": {c["criterion_key"]: c["value"] for c in crit},
        })
    audit.record(db, "score_read", "ok", actor=identity)
    return jsonify({"judge_id": identity["id"], "scores": result})


@app.route("/api/judges/<judge_id>/scores")
@require_role("judge")
def api_judge_scores_by_path(judge_id):
    """The path form of the same endpoint: /api/judges/<id>/scores. Same
    rule as the query-parameter form — only your own session's identity
    may be read, regardless of which judge id appears in the URL.
    """
    db = open_db()
    identity = current_identity()
    if judge_id != identity["id"]:
        audit.record(db, "score_read", "denied", actor=identity, resource=judge_id,
                     detail={"reason": "peer scores (path form)"})
        return error("you may only read your own scores", 403)
    return api_judge_own_scores()


@app.route("/judge/progress")
@app.route("/judge/progress/<event_id>")
@require_role("organizer")
def judge_progress(event_id=None):
    db = open_db()
    events = db.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    if event_id is None:
        return render_template("judge_progress.html", rows=[], events=events,
                               selected_event=None, identity=current_identity())
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return error("event not found", 404)
    rows = db.execute(
        "SELECT u.id, u.name, COUNT(DISTINCT a.id) AS assigned, "
        "  COUNT(DISTINCT CASE WHEN sc.id IS NOT NULL THEN a.id END) AS completed "
        "FROM users u "
        "JOIN judge_track_eligibility jte ON jte.user_id=u.id "
        "JOIN tracks t ON t.id=jte.track_id AND t.event_id=? "
        "LEFT JOIN assignments a ON a.judge_id=u.id AND a.event_id=? "
        "LEFT JOIN scores sc ON sc.assignment_id=a.id "
        "WHERE u.role='judge' GROUP BY u.id ORDER BY u.id",
        (event_id, event_id)
    ).fetchall()
    return render_template("judge_progress.html", rows=rows, events=events,
                           selected_event=event, identity=current_identity())


# ---------------------------------------------------------------------
# Organizer control room
# ---------------------------------------------------------------------

@app.route("/organizer")
@require_role("organizer")
def organizer_room():
    db = open_db()
    events = db.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    stats = {}
    for ev in events:
        teams_n = db.execute("SELECT COUNT(*) AS n FROM teams WHERE event_id=?", (ev["id"],)).fetchone()["n"]
        projects_n = db.execute("SELECT COUNT(*) AS n FROM projects WHERE event_id=?", (ev["id"],)).fetchone()["n"]
        assigned_n = db.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE event_id=?", (ev["id"],)
        ).fetchone()["n"]
        completed_n = db.execute(
            "SELECT COUNT(*) AS n FROM scores s JOIN assignments a "
            "ON a.judge_id=s.judge_id AND a.project_id=s.project_id WHERE a.event_id=?",
            (ev["id"],),
        ).fetchone()["n"]
        has_run = db.execute(
            "SELECT 1 FROM normalization_runs WHERE event_id=? LIMIT 1", (ev["id"],)
        ).fetchone() is not None
        stats[ev["id"]] = {
            "teams": teams_n, "projects": projects_n,
            "assigned": assigned_n, "completed": completed_n,
            "remaining": max(0, assigned_n - completed_n),
            "status": event_status(ev),
            "can_normalize": can_run_normalization(ev),
            "can_publish": has_run and can_run_normalization(ev)
                           and event_status(ev) not in ("published", "archived"),
        }
    return render_template("organizer_room.html", events=events, stats=stats,
                            identity=current_identity())


def _validate_event_payload(name, start_at_raw, close_raw, judging_close_raw):
    """Backend-independent validation for event creation/configuration
    (audited CRITICAL fix: this functionality did not exist at all
    before). Returns a list of error strings; empty means valid.
    Frontend validation, if any, is a convenience only - this is what
    actually decides whether the event is created.
    """
    errors = []
    if not name or not name.strip():
        errors.append("name is required")

    def _try_parse(raw, field):
        if not raw:
            errors.append(f"{field} is required")
            return None
        try:
            return parse_iso(raw)
        except ValueError:
            errors.append(f"{field} is not a valid timestamp")
            return None

    start_dt = _try_parse(start_at_raw, "start time") if start_at_raw else None
    close_dt = _try_parse(close_raw, "submission close time")
    judging_close_dt = _try_parse(judging_close_raw, "judging close time") if judging_close_raw else None

    if start_dt and close_dt and close_dt <= start_dt:
        errors.append("submission close time must be after the start time")
    if close_dt and judging_close_dt and judging_close_dt <= close_dt:
        errors.append("judging close time must be after the submission close time")

    return errors


@app.route("/organizer/events/new", methods=["GET", "POST"])
@require_role("organizer")
def create_event():
    db = open_db()
    identity = current_identity()

    if request.method == "GET":
        return render_template("create_event.html", identity=identity, error=None)

    payload = request.get_json(silent=True) if request.is_json else request.form
    if request.is_json and not isinstance(payload, dict):
        return error("JSON body must be an object", 400)

    def _string_field(key, default=None):
        value = payload.get(key, default)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} must be a string")
            return default
        return value

    errors = []
    name = _string_field("name", "")
    description = _string_field("description", "")
    start_at = _string_field("start_at")
    submissions_close = _string_field("submissions_close")
    judging_close = _string_field("judging_close")

    def _list_field(key):
        if not request.is_json:
            raw = payload.get(key, "")
            return [item.strip() for item in raw.split(",") if item.strip()]
        value = payload.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            errors.append(f"{key} must be a list")
            return []
        if any(not isinstance(item, str) for item in value):
            errors.append(f"every {key} entry must be a string")
            return []
        values = [item.strip() for item in value]
        if any(not item for item in values):
            errors.append(f"{key} entries must not be empty")
        return [item for item in values if item]

    track_names = _list_field("tracks")
    prize_names = _list_field("prizes")
    name = (name or "").strip()
    if request.is_json:
        description = (description or "").strip() or None
        start_at = start_at or None
        judging_close = judging_close or None
    else:
        description = description.strip() or None
        start_at = start_at or None
        judging_close = judging_close or None

    errors.extend(_validate_event_payload(name, start_at, submissions_close, judging_close))
    # Reject duplicate track identifiers within the same event up front
    # (audited requirement) - case-sensitive exact-name duplicates here,
    # since the schema's own UNIQUE(event_id, name) is the backstop that
    # makes this non-bypassable even if this check were ever skipped.
    if len(track_names) != len(set(track_names)):
        errors.append("duplicate track names are not allowed")
    if errors:
        if request.is_json:
            return error("; ".join(errors), 400)
        return render_template("create_event.html", identity=identity,
                                error="; ".join(errors)), 400

    event_id = f"evt_{secrets.token_hex(6)}"
    db.execute(
        "INSERT INTO events (id, name, description, kind, start_at, "
        "submissions_close, judging_close, publish_state, created_by, created_at) "
        "VALUES (?, ?, ?, 'live', ?, ?, ?, 'draft', ?, ?)",
        (event_id, name, description, start_at, submissions_close, judging_close,
         identity["id"], now_iso()),
    )
    track_ids = []
    for t_name in track_names:
        track_id = f"trk_{secrets.token_hex(6)}"
        db.execute("INSERT INTO tracks (id, event_id, name) VALUES (?, ?, ?)",
                    (track_id, event_id, t_name))
        track_ids.append(track_id)
    for p_name in prize_names:
        db.execute("INSERT INTO prizes (id, event_id, name) VALUES (?, ?, ?)",
                    (f"prz_{secrets.token_hex(6)}", event_id, p_name))
    db.commit()
    audit.record(db, "event_created", "ok", actor=identity, resource=event_id,
                 detail={"name": name})

    if request.is_json:
        return jsonify({"id": event_id, "name": name, "tracks": track_ids}), 201
    return redirect(url_for("organizer_room"))


@app.route("/organizer/events/<event_id>/tracks", methods=["POST"])
@require_role("organizer")
def add_track(event_id):
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        return error("event not found", 404)
    if configuration_is_frozen(event):
        return error("event configuration is frozen once judging starts", 403)

    name = (request.get_json(silent=True) or {}).get("name", "").strip() if request.is_json \
        else request.form.get("name", "").strip()
    if not name:
        return error("track name is required", 400)
    existing = db.execute(
        "SELECT 1 FROM tracks WHERE event_id=? AND name=?", (event_id, name)
    ).fetchone()
    if existing:
        return error("a track with this name already exists for this event", 400)

    track_id = f"trk_{secrets.token_hex(6)}"
    db.execute("INSERT INTO tracks (id, event_id, name) VALUES (?, ?, ?)",
                (track_id, event_id, name))
    db.commit()
    audit.record(db, "track_created", "ok", actor=identity, resource=track_id,
                 detail={"event_id": event_id, "name": name})
    return jsonify({"id": track_id, "name": name}), 201


@app.route("/organizer/events/<event_id>/prizes", methods=["POST"])
@require_role("organizer")
def add_prize(event_id):
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        return error("event not found", 404)
    if configuration_is_frozen(event):
        return error("event configuration is frozen once judging starts", 403)

    payload = request.get_json(silent=True) if request.is_json else request.form
    name = payload.get("name", "").strip()
    description = payload.get("description", "").strip() or None
    if not name:
        return error("prize name is required", 400)

    prize_id = f"prz_{secrets.token_hex(6)}"
    db.execute("INSERT INTO prizes (id, event_id, name, description) VALUES (?, ?, ?, ?)",
                (prize_id, event_id, name, description))
    db.commit()
    audit.record(db, "prize_created", "ok", actor=identity, resource=prize_id,
                 detail={"event_id": event_id, "name": name})
    return jsonify({"id": prize_id, "name": name}), 201


# ---------------------------------------------------------------------
# Judge invitation and assignment management
# ---------------------------------------------------------------------

@app.route("/organizer/events/<event_id>/judges", methods=["GET", "POST"])
@require_role("organizer")
def manage_judges(event_id):
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        return error("event not found", 404)

    if request.method == "GET":
        judges = db.execute(
            "SELECT u.id, u.name, u.email, "
            "  GROUP_CONCAT(jte.track_id) AS track_ids "
            "FROM users u LEFT JOIN judge_track_eligibility jte ON jte.user_id=u.id "
            "  AND jte.track_id IN (SELECT id FROM tracks WHERE event_id=?) "
            "WHERE u.role='judge' GROUP BY u.id ORDER BY u.id",
            (event_id,)
        ).fetchall()
        tracks = db.execute("SELECT id, name FROM tracks WHERE event_id=?", (event_id,)).fetchall()
        return render_template("manage_judges.html", judges=judges, tracks=tracks,
                                event=event, identity=identity)

    # POST: invite/create a judge account and set their track eligibility
    # for this event. "Invitation" in a self-hosted, offline-first tool
    # means an organizer directly provisions the account (there is no
    # external email service to send a real invite through) - this is a
    # deliberate design choice, documented in ARCHITECTURE.md, not a
    # stand-in for a missing email integration.
    if configuration_is_frozen(event):
        return error("event configuration is frozen once judging starts", 403)

    if request.method == "POST":
        allowed, retry_after = check_rate_limit(f"invite:{request.remote_addr}", max_requests=10)
        if not allowed:
            return error("too many attempts", 429, headers={"Retry-After": str(retry_after)})

    payload = request.get_json(silent=True) if request.is_json else request.form
    name = payload.get("name", "").strip()
    email = payload.get("email", "").strip().lower()
    track_ids = payload.get("track_ids") or []
    if not request.is_json and request.form.get("track_ids"):
        track_ids = request.form.getlist("track_ids")

    if not name or not email:
        return error("name and email are required", 400)

    # Validate the event-scoped track set before creating a user, so a bad
    # invitation cannot leave a partial judge account behind.
    for track_id in track_ids:
        track = db.execute("SELECT event_id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if track is None or track["event_id"] != event_id:
            return error(f"track {track_id} does not belong to this event", 400)

    initial_password = None
    existing_user = db.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if existing_user:
        if existing_user["role"] != "judge":
            return error("this email belongs to an existing non-judge account", 400)
        judge_id = existing_user["id"]
    else:
        judge_id = f"jdg_{secrets.token_hex(6)}"
        initial_password = secrets.token_urlsafe(18)
        db.execute(
            "INSERT INTO users (id, email, name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, 'judge', ?, ?)",
            (judge_id, email, name, hash_password(initial_password), now_iso()),
        )

    for track_id in track_ids:
        db.execute(
            "INSERT OR IGNORE INTO judge_track_eligibility (user_id, track_id) VALUES (?, ?)",
            (judge_id, track_id),
        )
    db.commit()
    audit.record(db, "judge_invited", "ok", actor=identity, resource=judge_id,
                 detail={"event_id": event_id, "email": email})

    if request.is_json:
        return jsonify({"id": judge_id, "email": email,
                        "initial_password": initial_password}), 201
    return render_template("judge_invitation_success.html", event=event,
                           email=email, initial_password=initial_password,
                           identity=identity)


@app.route("/organizer/events/<event_id>/assignments", methods=["GET", "POST"])
@require_role("organizer")
def manage_assignments(event_id):
    """GET shows current assignments; POST (re)generates them using the
    deterministic assignment engine (audited CRITICAL fix: this had no
    route at all - assignment.py's engine existed but nothing in the
    running application ever called it for a live event other than the
    one-time call baked into scripts/seed.py).
    """
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        return error("event not found", 404)

    if request.method == "GET":
        rows = db.execute(
            "SELECT a.*, u.name AS judge_name, p.title AS project_title "
            "FROM assignments a JOIN users u ON u.id=a.judge_id "
            "JOIN projects p ON p.id=a.project_id WHERE a.event_id=? "
            "ORDER BY p.id, u.id",
            (event_id,),
        ).fetchall()
        return render_template("manage_assignments.html", rows=rows, event=event,
                                identity=identity)

    if configuration_is_frozen(event):
        return error("event configuration is frozen once judging starts", 403)

    try:
        raw_target = (request.get_json(silent=True) or {}).get("target_reviews", 3) if request.is_json else request.form.get("target_reviews", 3)
        target = int(raw_target)
        if target < 1 or target > 50:
            raise ValueError()
    except (ValueError, TypeError):
        return error("target_reviews must be an integer between 1 and 50", 400)

    projects = db.execute(
        "SELECT id, track_id, team_id FROM projects WHERE event_id=? AND status='submitted'",
        (event_id,),
    ).fetchall()
    judges_by_track = {}
    for row in db.execute(
        "SELECT track_id, user_id FROM judge_track_eligibility jte "
        "JOIN tracks t ON t.id=jte.track_id WHERE t.event_id=?", (event_id,)
    ).fetchall():
        judges_by_track.setdefault(row["track_id"], []).append(row["user_id"])

    team_owner_by_project = {}
    for p in projects:
        members = db.execute(
            "SELECT user_id FROM team_members WHERE team_id=?", (p["team_id"],)
        ).fetchall()
        team_owner_by_project[p["id"]] = {m["user_id"] for m in members}

    existing_rows = db.execute(
        "SELECT judge_id, project_id FROM assignments WHERE event_id=?",
        (event_id,),
    ).fetchall()
    existing_pairs = {(r["judge_id"], r["project_id"]) for r in existing_rows}
    initial_workload = {}
    for row in existing_rows:
        initial_workload[row["judge_id"]] = initial_workload.get(row["judge_id"], 0) + 1

    pairs = build_assignments(
        [dict(p) for p in projects], judges_by_track, team_owner_by_project,
        target_reviews_per_project=target,
        initial_workload=initial_workload,
        existing_pairs=existing_pairs,
    )
    created_pairs = []
    for judge_id, project_id in pairs:
        aid = f"asg_{judge_id}_{project_id}"
        cur = db.execute(
            "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, project_id, "
            "version, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (aid, event_id, judge_id, project_id, now_iso()),
        )
        if cur.rowcount:
            created_pairs.append((judge_id, project_id))
    db.commit()
    created = len(created_pairs)
    # One audit row per newly created assignment (who was assigned what),
    # plus one summary row for the generation run itself.
    for judge_id, project_id in created_pairs:
        audit.record(db, "judge_assigned", "ok", actor=identity, resource=project_id,
                     detail={"judge_id": judge_id, "event_id": event_id})
    audit.record(db, "assignments_generated", "ok", actor=identity, resource=event_id,
                 detail={"pairs_considered": len(pairs), "newly_created": created})

    if request.is_json:
        return jsonify({"pairs_considered": len(pairs), "newly_created": created}), 200
    return redirect(url_for("manage_assignments", event_id=event_id))


# ---------------------------------------------------------------------
# Rubric builder
# ---------------------------------------------------------------------

def validate_rubric_criteria(criteria: list) -> list:
    """The one production validator for rubric criteria - used by both
    the rubric-builder route below and by tests directly (audited fix:
    a prior test suite validated a local copy of this logic rather than
    a production function, so a broken production validator could not
    have been caught by that test). Returns a list of error strings;
    empty means valid.

    criteria: list of {"key": str, "label": str, "weight": float,
    "min_value": float, "max_value": float}.
    """
    errors = []
    if not criteria:
        errors.append("at least one criterion is required")
        return errors

    keys = [c.get("key", "").strip() for c in criteria]
    if any(not k for k in keys):
        errors.append("every criterion needs a non-empty key")
    if len(keys) != len(set(keys)):
        errors.append("criterion keys must not be duplicated")

    total_weight = 0.0
    for c in criteria:
        weight = c.get("weight")
        if weight is None:
            errors.append(f"{c.get('key', '?')}: weight is required")
            continue
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            errors.append(f"{c.get('key', '?')}: weight must be a number")
            continue
        if not math.isfinite(weight):
            errors.append(f"{c.get('key', '?')}: weight must be finite")
            continue
        if weight < 0:
            errors.append(f"{c.get('key', '?')}: weight must be >= 0")
        total_weight += weight

        lo = c.get("min_value", 1)
        hi = c.get("max_value", 5)
        try:
            lo, hi = float(lo), float(hi)
        except (TypeError, ValueError):
            errors.append(f"{c.get('key', '?')}: min_value/max_value must be numbers")
            continue
        if not math.isfinite(lo) or not math.isfinite(hi):
            errors.append(f"{c.get('key', '?')}: min_value/max_value must be finite")
            continue
        if hi <= lo:
            errors.append(f"{c.get('key', '?')}: max_value must be greater than min_value")

    # Tolerant floating point comparison, not exact equality - weights
    # entered as e.g. 0.1 + 0.2 + 0.7 must not be rejected over a
    # trailing float rounding error.
    if not errors and abs(total_weight - 1.0) > 1e-6:
        errors.append(f"weights must sum to 1.0 (got {total_weight:.6f})")

    return errors


def weighted_score_bounds(criteria):
    """Return the attainable weighted raw-score interval for a rubric."""
    return (
        sum(c["weight"] * c["min_value"] for c in criteria),
        sum(c["weight"] * c["max_value"] for c in criteria),
    )


@app.route("/organizer/events/<event_id>/rubric", methods=["GET", "POST"])
@require_role("organizer")
def rubric_builder(event_id):
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        return error("event not found", 404)

    if request.method == "GET":
        current = db.execute(
            "SELECT * FROM rubric_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        criteria = []
        if current:
            criteria = db.execute(
                "SELECT * FROM rubric_criteria WHERE rubric_version_id=?", (current["id"],)
            ).fetchall()
        return render_template("rubric_builder.html", event=event, current=current,
                                criteria=criteria, identity=identity, error=None)

    if configuration_is_frozen(event):
        return error("event configuration is frozen once judging starts", 403)

    payload = request.get_json(silent=True) if request.is_json else None
    if payload is None:
        # Form fallback: criterion_key[], criterion_label[], criterion_weight[]
        keys = request.form.getlist("criterion_key")
        labels = request.form.getlist("criterion_label")
        weights = request.form.getlist("criterion_weight")
        mins = request.form.getlist("criterion_min") or ["1"] * len(keys)
        maxs = request.form.getlist("criterion_max") or ["5"] * len(keys)
        criteria = [
            {"key": k, "label": l, "weight": w, "min_value": mn, "max_value": mx}
            for k, l, w, mn, mx in zip(keys, labels, weights, mins, maxs)
            # The HTML form always renders a fixed number of rows; rows
            # the organizer left entirely blank are not criteria and are
            # dropped here rather than failing validation as "empty key".
            if (k or "").strip() or (w or "").strip()
        ]
    else:
        criteria = payload.get("criteria", [])

    errors = validate_rubric_criteria(criteria)
    if errors:
        if request.is_json:
            return error("; ".join(errors), 400)
        current = db.execute(
            "SELECT * FROM rubric_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return render_template("rubric_builder.html", event=event, current=current,
                                criteria=[], identity=identity,
                                error="; ".join(errors)), 400

    # New rubric version - never mutates an existing one (see
    # DATA-MODEL.md / JUDGING.md rubric versioning). Any score already
    # created stays pinned to whichever version it was scored under
    # (see judge_review()).
    existing = db.execute(
        "SELECT MAX(version) AS v FROM rubric_versions WHERE event_id=?", (event_id,)
    ).fetchone()
    version = (existing["v"] or 0) + 1
    rubric_id = f"{event_id}_rubric_v{version}"
    db.execute(
        "INSERT INTO rubric_versions (id, event_id, version, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (rubric_id, event_id, version, now_iso(), identity["id"]),
    )
    for c in criteria:
        db.execute(
            "INSERT INTO rubric_criteria (id, rubric_version_id, key, label, "
            "description, weight, min_value, max_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{rubric_id}_{c['key']}", rubric_id, c["key"], c.get("label") or c["key"],
             c.get("description"), float(c["weight"]),
             float(c.get("min_value", 1)), float(c.get("max_value", 5))),
        )
    db.commit()
    audit.record(db, "rubric_created", "ok", actor=identity, resource=rubric_id,
                 detail={"event_id": event_id, "version": version})

    if request.is_json:
        return jsonify({"id": rubric_id, "version": version}), 201
    return redirect(url_for("rubric_builder", event_id=event_id))


@app.route("/organizer/integrity/<event_id>")
@require_role("organizer")
def integrity_view(event_id):
    """Evidence about the judging data for one event. Every figure is
    computed from persisted rows on each request; nothing is cached or
    hard-coded.

    Scores are attributed to an event through their PROJECT, not via a
    join on assignments: a score whose assignment row is missing is
    still a score and must still be counted (the previous query silently
    dropped such rows).
    """
    db = open_db()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return error("event not found", 404)

    duplicates = db.execute(
        "SELECT title, GROUP_CONCAT(id) AS ids FROM projects "
        "WHERE event_id=? AND status='submitted' "
        "GROUP BY title HAVING COUNT(*) > 1",
        (event_id,),
    ).fetchall()

    # One row per score. Per-judge mean/variance need every individual
    # raw_weighted value and stock SQLite has no VARIANCE aggregate, so
    # rows are fetched ungrouped and grouped in Python. (The original
    # defect here was a bare COUNT(*) with no GROUP BY, which SQLite
    # collapses to ONE row for the whole event.)
    rows = db.execute(
        "SELECT s.judge_id, u.name, s.raw_weighted, s.comment, "
        "       p.track_id AS project_track "
        "FROM scores s JOIN projects p ON p.id = s.project_id "
        "JOIN users u ON u.id = s.judge_id "
        "WHERE p.event_id=? ORDER BY s.judge_id, s.id",
        (event_id,),
    ).fetchall()

    from collections import defaultdict
    grouped = defaultdict(list)
    names = {}
    for r in rows:
        grouped[r["judge_id"]].append(r["raw_weighted"])
        names[r["judge_id"]] = r["name"]

    judge_table = []
    for jid in sorted(grouped):
        scores = grouped[jid]
        n = len(scores)
        mean_val = sum(scores) / n
        var = sum((x - mean_val) ** 2 for x in scores) / n
        judge_table.append({"judge_id": jid, "name": names[jid], "n": n,
                            "mean": mean_val, "variance": var,
                            "zero_variance": n >= 2 and var == 0,
                            "low_sample": n <= 1})

    zero_variance = [j for j in judge_table if j["zero_variance"]]
    low_sample = [j for j in judge_table if j["low_sample"]]

    # High workload = strictly more than 1.5x the median review count.
    # A relative threshold, because what counts as "a lot" depends on
    # the event; it is a flag for an organizer to look at, not a rule.
    counts = sorted(j["n"] for j in judge_table)
    median = (counts[len(counts) // 2] if len(counts) % 2 else
              (counts[len(counts) // 2 - 1] + counts[len(counts) // 2]) / 2) if counts else 0
    high_workload = [j for j in judge_table if median and j["n"] > 1.5 * median]

    # Track mismatch: a judge scored a project in a track they are NOT
    # registered as eligible for. Only meaningful for judges who have
    # any track eligibility recorded at all.
    mismatches = db.execute(
        "SELECT s.judge_id, u.name, p.id AS project_id, p.title, p.track_id "
        "FROM scores s JOIN projects p ON p.id = s.project_id "
        "JOIN users u ON u.id = s.judge_id "
        "WHERE p.event_id=? AND p.track_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM judge_track_eligibility e WHERE e.user_id = s.judge_id) "
        "AND NOT EXISTS (SELECT 1 FROM judge_track_eligibility e "
        "                WHERE e.user_id = s.judge_id AND e.track_id = p.track_id) "
        "ORDER BY s.judge_id, p.id",
        (event_id,),
    ).fetchall()

    missing_comments = sum(1 for r in rows if not (r["comment"] or "").strip())

    coverage = db.execute(
        "SELECT p.id, p.title, COUNT(s.id) AS reviews FROM projects p "
        "LEFT JOIN scores s ON s.project_id = p.id "
        "WHERE p.event_id=? AND p.status='submitted' GROUP BY p.id ORDER BY reviews, p.id",
        (event_id,),
    ).fetchall()
    coverage_dist = defaultdict(int)
    for c in coverage:
        coverage_dist[c["reviews"]] += 1

    return render_template("integrity.html", event=event, event_id=event_id,
                            duplicates=duplicates, judge_table=judge_table,
                            zero_variance=zero_variance, low_sample=low_sample,
                            high_workload=high_workload, workload_median=median,
                            mismatches=mismatches, missing_comments=missing_comments,
                            total_reviews=len(rows),
                            coverage_dist=sorted(coverage_dist.items()),
                            identity=current_identity())


def _compute_judging_state_fingerprint(db, event_id: str) -> str:
    """Compute a deterministic SHA-256 fingerprint of all result-affecting
    judging inputs for an event.

    Inputs included (sorted for determinism):
    - rubric versions and their criteria (key, weight, min/max)
    - active judge assignments (judge_id, project_id)
    - score records (id, judge_id, project_id, rubric_version_id, raw_weighted)
    - score criteria values (score_id, criterion_key, value)

    NOT included: timestamps, comments, or any non-score-affecting field.
    This must exactly match the normalization engine's actual inputs so
    that a change to any result-affecting input changes the fingerprint.
    """
    # 1. Rubric versions + criteria for this event (sorted deterministically)
    rubric_rows = db.execute(
        "SELECT rv.id, rc.key, rc.weight, rc.min_value, rc.max_value "
        "FROM rubric_versions rv "
        "JOIN rubric_criteria rc ON rc.rubric_version_id=rv.id "
        "WHERE rv.event_id=? "
        "ORDER BY rv.id, rc.key",
        (event_id,),
    ).fetchall()

    # 2. Assignments for this event (judge-project pairs)
    assignment_rows = db.execute(
        "SELECT judge_id, project_id FROM assignments "
        "WHERE event_id=? ORDER BY judge_id, project_id",
        (event_id,),
    ).fetchall()

    # 3. Score records for projects in this event
    score_rows = db.execute(
        "SELECT s.id, s.judge_id, s.project_id, s.rubric_version_id, s.raw_weighted "
        "FROM scores s JOIN projects p ON p.id=s.project_id "
        "WHERE p.event_id=? ORDER BY s.id",
        (event_id,),
    ).fetchall()

    # 4. Score criteria values (criterion-level) for those scores
    score_ids = [r["id"] for r in score_rows]
    criteria_rows = []
    for sid in sorted(score_ids):
        rows = db.execute(
            "SELECT score_id, criterion_key, value "
            "FROM score_criteria WHERE score_id=? "
            "ORDER BY criterion_key",
            (sid,),
        ).fetchall()
        criteria_rows.extend(rows)

    # Build a canonical JSON-serializable structure (sorted, no timestamps)
    state = {
        "rubric_criteria": [
            {"rv_id": r["id"], "key": r["key"], "weight": r["weight"],
             "min": r["min_value"], "max": r["max_value"]}
            for r in rubric_rows
        ],
        "assignments": [
            {"judge_id": r["judge_id"], "project_id": r["project_id"]}
            for r in assignment_rows
        ],
        "scores": [
            {"id": r["id"], "judge_id": r["judge_id"],
             "project_id": r["project_id"],
             "rubric_version_id": r["rubric_version_id"],
             "raw_weighted": r["raw_weighted"]}
            for r in score_rows
        ],
        "score_criteria": [
            {"score_id": r["score_id"], "key": r["criterion_key"],
             "value": r["value"]}
            for r in criteria_rows
        ],
    }
    # Use separators=(',', ':') for compact/deterministic JSON.
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@app.route("/organizer/normalize/<event_id>", methods=["POST"])
@require_role("organizer")
def run_normalization_route(event_id):
    """Run normalization for an event and persist everything needed to
    replay it: run-level stats, per-judge stats, per-review normalized
    values (with z-scores), and per-project results.

    Each review is weighted using the rubric version it was actually
    scored under (scores.rubric_version_id), never the event's newest
    rubric. Reviews from rubric versions whose weights/score ranges
    differ cannot be pooled into one global mean/variance - that would
    mix incomparable scales - so a run refuses to proceed and says so,
    rather than silently producing a number that means nothing. The
    organizer resolves this by re-scoring or by restricting to one
    rubric version.
    """
    db = open_db()
    identity = current_identity()

    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return error("event not found", 404)
    if not can_run_normalization(event):
        audit.record(db, "normalization_executed", "denied", actor=identity,
                     resource=event_id, detail={"event_status": event_status(event)})
        return error("normalization can only be run once submissions have closed "
                     f"(event status: {event_status(event)})", 409)

    score_rows = db.execute(
        "SELECT s.id AS score_id, s.judge_id, s.project_id, s.rubric_version_id, s.comment "
        "FROM scores s JOIN projects p ON p.id = s.project_id "
        "WHERE p.event_id=? ORDER BY s.id", (event_id,),
    ).fetchall()
    if not score_rows:
        return error("no scores recorded for this event yet", 400)

    # Compute the judging-state fingerprint BEFORE reading criteria, so
    # the fingerprint represents the state at the start of this run.
    current_fingerprint = _compute_judging_state_fingerprint(db, event_id)

    # Load each distinct rubric version's criteria once.
    rubric_defs = {}
    for rv_id in {r["rubric_version_id"] for r in score_rows}:
        crit = db.execute(
            "SELECT key, weight, min_value, max_value FROM rubric_criteria "
            "WHERE rubric_version_id=?", (rv_id,)
        ).fetchall()
        weighted_lo, weighted_hi = weighted_score_bounds(crit)
        rubric_defs[rv_id] = {
            "weights": {c["key"]: c["weight"] for c in crit},
            "lo": weighted_lo,
            "hi": weighted_hi,
        }

    scales = {(d["lo"], d["hi"]) for d in rubric_defs.values()}
    if len(scales) > 1:
        return error("cannot normalize: this event has reviews scored on rubric "
                     "versions with different score ranges (" +
                     ", ".join(f"{lo:g}-{hi:g}" for lo, hi in sorted(scales)) +
                     "); these scales cannot be pooled", 409)
    (lo, hi), = scales

    reviews = []
    for r in score_rows:
        crit = db.execute(
            "SELECT criterion_key, value FROM score_criteria WHERE score_id=?", (r["score_id"],)
        ).fetchall()
        values = {c["criterion_key"]: c["value"] for c in crit}
        required = set(rubric_defs[r["rubric_version_id"]]["weights"])
        missing = required - set(values)
        if missing:
            # Never silently compute a reduced score for an incomplete review.
            return error(f"cannot normalize: review {r['score_id']} is missing "
                         f"criteria {sorted(missing)}", 409)
        reviews.append({
            "score_id": r["score_id"], "judge_id": r["judge_id"],
            "project_id": r["project_id"], "criteria": values,
            "rubric_version_id": r["rubric_version_id"],
            "comment": r["comment"]
        })

    # weighted_raw_score() takes one weights dict; reviews under
    # different rubric versions may have different weights, so compute
    # each review's raw score under its own rubric here, then hand the
    # engine pre-weighted single-criterion reviews.
    from normalization import weighted_raw_score
    prepared = []
    for r in reviews:
        raw = weighted_raw_score(r["criteria"], rubric_defs[r["rubric_version_id"]]["weights"])
        prepared.append({**r, "criteria": {"raw": raw}})
    global_stats, judge_stats, enriched = run_normalization(
        prepared, {"raw": 1.0}, lo=lo, hi=hi)

    existing = db.execute(
        "SELECT MAX(version) AS v FROM normalization_runs WHERE event_id=?", (event_id,)
    ).fetchone()
    version = (existing["v"] or 0) + 1
    run_id = f"normrun_{event_id}_{version}"

    try:
        db.execute(
            "INSERT INTO normalization_runs (id, event_id, version, algorithm_version, k_param, "
            "global_mean, global_var, score_lo, score_hi, "
            "judging_state_fingerprint, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, event_id, version, "location-scale-shrinkage-v1", global_stats.k, global_stats.mean,
             global_stats.variance, lo, hi, current_fingerprint, now_iso(), identity["id"]),
        )
        for jid, js in judge_stats.items():
            db.execute(
                "INSERT INTO judge_normalization_stats (normalization_run_id, judge_id, "
                "n, mean, variance, shrunk_mean, shrunk_var, shrunk_sd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, jid, js.n, js.mean, js.variance, js.shrunk_mean,
                 js.shrunk_variance, js.shrunk_sd),
            )
        for r in enriched:
            db.execute(
                "INSERT INTO review_normalizations (normalization_run_id, score_id, "
                "raw_weighted, z_score, normalized_value, comment) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, r["score_id"], r["raw"], r["z"], r["normalized"], r.get("comment")),
            )

        # Snapshot the exact criterion values used in this run so that
        # explain_result() can display historical (immutable) data even
        # after a judge edits their score after the run.
        for r in reviews:
            for ckey, val in r["criteria"].items():
                db.execute(
                    "INSERT INTO normalization_run_criteria "
                    "(normalization_run_id, score_id, criterion_key, value) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, r["score_id"], ckey, val),
                )

        from collections import defaultdict
        per_project = defaultdict(list)
        for r in enriched:
            per_project[r["project_id"]].append(r)
        for pid, rows in per_project.items():
            raw_avg = sum(r["raw"] for r in rows) / len(rows)
            norm_avg = sum(r["normalized"] for r in rows) / len(rows)
            rv_ids = {r["rubric_version_id"] for r in rows}
            db.execute(
                "INSERT INTO project_results (id, event_id, project_id, "
                "normalization_run_id, rubric_version_id, review_count, raw_score, "
                "normalized_score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"result_{pid}_{run_id}", event_id, pid, run_id,
                 next(iter(rv_ids)) if len(rv_ids) == 1 else None,
                 len(rows), raw_avg, norm_avg, now_iso()),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    audit.record(db, "normalization_executed", "ok", actor=identity,
                 resource=event_id, detail={"version": version, "k": global_stats.k,
                                            "reviews": len(enriched)})
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"run_id": run_id, "version": version,
                        "reviews": len(enriched)}), 201
    return redirect(url_for("results_view", event_id=event_id))


@app.route("/organizer/results/<event_id>")
@require_role("organizer")
def results_view(event_id):
    db = open_db()
    latest_run = db.execute(
        "SELECT * FROM normalization_runs WHERE event_id=? ORDER BY version DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    results = []
    if latest_run:
        results = db.execute(
            "SELECT r.*, p.title FROM project_results r JOIN projects p ON p.id=r.project_id "
            "WHERE r.normalization_run_id=? ORDER BY r.normalized_score DESC",
            (latest_run["id"],),
        ).fetchall()
    return render_template("results.html", event_id=event_id, results=results,
                            latest_run=latest_run, identity=current_identity())


@app.route("/organizer/results/<event_id>/explain/<project_id>")
@require_role("organizer")
def explain_result(event_id, project_id):
    db = open_db()
    latest_run = db.execute(
        "SELECT * FROM normalization_runs WHERE event_id=? ORDER BY version DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    if not latest_run:
        return error("run normalization first", 400)

    result = db.execute(
        "SELECT r.*, p.title FROM project_results r JOIN projects p ON p.id=r.project_id "
        "WHERE r.normalization_run_id=? AND r.project_id=?",
        (latest_run["id"], project_id),
    ).fetchone()

    # Read the values recorded BY the normalization run being explained
    # (review_normalizations), not the live scores table - a score edited
    # after the run must not change what this page says the run did.
    reviews = db.execute(
        "SELECT s.*, u.name AS judge_name, rn.raw_weighted AS run_raw, "
        "       rn.z_score AS run_z, rn.normalized_value AS run_normalized, "
        "       rn.comment AS run_comment "
        "FROM review_normalizations rn "
        "JOIN scores s ON s.id = rn.score_id "
        "JOIN users u ON u.id = s.judge_id "
        "WHERE rn.normalization_run_id=? AND s.project_id=?",
        (latest_run["id"], project_id),
    ).fetchall()

    judge_breakdown = []
    for r in reviews:
        jstats = db.execute(
            "SELECT * FROM judge_normalization_stats WHERE normalization_run_id=? AND judge_id=?",
            (latest_run["id"], r["judge_id"]),
        ).fetchone()
        # Read criterion values from the run's immutable snapshot
        # (normalization_run_criteria), never from the live score_criteria
        # table. A score edited after the run must not change what this
        # page says the run used as input.
        crit = db.execute(
            "SELECT criterion_key, value FROM normalization_run_criteria "
            "WHERE normalization_run_id=? AND score_id=?",
            (latest_run["id"], r["id"]),
        ).fetchall()
        judge_breakdown.append({
            "judge_name": r["judge_name"], "judge_id": r["judge_id"],
            "raw": r["run_raw"], "z": r["run_z"], "normalized": r["run_normalized"],
            "comment": r["run_comment"] or "No comment provided",
            "criteria": {c["criterion_key"]: c["value"] for c in crit},
            "n": jstats["n"] if jstats else None,
            "judge_mean": jstats["mean"] if jstats else None,
            "judge_variance": jstats["variance"] if jstats else None,
            "shrunk_mean": jstats["shrunk_mean"] if jstats else None,
            "shrunk_sd": jstats["shrunk_sd"] if jstats else None,
        })

    return render_template("explain_result.html", result=result, event_id=event_id,
                            project_id=project_id, run=latest_run,
                            judge_breakdown=judge_breakdown, identity=current_identity())


@app.route("/organizer/publish/<event_id>", methods=["POST"])
@require_role("organizer")
def publish_results(event_id):
    """Publish an event's results. Preconditions, all server-side:
      - the event exists and is past OPEN (a publish before submissions
        close would freeze a result over a still-changing field);
      - at least one normalization run exists (otherwise there is no
        result to publish, and the published flag would be a claim about
        nothing);
      - it is not already published (a second publish would add a
        misleading audit entry for a no-op).
    Publishing does not touch scores or results; it only changes the
    event's state, after which scoring is refused (see scoring_is_open).
    """
    db = open_db()
    identity = current_identity()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return error("event not found", 404)

    status = event_status(event)
    if status in ("published", "archived"):
        return error(f"results are already {status}", 409)

    def refuse(reason, message):
        audit.record(db, "result_published", "denied", actor=identity,
                     resource=event_id, detail={"reason": reason, "event_status": status})
        return error(message, 409)

    if not can_run_normalization(event):
        return refuse("event not closed", "cannot publish while the event is still "
                      f"{status}; submissions must close first")
    run = db.execute(
        "SELECT id, version, judging_state_fingerprint FROM normalization_runs "
        "WHERE event_id=? ORDER BY version DESC LIMIT 1", (event_id,)
    ).fetchone()
    if run is None:
        return refuse("no normalization run", "run normalization before publishing")

    # Freshness gate: recompute the current fingerprint and compare with
    # what was recorded when the run was created. If they differ, the
    # judging data changed after that run and publishing it would produce
    # a result based on stale inputs.
    current_fp = _compute_judging_state_fingerprint(db, event_id)
    run_fp = run["judging_state_fingerprint"]
    if run_fp is not None and current_fp != run_fp:
        audit.record(db, "result_published", "denied", actor=identity,
                     resource=event_id,
                     detail={"reason": "stale_normalization",
                             "run_id": run["id"],
                             "run_fingerprint": run_fp,
                             "current_fingerprint": current_fp})
        return error(
            "Normalization run is stale; judging data changed after this run. "
            "Re-run normalization before publishing.", 409
        )

    db.execute("UPDATE events SET publish_state='published' WHERE id=?", (event_id,))
    db.commit()
    audit.record(db, "result_published", "ok", actor=identity, resource=event_id,
                 detail={"normalization_run": run["id"], "version": run["version"]})
    if _wants_json(request):
        return jsonify({"event_id": event_id, "status": "published",
                        "normalization_run": run["id"]}), 200
    return redirect(url_for("results_view", event_id=event_id))


@app.route("/organizer/audit")
@require_role("organizer")
def audit_log_view():
    db = open_db()
    rows = db.execute("SELECT * FROM audit_events ORDER BY seq DESC LIMIT 200").fetchall()
    chain_ok = audit.verify_chain(db)
    return render_template("audit.html", rows=rows, chain_ok=chain_ok, identity=current_identity())


# ---------------------------------------------------------------------
# CSV export  (T2)
# ---------------------------------------------------------------------

@app.route("/api/export.csv")
@require_role("organizer")
def export_csv():
    db = open_db()
    event_id = request.args.get("event", "").strip()
    if not event_id:
        return error("event query parameter is required", 400)
    rows = db.execute(
        "SELECT s.judge_id, s.project_id, p.title, s.raw_weighted, s.comment "
        "FROM scores s JOIN assignments a ON a.judge_id=s.judge_id AND a.project_id=s.project_id "
        "JOIN projects p ON p.id = s.project_id WHERE a.event_id=? ORDER BY s.project_id",
        (event_id,),
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["judge_id", "project_id", "project_title", "raw_weighted_score", "comment"])
    for r in rows:
        writer.writerow([r["judge_id"], r["project_id"], r["title"],
                          r["raw_weighted"], r["comment"] or ""])

    audit.record(db, "csv_exported", "ok", actor=current_identity(), resource=event_id)
    resp = app.response_class(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=export-{event_id}.csv"
    return resp


# ---------------------------------------------------------------------
# Health check (used by docker-compose healthcheck)
# ---------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    """Report whether the application can actually do its job, not
    merely whether the process is running. The audited fix: this used
    to return {"status": "ok"} unconditionally with no check at all, so
    a container whose SQLite file was missing, corrupted, or on an
    unwritable volume would still report healthy to Docker's
    healthcheck. This checks the one dependency the app has - its own
    database file - by opening a real connection and querying the
    schema; it deliberately checks nothing external, per the "no
    external services" requirement.
    """
    try:
        db = open_db()
        row = db.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        schema_ok = row is not None
        seeded = is_seeded(db)
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503

    if not schema_ok:
        return jsonify({"status": "error", "detail": "schema not initialized"}), 503

    return jsonify({"status": "ok", "database": "reachable", "seeded": seeded})


if __name__ == "__main__":
    ensure_seeded()
    app.run(host="0.0.0.0", port=8080)
