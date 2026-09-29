#!/usr/bin/env python3
"""Seed the Verdict Ledger database.

Idempotent: running this against an already-seeded database is a no-op
(it checks `is_seeded` first). Two events are created:

  Event A (evt_01, "fixture" kind) — the supplied DOGFOOD historical
  fixture event, loaded verbatim from fixtures.json. Its
  submissions_close date is the fixture's own date, in the past, and is
  never altered. This is the event the acceptance checker's "closed
  event refuses submissions" check runs against, and the event whose
  gallery must show the checked fixture titles on page one.

  Event B ("evt_live_2026") — a second, independent live demo event with
  future dates, its own teams/projects/judges/rubric, used to
  demonstrate the full create -> team -> project -> submit -> assign ->
  judge -> normalize -> publish flow without touching the closed
  historical event.

Run with:  python3 scripts/seed.py
"""
import json
import os
import sys
import secrets
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "backend"))

from db import get_connection, init_schema, is_seeded, mark_seed_complete  # noqa: E402
from auth import hash_password, now_iso  # noqa: E402
from assignment import build_assignments  # noqa: E402
from normalization import weighted_raw_score  # noqa: E402

FIXTURES_PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures.json")

DEMO_RUBRIC_WEIGHTS = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}
DEMO_RUBRIC_LABELS = {
    "functionality": ("Functionality", "Does it work, and do what it claims?"),
    "quality": ("Quality", "Code quality, polish, and craftsmanship."),
    "innovation": ("Innovation", "Novelty and creativity of the approach."),
}


def email_to_user_id(prefix, email):
    slug = email.split("@")[0].replace(".", "_")
    return f"{prefix}_{slug}"


