# Verdict Ledger

Evidence-first, self-hostable hackathon judging infrastructure.

**CLAIMED:** T1 + T2
**OFFICIAL ACCEPTANCE:** 7/7 PASS
The full project test suite is run as part of final verification; the verified
count is intentionally not hardcoded here.

## Core flow

Create Event → Create Team → Submit Project → Assign Judge → Private Judge Scoring → Normalize → Inspect Evidence → Publish

## Strongest differentiators

1. **Backend-enforced judge isolation** (no frontend-only hiding)
2. **Weighted rubric**
3. **Deterministic judge assignment**
4. **Score normalization** (sample-size shrunk)
5. **Normalization freshness gate** (prevents stale publication)
6. **Historical criterion snapshots** (immutable evidence)
7. **Audit trail** (hash-chained)
8. **CSV export**
9. **Self-hosted SQLite** (single file, no external DB)
10. **Offline dependency strategy** (vendored wheels)

## Honest Tier Status

| Tier | Status |
|---|---|
| **T1** | CLAIMED |
| **T2** | CLAIMED |
| **T3** | NOT CLAIMED |
| **T4** | NOT CLAIMED |

This project strictly targets the T1 and T2 requirements. T3 (voting/comments) and T4 (REST API/certificates) are explicitly out of scope.

Additionally, this project uses a **two-event model** for demonstration:
- `evt_01` (DOGFOOD fixture): Kept closed to preserve the historical data and pass acceptance checks.
- `evt_live_2026`: A live demo event for exploring the platform.

## Quick start

```bash
docker compose up
```

The image builds, the database seeds itself on first boot, and the portal is
at `http://localhost:8080`. No cloud account, external API, or network access
is needed at runtime. Data lives in a named volume; restarting does not
re-seed or duplicate anything.

Without Docker:

```bash
pip install -r requirements.txt
export VERDICT_LEDGER_DB=./verdict_ledger.db   # default is /data/verdict_ledger.db
python3 src/backend/app.py
```

Check the running portal with the official checker (unmodified, supplied by
DOGFOOD):

```bash
python3 run.py .dogfood.toml
```

Run the test suite (standard-library `unittest`, no extra installs):

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## Status

Stated precisely, because "implemented" and "verified" are different claims.

| Area | Implemented | Tested | Verified by official checker |
|---|---|---|---|
| Public gallery, search, track filter | yes | yes | yes (2 checks) |
| Deadline enforced server-side | yes | yes | yes (1 check) |
| Judge cannot read peer scores | yes | yes | yes (1 check) |
| Participant blocked from judge data | yes | yes | yes (1 check) |
| Judge reads own scores | yes | yes | yes (1 check) |
| Organizer CSV export | yes | yes | yes (1 check) |
| Draft -> edit -> submit lifecycle | yes | yes (API and browser form flow) | not covered by checker |
| Event creation with tracks/prizes | yes | yes | not covered |
| Judge invitation, deterministic assignment | yes | yes | not covered |
| Rubric builder with versioning | yes | yes | not covered |
| Normalization, results, "why" page | yes | yes | not covered |
| Publish with preconditions | yes | yes | not covered |
| Sessions with expiry and revocation | yes | yes | not covered |
| Hash-chained audit log | yes | yes | not covered |
| Normalization freshness gate (409 on stale publish) | yes | yes | not covered |
| Historical criteria snapshot for explain | yes | yes | not covered |
| Session token hashing (SHA-256) | yes | yes | not covered |
| Repo URL scheme validation | yes | yes | not covered |
| Offline Docker build (vendored wheels) | yes | yes | not covered |
| T3 (voting, comments) | **no** | - | - |
| T4 (REST API, webhooks, certificates, embed) | **no** | - | - |

