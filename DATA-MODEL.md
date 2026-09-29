# Data model

SQLite, defined in full in `src/backend/schema.sql`. Foreign keys are
enforced (`PRAGMA foreign_keys = ON` on every connection). All timestamps
are stored as ISO 8601 strings in UTC.

## Entity overview

```
users ──< sessions
users ──< team_members >── teams ──< projects
events ──< tracks
events ──< teams
events ──< projects
events ──< rubric_versions ──< rubric_criteria
users(judge) ──< judge_track_eligibility >── tracks
events ──< assignments >── users(judge) / projects
assignments ──< scores ──< score_criteria
events ──< normalization_runs ──< judge_normalization_stats
normalization_runs ──< normalization_run_criteria
normalization_runs ──< project_results >── projects
events ──< audit_events (not FK-scoped; global log)
events ──< votes / comments (T3, present but unused by T1/T2)
```

## Tables

### `users`
`id, email, name, role, password_hash, created_at`. `role` is constrained
to `visitor` is not stored (visitors have no row — anonymous access is the
default for public routes); stored roles are `participant`, `judge`,
`organizer`, `admin`.

### `sessions`
`token_hash (PK), user_id, created_at, expires_at, revoked_at`. The raw
token is never stored — `token_hash` holds `SHA-256(raw_token)` (see
SECURITY.md "Session Token Hashing"). A request carries the raw token; the
backend hashes it on every lookup (`auth.resolve_identity`). A session
authenticates only if a matching hash exists, `revoked_at` is NULL, and
`expires_at` is NULL or in the future. Sessions from a normal login get a
24-hour `expires_at`; logout sets `revoked_at` (the row is kept, so a
session's past existence stays inspectable). The fixed demo tokens have
`expires_at` NULL on purpose so `.dogfood.toml` keeps working across
restarts.

### `meta`
`key, value`. Holds a single `seed_complete` marker, written as the last
statement of the seed transaction. `is_seeded()` reads this, not "does any
event exist", so a crashed partial seed can never be mistaken for a
finished one.

### `events`
`id, name, description, kind ('fixture'|'live'), start_at,
submissions_close, judging_close, publish_state, created_at`. `kind`
distinguishes the immutable historical fixture event from the live demo
event, purely for documentation/display purposes — authorization and
deadline logic treat both identically. Status (`upcoming` / `open` /
`submissions_closed` / `judging` / `published`) is never stored; it's
derived on every read from `start_at` / `submissions_close` /
`judging_close` / `publish_state` compared against the server clock
(`events.py::event_status`).

### `tracks`, `prizes`
Simple children of `events`.

### `teams`
`id, event_id, name, owner_id, invite_code (unique), created_at`.
`invite_code` is a random token; joining a team requires presenting the
real code (`team/join` route) — there is no separate "add member" endpoint
that skips that check.

### `team_members`
Composite PK `(team_id, user_id)` — a simple membership join table.

### `projects`
`id, event_id, team_id, track_id, title, summary, repo_url, status
('draft'|'submitted'), submitted_at, created_at, updated_at`. A project
belongs to exactly one team and (optionally) one track. `status` plus
`submitted_at` distinguish a draft from a real submission; the public
gallery only ever shows `status='submitted'` rows.

### `judge_track_eligibility`
Composite PK `(user_id, track_id)`. A judge is only assignable to projects
in tracks they're eligible for — enforced by the assignment engine at
generation time, not by a runtime check (an assignment record existing at
all implies eligibility was already checked when it was created).

### `assignments`
`id, event_id, judge_id, project_id, version, created_at`, unique on
`(judge_id, project_id)`. This is the single source of truth for "is this
judge allowed to review this project" — `judge_review()` in `app.py`
checks for an assignment row before accepting a score, regardless of what
the URL claims.

### `rubric_versions`, `rubric_criteria`
A rubric is versioned per event. Each criterion row carries its own
`weight` (validated to sum to 1.0 across an event's active version — see
JUDGING.md) and a `min_value`/`max_value` range. A `scores` row records
which `rubric_version_id` was in effect, so a result stays interpretable
even if an organizer later changes the weights.

### `scores`
`id, assignment_id, judge_id, project_id, rubric_version_id NOT NULL,
comment, raw_weighted, created_at, updated_at`, unique on `(judge_id,
project_id)` — one score per judge per project, updatable but not
duplicatable. `raw_weighted` is the precomputed `S = Σ(w_i x_i)` value.

**`rubric_version_id` is set when a score is created and never changes.**
Editing an existing score re-validates and recomputes against that same
version, even if the organizer has since published a newer one. New scores
use the latest version. This keeps `raw_weighted` consistent with the
weights it was computed under (an earlier version recomputed with the newest
weights while leaving the old version id in place).

### `score_criteria`
Composite PK `(score_id, criterion_key)`. The individual per-criterion
values behind a score's weighted total — never discarded, so the full
input to any past calculation stays inspectable.

### `normalization_runs`
`id, event_id, version, k_param, global_mean, global_var, score_lo,
score_hi, judging_state_fingerprint, created_at, created_by`.
`score_lo/score_hi` record the rubric score range the run used for clipping.
`judging_state_fingerprint` is a SHA-256 hash of the canonical judging state
(all scores, criteria, and rubric weights) at the time the run was created.
`publish_results()` recomputes this fingerprint and rejects publication with
HTTP 409 if the current state no longer matches — this prevents a stale
normalization run from being published after scores or criteria have changed.
Each run of the normalization engine is a new row, not an overwrite — a
previous run's global statistics remain on record.

### `judge_normalization_stats`
Per-run, per-judge: `n, mean, variance, shrunk_mean, shrunk_var, shrunk_sd`.
This is what powers the "why did this change?" view's judge-by-judge
breakdown — every number shown there is read directly from this table, not
recomputed on the fly.

### `review_normalizations`
Per-run, per-review: `raw_weighted, z_score, normalized_value`. `z_score` is
NULL when the judge carried no discrimination signal (n=1 or zero variance)
and the value fell back to the global mean. The "why did this change?" page
reads this table, not the live `scores` table, so editing a score after a
run cannot change what that run is reported to have done.

### `project_results`
`id, event_id, project_id, normalization_run_id, rubric_version_id,
review_count, raw_score, normalized_score, created_at`, unique on
`(project_id, normalization_run_id)`. Both raw and normalized values are
retained side by side, along with which rubric and which normalization run
produced them — the reproducibility guarantee described in
ARCHITECTURE.md.

### `audit_events`
`seq INTEGER PRIMARY KEY AUTOINCREMENT, id, at, actor_id, actor_role,
action, resource, result, detail, prev_hash NOT NULL, hash NOT NULL`. `seq`
is the real primary key, so "the previous row" is well defined and gaps or
reuse cannot occur. Each row's `hash` covers its own fields plus
`prev_hash`; the first row chains to the literal `"genesis"`. Each append
runs under `BEGIN IMMEDIATE` so the read of the last hash and the insert
are one atomic unit. Nothing in the codebase updates or deletes rows. (An
earlier version declared `seq` as a plain nullable column that was never
set, which made every verification after the second record fail.)

### `normalization_run_criteria`
Composite PK `(normalization_run_id, score_id, criterion_key)`. An immutable
snapshot of the exact `score_criteria` values consumed by a specific
normalization run. Written once at normalization time and never updated.
`explain_result()` reads from this table, not the live `score_criteria`
table, so criterion values edited after a run cannot silently alter the
historical explanation of how that run's numbers were produced.

### `votes`, `comments`
Present in the schema for a future T3 implementation. Not written to or
read by any T1/T2 code path — see README.md's known limitations.

## Import / export paths

**In:** `scripts/seed.py` reads `fixtures.json` once (idempotently — see
`db.is_seeded`) and loads it into the schema above, preserving every
record including the intentionally awkward ones (the Dry Harbour
duplicate, zero-variance and low-sample judges, empty comments).

**Out:** `GET /api/export.csv?event=<id>` (organizer-only) — one row per
score, with judge id, project id, project title, raw weighted score, and
comment. This is the CSV the acceptance checker's T2 test exercises.
