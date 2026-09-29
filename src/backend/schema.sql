-- VERDICT LEDGER database schema
-- SQLite. Timestamps are stored as ISO 8601 UTC strings.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Meta / seed marker
-- ---------------------------------------------------------------------

-- A single-row table that is only ever written *after* a seed
-- transaction fully commits. db.is_seeded() checks this table, not
-- "does any event exist" — that old check could return true for a
-- database that crashed halfway through seeding, since some events (or
-- users, or projects) had already been inserted before the failure.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('participant','judge','organizer','admin')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Login looks up by lower(email); a case-sensitive UNIQUE on email
-- would let "A@x.com" and "a@x.com" both be inserted as distinct users
-- while behaving as the same login identity to a case-insensitive
-- lookup. This expression index enforces uniqueness on the value login
-- actually compares against.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (lower(email));

-- token_hash stores SHA-256(raw_token) — the plaintext token is never
-- persisted. See SECURITY.md "Session Token Hashing" for rationale.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);

-- ---------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    description        TEXT,
    kind               TEXT NOT NULL CHECK (kind IN ('fixture','live')) DEFAULT 'live',
    start_at           TEXT,
    submissions_close  TEXT NOT NULL,
    judging_close      TEXT,
    publish_state      TEXT NOT NULL CHECK (publish_state IN ('draft','published','archived')) DEFAULT 'draft',
    created_by         TEXT REFERENCES users(id),
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id       TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    UNIQUE (event_id, name)
);

CREATE TABLE IF NOT EXISTS prizes (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT
);

-- ---------------------------------------------------------------------
-- Teams
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    owner_id    TEXT REFERENCES users(id),
    invite_code TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    UNIQUE (event_id, id)
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id)
);

-- ---------------------------------------------------------------------
-- Projects / submissions
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    team_id       TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    track_id      TEXT REFERENCES tracks(id),
    title         TEXT NOT NULL,
    summary       TEXT,
    repo_url      TEXT,
    status        TEXT NOT NULL CHECK (status IN ('draft','submitted')) DEFAULT 'draft',
    submitted_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- A submitted project always has a submitted_at; a draft never
    -- does. This makes "submitted but no timestamp" or "draft with a
    -- timestamp" impossible to insert, rather than merely conventional.
    CHECK (
        (status = 'submitted' AND submitted_at IS NOT NULL) OR
        (status = 'draft' AND submitted_at IS NULL)
    ),
    UNIQUE (id, event_id),
    UNIQUE (event_id, id),
    FOREIGN KEY (event_id, team_id) REFERENCES teams(event_id, id) ON DELETE CASCADE
);

-- Compatibility guard for databases created before the composite project/team
-- foreign key was introduced. The trigger is harmless on fresh databases and
-- preserves the same invariant for an existing SQLite file without a risky
-- table rebuild during startup.
CREATE TRIGGER IF NOT EXISTS trg_projects_same_event_team
BEFORE INSERT ON projects
WHEN (SELECT event_id FROM teams WHERE id=NEW.team_id) IS NOT NULL
 AND (SELECT event_id FROM teams WHERE id=NEW.team_id) != NEW.event_id
BEGIN
    SELECT RAISE(ABORT, 'project team belongs to another event');
END;