**Official checker result, current build: 7/7 PASS, "claimed T1 T2, verified
T1 T2"** (see `acceptance-report.txt`, generated from a clean `docker compose
up` against `localhost:8080` with the byte-identical supplied `run.py`). The
checker exercises 7 behaviours; everything in the "not covered" rows is
verified only by this project's own tests, not by DOGFOOD's checker.

**Tests:** all discovered tests must pass with none skipped. Several are
mutation-checked: the audited bug
was reintroduced and the tests confirmed to fail (see "How the tests were
validated" in ARCHITECTURE.md).

## The two-event model

Two events exist from first boot:

- **`evt_01`** - the supplied DOGFOOD historical fixture, loaded verbatim
  (41 projects, 40 teams, 30 judges, 8 tracks, 126 scores). It is closed and
  published, and its dates are never altered. The checker's "closed event
  refuses submissions" test depends on this.
- **`evt_live_2026`** - an open demo event with its own teams, judges,
  rubric, and assignments, so the whole create -> submit -> assign -> judge
  -> normalize -> publish flow can be run live without touching the
  historical evidence.

## Seeded accounts

Local demo credentials for a local instance, not secrets.

| Role | Login | Notes |
|---|---|---|
| Organizer | `organizer@verdictledger.local` / `organizer-demo` | fixed token `org_demo_token` |
| Admin | `admin@verdictledger.local` / `admin-demo` | organizer powers only, not a participant or judge |
| Judge A (`jdg_01`) | fixture account | fixed token `jdg_a_demo_token` |
| Judge B (`jdg_02`) | fixture account | fixed token `jdg_b_demo_token` |
| Participant | fixture account | fixed token `prt_demo_token` (on the closed event) |
| Live judges | `judge1/2/3@verdictledger.local` / `judge-demo` | live event |
| Live participants | `participant1/2@verdictledger.local` / `participant-demo` | live event |

The fixed tokens never expire so `.dogfood.toml` stays valid across
restarts. Sessions created by a normal login expire after 24 hours and are
revoked server-side on logout. Fixture judges and participants get random
passwords; they are meant to be reached through their tokens.

## Permission model

Decided explicitly, not inherited:

| Role | Can do |
|---|---|
| visitor | browse the gallery and submitted projects |
| participant | create/join a team, create/edit/submit projects for their own team |
| judge | read own scores, review projects assigned to them |
| organizer | everything under `/organizer`, CSV export |
| admin | organizer powers. **Not** a participant or judge. |

`tests/test_access_matrix.py` enumerates every route against every role and
fails if a route exists without a declared policy.

## Hardening and integrity

The repository includes regression coverage for lifecycle, event scoping,
credential handling, normalization provenance, validation, and access control:

1. **Normalization freshness gate.** Publishing a normalization run that is
   stale (scores were edited after the run was created) returns HTTP 409. A
   SHA-256 fingerprint of the judging state is stored at normalization time
   and recomputed at publish time.
2. **Historical criteria snapshot.** `explain_result()` reads criterion
   values from a `normalization_run_criteria` snapshot table, not the live
   `score_criteria` table. Editing a score after normalization cannot change
   a historical explanation.
3. **Session token hashing.** Raw session tokens are never stored in the
   database. `sessions.token_hash` stores `SHA-256(raw_token)`; the
   plaintext is returned once at login and used in headers thereafter.
4. **Repo URL scheme validation.** `repo_url` fields are validated at both
   project create and edit to accept only `http://` or `https://` schemes.
   Non-HTTP schemes (e.g. `javascript:`, `file:`) are rejected with 400.
5. **Offline Docker build.** All required Python wheels (Flask + transitive deps)
   are vendored in `vendor/`. The Dockerfile installs with
   `pip install --no-index --find-links=/app/vendor`, requiring no PyPI
   access at build time.

## Known limitations

- **T3 and T4 are not implemented.** Public voting, comments, REST API,
  webhooks, certificates and embeds do not exist. The `votes` and `comments`
  tables are in the schema but nothing reads or writes them.
- **Result replay/verify has no dedicated UI or endpoint.** Reproducibility
  is real (a run stores everything needed, and re-running gives identical
  numbers, tested), but there is no button that recomputes a stored result
  and reports a match.
- **The fixture event is published, so scoring on it is refused.** That is
  intended (its judging is historical), but it means the normalization "why"
  page for `evt_01` reflects data that cannot be edited through the app.
- **The audit log is tamper-evident, not tamper-proof.** See SECURITY.md.
- **Plain HTTP only, no TLS.** Put it behind a TLS-terminating proxy for
  any real deployment.
- **In-memory rate limiting** is applied to login, team-join, and judge-invite requests to prevent brute-force guessing and abuse (process-local).
- **Judge "invitation" means direct provisioning.** There is no email
  service offline, so an organizer creates the judge account; a cryptographically
  random initial password is revealed once in the JSON response or HTML success
  page. There is no password-reset flow.
- **Server-rendered HTML with no client-side JavaScript.** Deliberate for
  offline simplicity; every action is a full page load.
- **Docs are advisory; the tests are the source of truth** for behaviour.

## Project structure

```
.dogfood.toml            checker config (real routes, real seeded tokens)
acceptance-report.txt    real checker output
run.py                   the supplied official checker, unmodified
fixtures.json            the supplied fixture data, unmodified
docker-compose.yml, Dockerfile, requirements.txt
src/backend/             app.py (routes), auth.py, core.py, db.py, schema.sql,
                         events.py (state machine), assignment.py,
                         normalization.py, audit.py
src/frontend/            templates/ and static/style.css (no CDN, no fonts)
scripts/seed.py          atomic, idempotent seed
vendor/                  7 vendored wheels (Flask + deps, offline Docker build)
tests/                   unittest suite (full discovery; count verified at run time)
```

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) - shape, state machine, design decisions
- [DATA-MODEL.md](DATA-MODEL.md) - schema and versioning
- [JUDGING.md](JUDGING.md) - assignment, rubric, normalization, worked examples
- [SECURITY.md](SECURITY.md) - threat by threat, with residual limits

## License

MIT - see [LICENSE](LICENSE).