def seed_fixture_event(conn, fixture):
    ev = fixture["event"]
    conn.execute(
        "INSERT INTO events (id, name, description, kind, start_at, "
        "submissions_close, judging_close, publish_state, created_at) "
        "VALUES (?, ?, ?, 'fixture', ?, ?, ?, 'published', ?)",
        (ev["id"], ev["name"],
         "The official DOGFOOD 2026 historical fixture event, loaded "
         "verbatim from fixtures.json. Closed on purpose — see "
         "ARCHITECTURE.md.",
         None, ev["submissions_close"], ev["submissions_close"], now_iso()),
    )

    track_id_map = {}
    for t in fixture["tracks"]:
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (?, ?, ?)",
                     (t["id"], ev["id"], t["name"]))
        track_id_map[t["id"]] = t["id"]

    # Judges: real user accounts with role=judge, track eligibility from
    # the fixture, and a deterministic session token seeded for jdg_01
    # and jdg_02 (used as judge_a / judge_b in .dogfood.toml).
    judge_user_id = {}
    for j in fixture["judges"]:
        uid = j["id"]  # keep fixture ids stable so scores map directly
        conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, 'judge', ?, ?)",
            (uid, j["email"], j["name"], hash_password(secrets.token_hex(8)), now_iso()),
        )
        judge_user_id[j["id"]] = uid
        for track in j.get("tracks", []):
            conn.execute(
                "INSERT OR IGNORE INTO judge_track_eligibility (user_id, track_id) "
                "VALUES (?, ?)", (uid, track),
            )

    # Teams + a participant member per team (first member email = owner)
    team_owner_email = {}
    for t in fixture["teams"]:
        conn.execute(
            "INSERT INTO teams (id, event_id, name, owner_id, invite_code, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (t["id"], ev["id"], t["name"], secrets.token_hex(6), now_iso()),
        )
        members = t.get("members", [])
        owner_uid = None
        for idx, member_email in enumerate(members):
            uid = email_to_user_id("prt", member_email)
            existing = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO users (id, email, name, role, password_hash, created_at) "
                    "VALUES (?, ?, ?, 'participant', ?, ?)",
                    (uid, member_email, member_email.split("@")[0], hash_password(secrets.token_hex(8)), now_iso()),
                )
            conn.execute(
                "INSERT OR IGNORE INTO team_members (team_id, user_id, joined_at) "
                "VALUES (?, ?, ?)", (t["id"], uid, now_iso()),
            )
            if idx == 0:
                owner_uid = uid
        if owner_uid:
            conn.execute("UPDATE teams SET owner_id=? WHERE id=?", (owner_uid, t["id"]))
        team_owner_email[t["id"]] = owner_uid

    # Projects
    for p in fixture["projects"]:
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, "
            "summary, repo_url, status, submitted_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?)",
            (p["id"], ev["id"], p["team"], p.get("track"), p["title"],
             p.get("summary"), p.get("repo_url"), p.get("submitted_at"),
             p.get("submitted_at", now_iso()), now_iso()),
        )

    # Rubric v1 for the fixture event (a design choice, documented as such
    # — the fixture data itself does not define organizer weights).
    rubric_id = f"{ev['id']}_rubric_v1"
    conn.execute(
        "INSERT INTO rubric_versions (id, event_id, version, created_at) "
        "VALUES (?, ?, 1, ?)", (rubric_id, ev["id"], now_iso()),
    )
    for key, weight in DEMO_RUBRIC_WEIGHTS.items():
        label, desc = DEMO_RUBRIC_LABELS[key]
        conn.execute(
            "INSERT INTO rubric_criteria (id, rubric_version_id, key, label, "
            "description, weight, min_value, max_value) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 5)",
            (f"{rubric_id}_{key}", rubric_id, key, label, desc, weight),
        )

    # Scores + assignments (assignment records reconstructed from the
    # scores themselves, since the fixture only records outcomes)
    for s in fixture["scores"]:
        judge_id = judge_user_id[s["judge"]]
        project_id = s["project"]
        assignment_id = f"asg_{judge_id}_{project_id}"
        conn.execute(
            "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, "
            "project_id, version, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (assignment_id, ev["id"], judge_id, project_id, now_iso()),
        )
        raw = weighted_raw_score(s["criteria"], DEMO_RUBRIC_WEIGHTS)
        score_id = f"score_{judge_id}_{project_id}"
        comment = s.get("comment") or None
        conn.execute(
            "INSERT INTO scores (id, assignment_id, judge_id, project_id, "
            "rubric_version_id, comment, raw_weighted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (score_id, assignment_id, judge_id, project_id, rubric_id,
             comment, raw, now_iso(), now_iso()),
        )
        for crit_key, val in s["criteria"].items():
            conn.execute(
                "INSERT INTO score_criteria (score_id, criterion_key, value) "
                "VALUES (?, ?, ?)", (score_id, crit_key, val),
            )

    # No commit here: this function is always called as part of one
    # atomic seed transaction managed by main() - see the atomicity
    # fix's docstring on main() for why intermediate commits inside
    # individual seed_* helpers were the audited defect.
    return judge_user_id


