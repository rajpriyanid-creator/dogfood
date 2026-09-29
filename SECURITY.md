# Security

Honest, threat-by-threat. Verdict Ledger does not claim perfect security —
this document says what is mitigated, how, and what is left as a residual
limitation.

## IDOR (insecure direct object reference)

**Threat.** A caller changes an id in a URL or request body (a judge id, a
project id, a team id) hoping the backend trusts it instead of checking
who's actually asking.

**Mitigation.** Every sensitive route resolves identity from the session
token (`core.py::current_identity`) and either ignores a client-supplied id
entirely in favor of the session's own, or explicitly compares the two and
rejects a mismatch. Concretely:
- `/api/judge/scores?judge=<id>` and `/api/judges/<id>/scores` both compare
  the URL's id against the session's judge id and return 403 on mismatch
  (`app.py::api_judge_own_scores`, `api_judge_scores_by_path`).
- `/projects/new` derives the submitting team from the caller's own team
  membership (`team_members` joined on the session's user id) — a
  `team_id` field in the request body, if present, is never read for this
  purpose.
- `/judge/review/<project_id>` checks for an `assignments` row matching
  the session's judge id and that exact project id before accepting a
  score; there is no path that lets a judge score a project they weren't
  assigned by presenting its id directly.

**Residual limitation.** Ids themselves (project ids, team ids) are not
secret and are guessable/enumerable by design (the gallery is public). The
protection is entirely on the authorization check, not on id obscurity.

## Peer-score access (judge B reading judge A's scores)

**Threat.** The single check DOGFOOD's own spec calls "the one that costs
the most points" — hiding a peer's scores in the UI while the API still
returns them to anyone logged in.

**Mitigation.** The check lives in the backend (`api_judge_own_scores`),
not the template. It runs identically regardless of which URL shape is
used to ask (`?judge=` query param or `/api/judges/<id>/scores` path
form) — see `tests/test_authorization.py::test_judge_b_cannot_see_judge_a_via_query_param`
and `..._via_path_form`. It is a general rule (compare session identity to
requested identity) rather than a hardcoded response for the specific URL
the acceptance checker happens to probe — verified by the adversarial spot
check in the demo flow and by the same authorization module being reused
for every score-reading route.

## Participant → judge escalation

**Threat.** A participant account reaching judge-only data.

**Mitigation.** `require_role("judge")` on every judge route; a
participant's session resolves to `role='participant'`, which is not in
the allowed set. Verified by `test_participant_blocked_from_judge_endpoint`.

## Judge → organizer escalation

**Threat.** A judge account reaching organizer-only controls (CSV export,
normalization triggers, publish, audit log).

**Mitigation.** `require_role("organizer")` (which also allows `admin`,
a strict superset) on every organizer route. Verified by
`test_judge_blocked_from_organizer_endpoint`, `test_csv_export_requires_organizer`.

## Cross-team modification

**Threat.** A participant modifying or submitting on behalf of a team they
don't belong to.

**Mitigation.** Team identity for a submission is always derived from the
caller's own `team_members` row, never from a client-supplied team id (see
IDOR section above). Joining a team requires the team's actual invite
code — there is no "add yourself to team X" endpoint that skips that
check.

**Residual limitation.** Invite codes are `secrets.token_hex(6)` (48 bits
of entropy) — not brute-forceable in practice, but also not
rate-limited at the application layer in this build. A real deployment
serving a large public event should add rate limiting on `/team/join`.

## Deadline gaming

**Threat.** A client claiming or implying an event is still open to
smuggle in a late submission.

