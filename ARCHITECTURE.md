# Architecture

## Shape of the system

A single deployable service: one Flask process serves both server-rendered
HTML (for humans) and a JSON API under `/api` (for the acceptance checker
and any future programmatic client). One SQLite file is the entire
persistence layer. No microservices, no queue, no cache layer, no separate
frontend build step. This is a deliberate choice for a self-hostable
72-hour build: fewer moving parts an organizer has to operate, and nothing
that requires network access at runtime.

### Data flow pipeline

The overall flow of data through the system follows a strict linear sequence:

```
SYSTEM
  ↓
HTTP / Flask
  ↓
AUTHORIZATION
  ↓
T1 SUBMISSION
  ↓
T2 JUDGING
  ↓
NORMALIZATION
  ↓
HISTORICAL EVIDENCE
  ↓
FRESHNESS CHECK
  ↓
PUBLISH
```

### Component layout

```
                     ┌─────────────────────────┐
   browser  ───────► │   Flask app (app.py)    │
   curl              │  ┌────────────────────┐ │
                      │  │ routes             │ │
                      │  │  - gallery (T1)    │ │
                      │  │  - submission (T1) │ │
                      │  │  - judge API (T2)  │ │
                      │  │  - organizer room  │ │
                      │  └─────────┬──────────┘ │
                      │            │             │
                      │  ┌─────────▼──────────┐ │
                      │  │ core.py            │ │
                      │  │  identity + role   │ │
                      │  │  guard decorators  │ │
                      │  └─────────┬──────────┘ │
                      │            │             │
                      │  ┌─────────▼──────────┐ │
                      │  │ auth.py / events.py│ │
                      │  │ assignment.py      │ │
                      │  │ normalization.py   │ │
                      │  │ audit.py           │ │
                      │  └─────────┬──────────┘ │
                      │            │             │
                      │  ┌─────────▼──────────┐ │
                      │  │ db.py → SQLite file │ │
                      │  └────────────────────┘ │
                      └─────────────────────────┘
```

## Offline design

Everything the frontend needs — CSS, fonts (system stacks, no webfonts) —
ships from `src/frontend/static/`. There is no `<script src="https://...">` 
anywhere in a template. The Docker image installs its one dependency
(Flask) from vendored wheels in `vendor/` using
`pip install --no-index --find-links=/app/vendor` — no PyPI access is
required at build time, and nothing is fetched at container start. The
database is a file in a Docker volume. Disconnecting the network after
`docker compose build` changes nothing about runtime behavior.

The `vendor/` directory contains all 7 wheels (Flask plus its transitive
dependencies: Werkzeug, Jinja2, MarkupSafe, Click, Blinker,
itsdangerous), pre-downloaded as platform-appropriate `.whl` files.

## Authentication