def seed_live_event(conn):
    """A second, independent event with open/future dates so the
    submit -> assign -> judge -> normalize -> publish flow can be
    demonstrated live, without touching the closed historical event.
    """
    ev_id = "evt_live_2026"
    now = datetime.now(timezone.utc)
    start_at = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    close_at = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    judging_close = (now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "INSERT INTO events (id, name, description, kind, start_at, "
        "submissions_close, judging_close, publish_state, created_at) "
        "VALUES (?, 'Verdict Ledger Live Demo', ?, 'live', ?, ?, ?, 'draft', ?)",
        (ev_id, "A second, open demo event used to show the full "
         "submit -> judge -> publish lifecycle live.",
         start_at, close_at, judging_close, now_iso()),
    )

    track_names = ["Developer tools", "Fintech", "Health"]
    track_ids = []
    for i, name in enumerate(track_names, 1):
        tid = f"live_trk_{i:02d}"
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (?, ?, ?)",
                     (tid, ev_id, name))
        track_ids.append(tid)

    # Demo accounts: one organizer, a handful of judges, a couple of
    # participant teams with a starter project each so the gallery and
    # judge queue are not empty on first boot.
    organizer_id = "organizer"
    conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, created_at) "
        "VALUES (?, 'organizer@verdictledger.local', 'Demo Organizer', "
        "'organizer', ?, ?)",
        (organizer_id, hash_password("organizer-demo"), now_iso()),
    )
    admin_id = "admin"
    conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, created_at) "
        "VALUES (?, 'admin@verdictledger.local', 'Demo Admin', 'admin', ?, ?)",
        (admin_id, hash_password("admin-demo"), now_iso()),
    )

    demo_judges = [
        ("live_judge_1", "judge1@verdictledger.local", "Live Judge One", [track_ids[0]]),
        ("live_judge_2", "judge2@verdictledger.local", "Live Judge Two", [track_ids[1], track_ids[2]]),
        ("live_judge_3", "judge3@verdictledger.local", "Live Judge Three", [track_ids[0], track_ids[1]]),
    ]
    for uid, email, name, tracks in demo_judges:
        conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, 'judge', ?, ?)",
            (uid, email, name, hash_password("judge-demo"), now_iso()),
        )
        for t in tracks:
            conn.execute(
                "INSERT INTO judge_track_eligibility (user_id, track_id) VALUES (?, ?)",
                (uid, t),
            )

    demo_teams = [
        ("live_tm_1", "Harbor Light", ["participant1@verdictledger.local"]),
        ("live_tm_2", "Quiet Static", ["participant2@verdictledger.local"]),
    ]
    projects = []
    for i, (tid, name, members) in enumerate(demo_teams):
        conn.execute(
            "INSERT INTO teams (id, event_id, name, owner_id, invite_code, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (tid, ev_id, name, secrets.token_hex(6), now_iso()),
        )
        owner_uid = None
        for idx, email in enumerate(members):
            uid = email_to_user_id("live_prt", email)
            conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, created_at) "
                "VALUES (?, ?, ?, 'participant', ?, ?)",
                (uid, email, email.split("@")[0], hash_password("participant-demo"), now_iso()),
            )
            conn.execute(
                "INSERT INTO team_members (team_id, user_id, joined_at) VALUES (?, ?, ?)",
                (tid, uid, now_iso()),
            )
            if idx == 0:
                owner_uid = uid
        conn.execute("UPDATE teams SET owner_id=? WHERE id=?", (owner_uid, tid))

        pid = f"live_prj_{i+1}"
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, "
            "summary, repo_url, status, submitted_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', NULL, ?, ?)",
            (pid, ev_id, tid, track_ids[i % len(track_ids)], name,
             "A starter project seeded for the live demo.",
             "https://example.org/demo-repo", now_iso(), now_iso()),
        )
        projects.append({"id": pid, "track_id": track_ids[i % len(track_ids)], "team_id": tid})

    # Rubric v1 for the live event (same demo weights)
    rubric_id = f"{ev_id}_rubric_v1"
    conn.execute(
        "INSERT INTO rubric_versions (id, event_id, version, created_at) "
        "VALUES (?, ?, 1, ?)", (rubric_id, ev_id, now_iso()),
    )
    for key, weight in DEMO_RUBRIC_WEIGHTS.items():
        label, desc = DEMO_RUBRIC_LABELS[key]
        conn.execute(
            "INSERT INTO rubric_criteria (id, rubric_version_id, key, label, "
            "description, weight, min_value, max_value) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 5)",
            (f"{rubric_id}_{key}", rubric_id, key, label, desc, weight),
        )

    # Assignment: build with the deterministic engine
    judges_by_track = {}
    for uid, _, _, tracks in demo_judges:
        for t in tracks:
            judges_by_track.setdefault(t, []).append(uid)
    team_owner_by_project = {}
    for p in projects:
        members = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id=?", (p["team_id"],)
        ).fetchall()
        team_owner_by_project[p["id"]] = {m["user_id"] for m in members}

    pairs = build_assignments(projects, judges_by_track, team_owner_by_project,
                               target_reviews_per_project=2)
    for judge_id, project_id in pairs:
        aid = f"asg_{judge_id}_{project_id}"
        conn.execute(
            "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, "
            "project_id, version, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (aid, ev_id, judge_id, project_id, now_iso()),
        )

    # No commit here - part of the single atomic seed transaction.
    return {"organizer": organizer_id, "admin": admin_id,
            "judges": [j[0] for j in demo_judges]}