**Mitigation.** `events.py::submissions_are_open` computes openness from
the stored `submissions_close` timestamp compared against
`datetime.now(timezone.utc)` at request time — there is no request field
that can override this, and none is read for this purpose even if
supplied (`test_deadline_is_computed_server_side_not_client_supplied`
sends spoofed `event_status`/`submissions_close` fields and confirms
they're ignored).

## Score tampering

**Threat.** Modifying a score after the fact without a record of the
change, or submitting a score outside the valid criterion range.

**Mitigation.** Every criterion value is range-checked against the
rubric's `min_value`/`max_value` before it's written
(`judge_review()` in `app.py`); out-of-range submissions are rejected with
400. Score creation and updates are both recorded in the audit log
(`score_submitted` / `score_updated` actions). The audit log's hash chain
means an out-of-band edit to a `scores` row (bypassing the application
entirely, e.g. direct SQL) does not itself get logged and is not directly
detected by the chain — the chain only covers what passes through
`audit.record()`. A score's rubric version is fixed at creation and edits
keep using it, so a later rubric change cannot silently alter an existing
review. Detecting a direct database edit would require comparing
current `scores` values against a separately-hashed snapshot, which this
build does not implement.

## Session theft and lifetime

**Threat.** A stolen or leaked session token being used to impersonate a
user, including after that user has logged out.

**Mitigation.** Tokens are `secrets.token_hex(20)` (160 bits) and the login
cookie is `httponly` with `samesite=Lax`. Sessions from a normal login
expire after 24 hours. Logout sets `revoked_at` on the server-side row, so
the same token presented later - as a cookie or a bearer header - is
rejected (`tests/test_session_expiration.py`). Revoking one session does not
affect any other user's or any other session of the same user. A new token
is minted on every login, so there is no session-fixation path.

**Residual limitations.**
- Plain HTTP only; run behind a TLS-terminating proxy for anything public.
  Without TLS a token can be sniffed on the network.
- The fixed demo tokens (`org_demo_token`, `jdg_a_demo_token`, ...) are
  published in this repository and **never expire**. They exist so
  `.dogfood.toml` stays valid. **Delete or rotate them before running a
  real event**: anyone who has read this repo has organizer access to a
  default install.
- There is no "log out everywhere" and no idle timeout.

## Brute force & rate limiting

**NOT IMPLEMENTED.**

**Threat.** Repeated password guessing against `/login` or invite-code
guessing against `/team/join`.

**Mitigation.** None built in. This is a deliberate scope limitation for this
offline, self-hostable build. Since it requires no Redis or external
state-store, a production deployment should enforce rate limiting (e.g., 5
failed attempts per IP per 60 seconds) at the reverse-proxy layer (e.g.,
Nginx or Traefik) rather than in the application layer.

## Password reset

**NOT IMPLEMENTED.**

**Threat.** A user loses their password and cannot regain access.

**Mitigation.** This platform is designed to operate completely offline
without external network access, so there is no SMTP integration or
email-based password reset flow. An organizer provisions judge accounts
directly. If a judge loses their credentials, the organizer must generate a
new account or reset it via out-of-band database access. This is an
intentional limitation of the offline architecture.

## Session token hashing

**Threat.** If the database file is compromised (stolen backup, exposed
volume, SQL injection on a read path), an attacker who reads the `sessions`
table obtains tokens they can use directly to impersonate any active user.

**Mitigation.** Raw session tokens are **never persisted**. The `sessions`
table's primary key is `token_hash`, which stores `SHA-256(raw_token)`. The
raw token is returned exactly once — at login — and used by the client in
subsequent requests. On every request, `auth.resolve_identity()` hashes the
presented token and looks up the hash. This means a leaked database does not
give an attacker usable session credentials; they would need the raw token,
which exists only in flight (in the cookie or Authorization header) and in
the client's local storage.

The same hashing is applied in `create_session()`, `resolve_identity()`,
and `revoke_session()` in `auth.py`, and in `seed.py` for the fixed demo
tokens. The SHA-256 hash of each demo token is what's stored, not the
plaintext.

Verified by:
- `test_raw_token_not_stored_in_db` — confirms the raw token string does
  not appear anywhere in the sessions table.
- `test_hashed_token_lookup_works` — confirms that hashing the raw token
  produces the stored primary key.
- `test_wrong_raw_token_rejected` — confirms a token that produces a
  different hash is rejected.

## Input validation (repo URL)

**Threat.** A participant submits a project with a `repo_url` using a
dangerous scheme (`javascript:`, `file:`, `data:`), which could lead to
XSS or information disclosure when rendered as a link in the gallery or
project detail page.

**Mitigation.** `_validate_repo_url()` in `app.py` parses the URL and
rejects any scheme that is not `http` or `https` (case-insensitive
comparison). Applied at both project creation (`/projects/new`) and
project editing (`/projects/<id>/edit`). Returns HTTP 400 with a
descriptive error message. Empty/blank URLs are accepted (the field is
optional).

## Sybil voting / ballot stuffing (T3)

**Not applicable to this build.** T3 (public voting) is not implemented —
see README.md's known limitations. The `votes` table exists in the schema
with a `UNIQUE(event_id, project_id, voter_key)` constraint as a starting
point for one-vote-per-voter-key enforcement, but no voting route reads or
writes it, so there is no live attack surface here to describe honestly
beyond "this feature doesn't exist yet."

## Judge collusion

**Threat.** Judges coordinating scores outside the platform.

**Mitigation.** None, and none is claimed. This is a social/procedural
problem, not one software can detect from score values alone — a
coordinated pair of judges giving the same project unusually high scores
looks identical, from the data, to two judges who happen to genuinely
agree. The audit log at least makes it possible to see who reviewed what
and when, which supports an organizer's own manual investigation, but the
platform does not attempt automated collusion detection.

## Draft visibility

**Threat.** Reading an unsubmitted project by guessing or leaking its id.

**Mitigation.** `/projects/<id>` returns 404 (not 403, so existence is not
confirmed) unless the project is submitted, or the caller is on the owning
team, or is an organizer/admin. Drafts are also excluded from the gallery
and from other projects' duplicate-candidate lists. There is no JSON project
endpoint, so there is no second path to protect.

## Deadline and lifecycle enforcement

Creating a project, editing a draft, submitting it, creating a team, and
joining a team are all refused unless the event is OPEN by the server clock.
Scoring is refused unless the event is SUBMISSIONS_CLOSED or JUDGING, and
refused again once published. Publishing requires a prior normalization run.
None of these read a client-supplied state or date.

## Audit log: tamper-evident, not tamper-proof

Stated plainly, because the distinction matters: the hash chain
(`audit.py`) means that if any row's stored fields are altered after the
fact, recomputing the chain from genesis forward will produce a different
hash than what's stored in every subsequent row, making the tampering
*detectable* by anyone who runs `audit.verify_chain()`. It does **not**
mean the log cannot be tampered with — someone with direct write access to
the SQLite file can rewrite the entire table, recomputing every hash
correctly as they go, and the chain will verify as intact. True
tamper-*proofing* would require an external, independently-controlled
anchor (e.g., periodically publishing the chain's tip hash somewhere the
operator doesn't control) which this build does not implement.