Sessions are opaque random tokens (`secrets.token_hex(20)`) minted at login.
The raw token is returned once to the caller (as a cookie or in the JSON
response) and **never stored in the database**. Instead, `SHA-256(raw_token)`
is persisted in the `sessions.token_hash` column. Every subsequent request
re-hashes the presented token and looks up the hash — this means a database
leak does not directly expose session credentials (see SECURITY.md "Session
Token Hashing").

A request can present a token as a cookie (`Cookie: session=...`) or as a
bearer token (`Authorization: Bearer ...`) — both forms resolve through the
exact same `auth.resolve_identity()` lookup. This dual support exists because
`.dogfood.toml`'s own documentation says a header can be "a cookie, a
bearer token, a basic auth string" — the checker does not care which, it
just attaches whatever `[auth]` says. Passwords are hashed with PBKDF2-
HMAC-SHA256, 120,000 iterations, a random salt per user.

## Authorization

`core.py` is the one place authorization decisions get made. Two
decorators — `require_auth` and `require_role(*roles)` — wrap every
sensitive route. Both call `current_identity()`, which resolves the
requester from their session token and nothing else. There is no code
path anywhere in the application that reads a role, user id, judge id,
or team id from request data (query string, form body, JSON body, or
header) and trusts it for an authorization decision. Where request data
*names* an id (e.g. `?judge=jdg_01`, or a `team_id` in a submission
payload), that value is either ignored in favor of the session-derived
identity, or explicitly compared against it and rejected on mismatch —
see `api_judge_own_scores()` in `app.py` for the canonical example, which
is also exactly the check `run.py`'s "judge cannot see peer scores" test
exercises.

## Assignment engine

`assignment.py::build_assignments` is a pure function: given projects (with
track and team), a track-eligible judge pool per track, and each project's
own team membership (to exclude self-review), it returns judge/project
pairs. It is deterministic — the same inputs always produce the same
output, verified by `tests/test_assignment.py::test_determinism` — because
judges and projects are processed in a fixed sort order and each project's
reviewers are chosen by workload-ascending order rather than randomly.
Overlap (multiple judges per project) is intentional, not a bug: the DOGFOOD
brief calls this out explicitly as useful for calibration, and disjoint
batches are never required.

## Scoring engine

A review's raw weighted score is `S = Σ(weight_i × criterion_score_i)`,
computed once at submission time and stored alongside the individual
criterion values (nothing is thrown away — see `score_criteria` in
DATA-MODEL.md).

## Normalization engine

The full derivation lives in [JUDGING.md](JUDGING.md); this section covers
only how it's wired into the app. `normalization.py` is a pure module with
no database dependency — `run_normalization(reviews, weights, k)` takes
plain review dicts and returns global stats, per-judge stats, and each
review's raw + normalized score. `app.py`'s
`run_normalization_route` is the only place that touches the database: it
loads scores for an event, calls the pure function, and persists a new
`normalization_runs` row plus per-judge stats and per-project results. This
separation is what makes the math independently testable
(`tests/test_normalization.py` never touches Flask or SQLite) and is also
what makes a result reproducible: re-running normalization against
unchanged scores calls the same pure function with the same inputs and
gets the same output, by construction.

## Result engine

A `project_results` row retains raw score, normalized score, review count,
which rubric version and which normalization run produced it. Nothing is
overwritten in place — a new normalization run creates new result rows
rather than mutating old ones, so a previously published result stays
inspectable even after the event's scores or rubric change later.

## Normalization freshness gate

Publishing a normalization run is gated by a **freshness check**: at
normalization time, a SHA-256 fingerprint of the entire judging state (all
scores, their per-criterion values, and the rubric weights) is computed
and stored as `judging_state_fingerprint` on the `normalization_runs` row.
When an organizer requests publication, `publish_results()` recomputes
this fingerprint from the current database state. If it differs from the
stored one — meaning scores or criteria were edited between normalization
and publish — the request is refused with HTTP 409 and a message
explaining that a re-normalization is needed. This prevents publishing
stale results that no longer reflect the current judging data.

## Historical criteria snapshot

When a normalization run is created, the exact `score_criteria` values
(per-score, per-criterion) consumed by that run are copied into the
`normalization_run_criteria` table. The `explain_result()` view reads from
this snapshot, never from the live `score_criteria` table. This means that
editing a score's criteria after normalization cannot retroactively change
what a historical normalization run is reported to have computed — the
explanation always reflects the data as it existed at run time.

## Input validation

`repo_url` fields on project create and edit are validated by
`_validate_repo_url()` in `app.py`, which rejects any URL whose scheme is
not `http` or `https` (case-insensitive). This prevents injection of
`javascript:`, `file:`, `data:`, or other dangerous URI schemes into the
gallery and project detail pages. Invalid URLs return HTTP 400 with a
descriptive message.

## Fixture vs. live event

Two independent events exist from first boot (`scripts/seed.py`):

- **`evt_01`** — the official DOGFOOD historical fixture, loaded verbatim.
  Its `submissions_close` is the fixture's own past date and is never
  altered, because the acceptance checker's "closed event refuses
  submissions" test specifically depends on that date already being in
  the past when the portal boots.
- **`evt_live_2026`** — a second, independent event with open/future
  dates, its own tracks, judges, teams, and a couple of starter draft
  projects, used to demonstrate the full create → submit → assign → judge
  → normalize → publish flow without ever touching the closed fixture
  event. This is why the fixture event's gallery entries never look like
  a "broken" submission flow — that flow lives entirely in the other
  event.

## Duplicate handling

`prj_07` and `prj_41` (both titled "Dry Harbour") are loaded as two
ordinary, independent project rows — nothing merges or deletes either one.
`project_detail()` in `app.py` runs a same-title/same-event query and
surfaces a "potential duplicate submission" callout on both projects'
pages; the organizer integrity view (`integrity_view()`) surfaces the same
pair event-wide. The duplicate is evidence, not an error to be silently
resolved.

## Gallery deterministic ordering

The gallery uses deterministic project ordering so pagination and fixture
visibility remain reproducible. The actual SQL implementation (in `app.py`
`gallery()`) explicitly orders by:
`ORDER BY (p.event_id = 'evt_01') DESC, p.id ASC`
This ensures the checked fixture event is always at the top, and projects
are always displayed in a stable order.

## Audit log

Append-only, hash-chained (`audit.py`). Every write action — login,
submission, score, normalization run, publish, denied access attempt —
gets a row whose hash covers its own fields plus the previous row's hash.
Recomputing the chain (`audit.verify_chain`) detects whether any row has
been altered after the fact. This is explicitly **tamper-evident, not
tamper-proof** — see SECURITY.md for what that distinction does and does
not protect against.

## Security boundaries

Summarized in README.md and detailed threat-by-threat in SECURITY.md. The
short version: every boundary that matters (role, resource ownership,
assignment eligibility, deadline) is re-checked server-side on every
request that touches it, never assumed from a prior page load or a
client-supplied flag.

## Design tradeoffs

- **Server-rendered HTML over a JS framework.** Fewer build steps, no
  bundler, works with JavaScript disabled, and keeps the "no CDN" offline
  requirement trivially true. The tradeoff is a full page reload per
  action rather than optimistic UI updates — acceptable for an
  organizer-facing operational tool judged on correctness, not for a
  consumer product judged on polish.
- **SQLite over a client-server database.** One file, zero configuration,
  trivially backed up, and satisfies "no hosted database" by construction.
  The tradeoff is limited write concurrency, which is a non-issue at
  hackathon scale (tens of judges, not thousands of concurrent writers).
- **Assignment records reconstructed from fixture scores rather than
  generated fresh.** The fixture data only records outcomes (who scored
  what), not the assignment process that produced them. Seeding therefore
  creates one `assignments` row per fixture score rather than running the
  assignment engine against the fixture — the engine itself is exercised
  fully (and demonstrably deterministic) against the live demo event
  instead, and by its own unit test suite.


## Event lifecycle (single state machine)

`events.py` maps timestamps to exactly one state, and every date-based
permission is a lookup against it:

```
UPCOMING -> OPEN -> SUBMISSIONS_CLOSED -> JUDGING -> PUBLISHED   (ARCHIVED: sticky)
```

| Action | Allowed while |
|---|---|
| create/edit/submit project, create/join team | OPEN |
| score a project | SUBMISSIONS_CLOSED or JUDGING |
| run normalization | SUBMISSIONS_CLOSED, JUDGING, or PUBLISHED |
| publish | past OPEN, and a normalization run exists |

PUBLISHED and ARCHIVED come from `publish_state`, not from dates. An event
with no `judging_close` stays SUBMISSIONS_CLOSED until published.

## Design decisions (choices, not DOGFOOD requirements)

- **SQLite, one process.** No external database; trivially backed up.
- **Two-event seed model.** Historical fixture kept closed; separate live
  event for demonstration.
- **Deterministic assignment with blind overlap.** Same inputs, same output;
  judges never see each other's scores.
- **Rubric versions are immutable; scores pin their version.**
- **Normalization k=3, fixed.** A design parameter, not estimated. Not "full
  empirical Bayes".
- **Zero-variance and n=1 judges fall back to the global mean.**
- **Duplicates are flagged, never merged or deleted.**
- **Judge "invitation" is direct provisioning**, because there is no offline
  email service.
- **Admin = organizer powers only.**
- **One team per participant per event**, so a submission can never land
  under an unintended team.
- **Seed is a single transaction** with a completion marker written last.
- **Cross-table same-event invariants** (a project's track belongs to the
  project's event; an assignment's event matches its project's) are enforced
  in application code, because SQLite cannot express them declaratively.
  Track/event agreement is enforced on project create/edit and judge invite
  and is tested; assignment/event agreement holds because assignments are
  only created from that event's own projects.

## How the tests were validated

A passing test proves little until it has been seen to fail. For the audited
defects and the security boundaries, the bug or a weakened guard was
temporarily reintroduced and the suite confirmed to fail, then the code was
restored. Examples: re-adding the bare `COUNT(*)` with no `GROUP BY` fails
11 of 19 integrity tests; removing a role guard, widening a role, or adding an
unguarded route each fail `test_access_matrix.py`; using the newest rubric's
weights for every score fails the rubric-mixing regression. This is a manual
practice, not an automated mutation-testing tool, and was not applied to every
test.