def print_test_logins(conn):
    """Print seed credentials the way the story in spec.md describes:
    printed at boot, ready to paste into .dogfood.toml.

    Session tokens are hashed in the DB — we print the known fixed raw
    tokens rather than querying the DB (which only stores hashes).
    """
    print("seeded. test logins:")
    print("  organizer    Cookie: session=org_demo_token")
    print("  judge_a      Cookie: session=jdg_a_demo_token")
    print("  judge_b      Cookie: session=jdg_b_demo_token")
    print("  participant  Cookie: session=prt_demo_token")


def create_bootstrap_sessions(conn, judge_user_id, live_ids):
    """Create long-lived demo sessions for the accounts the acceptance
    checker and the demo script need, so `.dogfood.toml` values stay
    stable across restarts (idempotent: only inserted once).

    The raw token's SHA-256 hash is stored in sessions.token_hash.
    The plaintext token is never persisted.
    """
    from auth import hash_token as _ht

    def ensure_session(user_id, fixed_token):
        # Check by hash since the raw token is not stored.
        h = _ht(fixed_token)
        existing = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash=?", (h,)
        ).fetchone()
        if not existing:
            # Insert directly (not via create_session) to avoid an
            # internal conn.commit() that would break the outer
            # BEGIN IMMEDIATE seed transaction.
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, NULL)", (h, user_id, now_iso()),
            )

    # Fixed, deterministic demo tokens (documented in README) so the repo's
    # committed .dogfood.toml stays correct across `docker compose up`.
    ensure_session(judge_user_id["jdg_01"], "jdg_a_demo_token")
    ensure_session(judge_user_id["jdg_02"], "jdg_b_demo_token")
    ensure_session("organizer", "org_demo_token")

    first_participant = conn.execute(
        "SELECT tm.user_id AS uid FROM team_members tm "
        "JOIN teams t ON t.id = tm.team_id WHERE t.event_id='evt_01' "
        "ORDER BY tm.user_id LIMIT 1"
    ).fetchone()
    if first_participant:
        ensure_session(first_participant["uid"], "prt_demo_token")

    # No commit here - part of the single atomic seed transaction.


def main():
    """Seed the database as one atomic transaction.

    This is the audited P0 fix: the individual seed_* helpers used to
    each commit their own work independently, so a failure partway
    through (e.g. seed_live_event raising after seed_fixture_event had
    already committed) would leave a database with the fixture event
    fully loaded but the live event and bootstrap sessions missing -
    and the *old* is_seeded() check ("does any event exist") would
    already see that partial state as "seeded" on the next boot,
    permanently skipping the rest of the seed forever.

    Now: every insert happens inside one BEGIN IMMEDIATE transaction,
    the seed_complete marker is written as the last statement of that
    same transaction, and the whole thing commits once. If anything
    raises before that single commit, the transaction is rolled back in
    full - so a failed seed leaves the database exactly as empty as it
    was before the attempt, and the next boot will correctly see it as
    unseeded and try again, rather than getting stuck half-loaded.
    """
    init_schema()
    conn = get_connection()
    if is_seeded(conn):
        print("seeded. (already seeded, skipping)")
        _print_fixed_logins()
        conn.close()
        return

    with open(FIXTURES_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    conn.execute("BEGIN IMMEDIATE")
    try:
        judge_user_id = seed_fixture_event(conn, fixture)
        live_ids = seed_live_event(conn)
        create_bootstrap_sessions(conn, judge_user_id, live_ids)
        mark_seed_complete(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _print_fixed_logins()
    conn.close()


def _print_fixed_logins():
    print("seeded. test logins:")
    print("  organizer    Cookie: session=org_demo_token")
    print("  judge_a      Cookie: session=jdg_a_demo_token")
    print("  judge_b      Cookie: session=jdg_b_demo_token")
    print("  participant  Cookie: session=prt_demo_token")


if __name__ == "__main__":
    main()