-- ---------------------------------------------------------------------
-- Judges
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS judge_track_eligibility (
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, track_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (judge_id, project_id),
    FOREIGN KEY (project_id, event_id) REFERENCES projects(id, event_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------
-- Rubrics (versioned)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rubric_versions (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT REFERENCES users(id),
    UNIQUE (event_id, version)
);

CREATE TABLE IF NOT EXISTS rubric_criteria (
    id                TEXT PRIMARY KEY,
    rubric_version_id TEXT NOT NULL REFERENCES rubric_versions(id) ON DELETE CASCADE,
    key               TEXT NOT NULL,
    label             TEXT NOT NULL,
    description       TEXT,
    weight            REAL NOT NULL CHECK (weight >= 0),
    min_value         REAL NOT NULL DEFAULT 1,
    max_value         REAL NOT NULL DEFAULT 5,
    UNIQUE (rubric_version_id, key)
);

-- ---------------------------------------------------------------------
-- Scores
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scores (
    id                TEXT PRIMARY KEY,
    assignment_id     TEXT REFERENCES assignments(id) ON DELETE SET NULL,
    judge_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- The rubric version this score was actually calculated against.
    -- Set once at creation and never repointed to a newer rubric
    -- version on update — see auth.py/app.py judge_review() and
    -- JUDGING.md "rubric versioning" for why: a review's meaning is
    -- tied to the weights/range it was scored under, so re-labeling an
    -- old score as belonging to a newer rubric would make its stored
    -- raw_weighted value inconsistent with what that rubric version's
    -- weights would actually produce.
    rubric_version_id TEXT NOT NULL REFERENCES rubric_versions(id),
    comment           TEXT,
    raw_weighted      REAL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (judge_id, project_id)
);

CREATE TABLE IF NOT EXISTS score_criteria (
    score_id      TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
    criterion_key TEXT NOT NULL,
    value         REAL NOT NULL,
    PRIMARY KEY (score_id, criterion_key)
);

-- ---------------------------------------------------------------------
-- Normalization + results
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS normalization_runs (
    id                       TEXT PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    version                  INTEGER NOT NULL,
    algorithm_version        TEXT NOT NULL DEFAULT 'location-scale-shrinkage-v1',
    k_param                  REAL NOT NULL DEFAULT 3,
    global_mean              REAL,
    global_var               REAL,
    score_lo                 REAL NOT NULL DEFAULT 1,
    score_hi                 REAL NOT NULL DEFAULT 5,
    -- SHA-256 of the canonical judging-state at the time this run was
    -- created. publish_results() recomputes this fingerprint and rejects
    -- publication if the current state no longer matches, preventing a
    -- stale normalization run from being published after scores change.
    -- See JUDGING.md "Normalization Freshness" for the exact inputs.
    judging_state_fingerprint TEXT,
    created_at               TEXT NOT NULL,
    created_by               TEXT REFERENCES users(id),
    UNIQUE (event_id, version)
);

CREATE TABLE IF NOT EXISTS judge_normalization_stats (
    normalization_run_id TEXT NOT NULL REFERENCES normalization_runs(id) ON DELETE CASCADE,
    judge_id              TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    n                     INTEGER NOT NULL,
    mean                  REAL NOT NULL,
    variance              REAL NOT NULL,
    shrunk_mean           REAL NOT NULL,
    shrunk_var            REAL NOT NULL,
    shrunk_sd             REAL NOT NULL,
    PRIMARY KEY (normalization_run_id, judge_id)
);

-- Per-review normalized output, persisted rather than recomputed, so a
-- past normalization run's exact per-review contribution (z-score
-- included) stays inspectable even if the pure function's code changes
-- later.
CREATE TABLE IF NOT EXISTS review_normalizations (
    normalization_run_id TEXT NOT NULL REFERENCES normalization_runs(id) ON DELETE CASCADE,
    score_id              TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
    raw_weighted          REAL NOT NULL,
    z_score               REAL,
    normalized_value      REAL NOT NULL,
    comment               TEXT,
    PRIMARY KEY (normalization_run_id, score_id)
);

-- Immutable snapshot of the exact score_criteria values consumed by a
-- specific normalization run. explain_result() reads from here, never
-- from the live score_criteria table, so criterion values edited after
-- the run do not silently change the historical explanation.
-- LIVE TABLES (score_criteria) = current judging state
-- THIS TABLE = immutable evidence for a specific normalization run
CREATE TABLE IF NOT EXISTS normalization_run_criteria (
    normalization_run_id TEXT NOT NULL REFERENCES normalization_runs(id) ON DELETE CASCADE,
    score_id             TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
    criterion_key        TEXT NOT NULL,
    value                REAL NOT NULL,
    PRIMARY KEY (normalization_run_id, score_id, criterion_key)
);

CREATE TABLE IF NOT EXISTS project_results (
    id                    TEXT PRIMARY KEY,
    event_id              TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    normalization_run_id  TEXT REFERENCES normalization_runs(id),
    rubric_version_id     TEXT REFERENCES rubric_versions(id),
    review_count          INTEGER NOT NULL,
    raw_score             REAL,
    normalized_score      REAL,
    created_at            TEXT NOT NULL,
    UNIQUE (project_id, normalization_run_id),
    FOREIGN KEY (event_id, project_id) REFERENCES projects(event_id, id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS trg_project_results_same_event
BEFORE INSERT ON project_results
WHEN (SELECT event_id FROM projects WHERE id=NEW.project_id) IS NOT NULL
 AND (SELECT event_id FROM projects WHERE id=NEW.project_id) != NEW.event_id
BEGIN
    SELECT RAISE(ABORT, 'project result belongs to another event');
END;

-- ---------------------------------------------------------------------
-- Audit log (append-only)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_events (
    -- seq is the table's real primary key and SQLite's native rowid
    -- (INTEGER PRIMARY KEY aliases rowid), auto-incrementing and never
    -- reused. This is what makes "the previous row" well-defined:
    -- ORDER BY seq orders by the one column guaranteed to be
    -- monotonic and gap-stable, not by insertion order inferred from a
    -- timestamp two records could tie on.
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    at         TEXT NOT NULL,
    actor_id   TEXT,
    actor_role TEXT,
    action     TEXT NOT NULL,
    resource   TEXT,
    result     TEXT NOT NULL,
    detail     TEXT,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- T3: voting / comments (schema present, not wired to any T1/T2 route —
-- see README.md known limitations)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS votes (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    voter_key  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (event_id, project_id, voter_key)
);

CREATE TABLE IF NOT EXISTS comments (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    TEXT REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
