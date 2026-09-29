# Verdict Ledger — 5 Minute Demo Flow

This script walks through the core T1 + T2 capabilities of Verdict Ledger.

## 0:00 — Boot
Run `docker compose up`. The app boots on `http://localhost:8080`, seeds its SQLite database idempotently, and requires no external network access.

## 0:30 — Live Event
Open `http://localhost:8080` in your browser. You will see two events: the historical DOGFOOD fixture (`evt_01`) and an open live demo event. Click into the **live demo event**.

## 1:00 — Team Creation (T1)
Log in as `participant1@verdictledger.local` (password: `participant-demo`).
Navigate to the event and click **Create Team**.

## 1:30 — Project Submission (T1)
With your team created, click **New Project**. Fill in a title, summary, and a valid HTTP/HTTPS repository URL. Click **Save Draft**, then **Submit Project**. 

## 2:00 — Assignment (T2)
Log in as `organizer@verdictledger.local` (password: `organizer-demo`).
Go to the **Organizer Room** for the live event.
Run **Generate Assignments** to deterministically pair the new submission with eligible judges based on track.

## 2:30 — Private Scoring (T2)
Log in as `judge1@verdictledger.local` (password: `judge-demo`).
You will see your assigned project. Submit a review using the 3-criterion weighted rubric.

## 3:00 — Backend Judge Isolation
*The Money Shot.*
Log in as `judge2@verdictledger.local` (password: `judge-demo`).
Attempt to access Judge 1's scores directly by navigating to the API endpoint for their scores:
`http://localhost:8080/api/judge/scores?judge=jdg_01`
**Result:** HTTP 403 Forbidden. The backend actively denies access because the session identity does not match the requested identity. Hiding the link in the UI is not enough; the isolation is enforced at the route level.

## 3:30 — Normalization
Log back in as `organizer@verdictledger.local`.
Go to the Organizer Room and run **Normalize Scores**.
This computes the sample-size shrunk location-scale normalization for all reviews, and takes a cryptographic snapshot of the exact criteria used.

## 4:00 — Inspect Evidence
Click **Explain** on any normalized result to open the "Why did this change?" page.
You will see:
- The raw score
- The normalized score
- The exact per-judge contribution and shrinkage
- The historical evidence snapshot (which remains immutable even if live scores are later edited)

## 4:30 — Stale Normalization Gate
To demonstrate freshness protection:
As a judge, edit a score that was already normalized.
As the organizer, attempt to click **Publish** in the Organizer Room.
**Result:** HTTP 409 Conflict. The system detects that the judging-state fingerprint has changed since the last normalization run, preventing the publication of stale results.

## 5:00 — Rerun and Publish
As the organizer, click **Normalize Scores** again to incorporate the edited score.
Click **Publish**. It now succeeds.
Click **Download CSV Export** to obtain the final, official export of the event's results.
