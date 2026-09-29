# Judging

## Assignment strategy

`src/backend/assignment.py::build_assignments` is a deterministic,
track-aware assignment engine. Constraints, in the order they're applied:

1. **Track eligibility.** A judge is only ever assigned a project in a
   track they're eligible for (`judge_track_eligibility`).
2. **No self-review.** A project's own team members are excluded from its
   eligible judge pool, even if one of them happens to hold a judge role
   elsewhere.
3. **Target review count.** Each project gets a configurable target number
   of reviewers (configurable per generation run; the fixture event's assignments
   are reconstructed from its recorded scores rather than generated), capped by however many eligible judges actually
   exist for that track.
4. **Workload balancing.** Among eligible judges for a project, the
   least-loaded-so-far judges (ties broken by judge id) are chosen first,
   so load spreads roughly evenly rather than piling onto whichever judge
   sorts first.
5. **Overlap, not disjoint batches.** Multiple judges may review the same
   project. The engine does not try to partition judges into disjoint
   batches — shared coverage across judges is what makes calibration and
   normalization possible in the first place.

**Determinism** means: given the same projects, the same track-eligible
judge pools, and the same team-ownership map, `build_assignments` returns
the exact same list of (judge, project) pairs every time
(`tests/test_assignment.py::test_determinism`). This is a repeatability
property, not a claim of optimality — it does not search for a
globally-optimal balance, it walks a fixed, explainable procedure.

## Rubric

Assignment generation is deterministic and additive: it inserts only missing
`(judge_id, project_id)` pairs and never removes existing assignments.

## Historical review evidence

Each normalization stores the review comment in `review_normalizations.comment`.
The explanation view reads that snapshot, so editing a live score comment after
normalization does not rewrite the historical explanation.

## Mixed rubric versions

Each review is calculated using the rubric version pinned on its score. A
project result records `rubric_version_id` only when all of its reviews used
one version. If reviews from multiple rubric versions are aggregated, the
result's `rubric_version_id` is NULL; no version is selected as a proxy.

Score ranges must be compatible; normalization refuses incompatible ranges
rather than pooling incomparable values. The documented normalization formula
and algorithm version are unchanged.

Weights must be non-negative and sum to 1.0 across an event's active
rubric version. The demo weighting used for both seeded events:

| Criterion | Weight |
|---|---|
| Functionality | 40% |
| Quality | 35% |
| Innovation | 25% |

**This weighting is a design choice for the demonstration, not something
the DOGFOOD fixture data specifies** — `fixtures.json`'s `scores` entries
carry raw per-criterion values (`functionality`, `quality`, `innovation`)
but no organizer-assigned weights. Verdict Ledger's rubric engine is fully
general — an organizer can define their own criteria, labels, descriptions,
and weights per event — this table just documents the specific numbers
used to compute the fixture event's raw and normalized scores shown in this
build.

Raw score per review: **S = Σ(weight_i × criterion_score_i)**.

## Normalization

This is the signature technical feature, and the part most worth reading
carefully.

### What it is, and what it is explicitly not

A **transparent, sample-size-shrunk location-scale heuristic inspired by
empirical-Bayes reasoning**. It borrows the shrinkage intuition from
empirical Bayes (pull a small-sample estimate toward a shared baseline,
by an amount that shrinks as the sample grows) without implementing full
empirical Bayes (there is no hierarchical model, no estimated prior
distribution shape, no maximum-likelihood or method-of-moments fit for the
prior's own parameters — `k` is fixed, not estimated from the data). This
codebase and its documentation never call it "full empirical Bayes,"
deliberately.

### The formula

For each review, the raw weighted score is:

```
S = Σ(w_i × x_i)                    (rubric weights × criterion scores)
```

Across all reviews in an event, the global statistics are:

```
μ_g = mean(S)                        global mean
v_g = variance(S)                    global (population) variance
```

Per judge, across only the reviews that judge actually submitted:

```
n_j  = that judge's review count
μ_j  = mean(S) over the judge's own reviews
v_j  = variance(S) over the judge's own reviews    (population variance)
```

With a fixed design parameter **k = 3**, the judge's mean and variance are
shrunk toward the global statistics:

```
μ*_j = (n_j·μ_j + k·μ_g) / (n_j + k)
v*_j = (n_j·v_j + k·v_g) / (n_j + k)
σ*_j = sqrt(v*_j)
```

For a review by a judge whose **shrunk** variance is nonzero, the
normalized score is:

```
z = (S − μ*_j) / σ*_j
N = clip(μ_g + z·σ_g, 1, 5)
```

For a judge whose **observed** variance is exactly zero — every review
that judge submitted got the identical raw score, so there is no
relative discrimination signal to work with — the normalized score is
simply the global mean:

```
N = μ_g
```

`σ_g = sqrt(v_g)` is the global standard deviation.

### Why k = 3, and why it's a design parameter, not a constant

`k` sets how many "phantom" global-average reviews a judge's own history is
blended against before computing their shrunk mean and variance. A judge
with `n_j` far larger than `k` ends up close to their own unshrunk
statistics; a judge with `n_j` at or below `k` gets pulled hard toward the
global distribution. **This is a chosen constant, not something derived
from the fixture data or estimated by any statistical procedure** — a
different value would trade off differently between "trust each judge's
own scale" and "assume every judge is like the average judge until proven
otherwise," and there is no single correct value that formula-fitting
would produce. `k = 3` was chosen because it is small enough that a judge
with 5+ reviews (the bulk of the fixture's judges) is only lightly
shrunk, while a judge with 1–2 reviews is shrunk substantially — which
matches the intuition that a couple of data points should not be trusted
as much as a dozen.

### Edge cases

**n = 1.** A single review has zero variance by definition (there is only
one point, so there is no spread to measure). This is *not* special-cased
separately in the code — it falls naturally out of the zero-variance rule
above: `v_j = 0` when `n_j = 1`, so `normalize_one` takes the zero-variance
branch and returns the global mean. Verified against the real fixture data
in `tests/test_normalization.py::test_fixture_low_sample_judges` for
`jdg_01` and `jdg_23`, both `n=1`.

**Zero variance (n > 1).** A judge who gave every reviewed project the
identical raw score. Same rule, same reasoning: no relative signal, fall
back to the global mean. Verified against `jdg_07` (n=3, all three raw
scores exactly 4.0) in
`tests/test_normalization.py::test_fixture_zero_variance_judge_jdg_07`.

**Missing reviews.** Never imputed as zero. A judge's `n_j`, `μ_j`, `v_j`
are computed only over the reviews that judge actually submitted — a judge
with 2 reviews is not treated as having 2 real scores and some number of
phantom zero scores. Verified in
`test_missing_reviews_never_treated_as_zero`.

**Empty/missing comments.** The normalization engine never reads
`comment` at all — it's not part of the formula in any way. The UI
renders an empty or absent comment as "No comment provided" rather than
`undefined`/`null`/`None` (see `explain_result.html` and `integrity.html`).

**Ties.** The formula has no random tiebreak anywhere — two reviews with
identical raw scores from the same judge always normalize to the exact
same value (`test_tie_handling_is_deterministic`). Where a full leaderboard
needs a strict ordering and two projects land on the identical normalized
average, this build does not impose an arbitrary secondary sort; a tie in
the data is left visible as a tie.

**Out-of-range values.** `judge_review()` in `app.py` validates every
submitted criterion value against that criterion's `min_value`/`max_value`
before it's ever written to the database or fed into normalization —
invalid values are rejected with a 400 at submission time, not silently
clamped later.

### Reproducibility

Running normalization twice against unchanged underlying scores produces
identical raw and normalized values each time
(`tests/test_results.py::test_reproducibility_rerun_same_scores_same_result`),
because `run_normalization` is a pure function of its inputs — same
reviews and weights in, same numbers out, no hidden state or randomness
anywhere in the calculation.

## Fixture proof: worked examples from the real data

All numbers below come from running `run_normalization` against the
actual `fixtures.json` scores with the demo rubric weights above (global
mean ≈ 3.568, global variance ≈ 0.450, global sd ≈ 0.671):

**Zero-variance judge — `jdg_07`.** 3 reviews, every one scored
functionality=4, quality=4, innovation=4 → raw S=4.0 on all three.
Observed variance is exactly 0. Every one of that judge's normalized
scores collapses to the global mean, ≈3.568, regardless of the raw 4.0 —
this judge supplies zero discrimination signal, so normalization treats
their scores as uninformative about *relative* project quality.

**n=1 judge — `jdg_01`.** One review, raw S=2.0 (a stricter-than-average
single score). Because n=1 implies zero variance by definition, this also
collapses to the global mean, ≈3.568 — a lone score, however different
from the average, is not enough evidence on its own to say this judge runs
harsh or lenient, so it is not allowed to swing that project's normalized
result on its own.

**Ordinary judge with real spread — `jdg_24`.** 11 reviews, own mean
≈3.318, own variance ≈0.416 (nonzero). Shrunk mean ≈3.372, shrunk sd
≈0.651 — close to, but not identical to, the judge's own raw statistics,
since 11 reviews is well above k=3 and dominates the blend. This judge's
individual scores keep most of their original relative spread after
normalization.

**Rank movement — the Dry Harbour duplicate, `prj_07`/`prj_41`.** Both
titled "Dry Harbour," preserved as separate rows (see DATA-MODEL.md).
`prj_07`'s reviews include `jdg_01`'s lone, harsh 2.0 (which normalizes up
to the global mean rather than dragging the project's average down) sitting
alongside ordinary reviews from `jdg_19`, `jdg_21`, `jdg_26`, `jdg_12` — the
normalized average for `prj_07` sits closer to those ordinary reviewers'
consensus than the raw average does, specifically because the one
low-information outlier score was pulled back toward the baseline instead
of being taken at face value.

## Demo weights, restated

To be unambiguous: functionality 40%, quality 35%, innovation 25% is *this
build's* choice for demonstration purposes, applied identically to both
the fixture event and the live demo event. An organizer using Verdict
Ledger for a real event would set their own weights through the rubric
engine; nothing about the normalization formula assumes these specific
numbers.

## Limitations of the method

- **k=3 is fixed, not tuned.** A different hackathon with very different
  judge-count/review-count patterns might warrant a different k; this
  build does not attempt to estimate an optimal k from the data, and
  doing so honestly would require a much larger, more principled model
  than a location-scale heuristic.
- **The zero-variance fallback is coarse.** A judge who gives 3 reviews
  scored 4, 4, 4.5 has *technically* nonzero variance and would not hit
  the fallback, while a judge who gives 4, 4, 4 exactly does — the rule is
  a bright line on exact equality, not a threshold on "close to zero."
- **No cross-track calibration.** Normalization here runs across an entire
  event, not separately per track. A track whose judges are systematically
  stricter or more lenient than another track's judges is not adjusted for
  differently — only per-judge shrinkage is modeled, not per-track effects.
- **This is not a peer-reviewed statistical method.** It is a documented,
  defensible, and fully transparent heuristic — every number it produces
  can be traced back to its inputs via the "why did this change?" view —
  but it has not been validated against ground truth, and no such
  validation is claimed.

## Tie behavior in results

Documented above under "Ties" — no synthetic tiebreak is applied.

## Reproducibility, restated

Every `project_results` row records which `normalization_run_id` and
`rubric_version_id` produced it. Given the same scores and the same
rubric, re-running normalization reproduces the same raw and normalized
values — this is a property of the pure function
`normalization.run_normalization`, verified directly by
`tests/test_normalization.py::test_reproducibility_same_inputs_same_outputs`
and end-to-end through the database by
`tests/test_results.py::test_reproducibility_rerun_same_scores_same_result`.

## Normalization freshness

Publishing results from a stale normalization run — one whose underlying
scores or criteria have changed since it was computed — is refused. The
mechanism:

1. When `run_normalization_route` creates a normalization run, it computes a
   SHA-256 fingerprint of the canonical judging state: all scores, their
   per-criterion values, and the active rubric weights, sorted
   deterministically. This fingerprint is stored as
   `judging_state_fingerprint` on the `normalization_runs` row.
2. When `publish_results()` is called, it recomputes the same fingerprint
   from the current database state.
3. If the two fingerprints differ, the request is refused with **HTTP 409**
   and a message explaining that scores have changed since the last
   normalization, and the organizer must re-run normalization before
   publishing.

This prevents the scenario where an organizer normalizes, a judge edits a
score, and the organizer publishes — the published results would not reflect
the actual current judging state. The organizer's path is:
normalize → (optionally edit scores) → re-normalize → publish.

## Historical criteria snapshot

When a normalization run is created, the exact per-score criterion values
(`score_criteria` rows) consumed by the calculation are copied into the
`normalization_run_criteria` table. The `explain_result()` view reads from
this snapshot, never from the live `score_criteria` table.

This means that editing a score's criteria after normalization (whether by a
judge correcting a review or by an organizer adjusting data) does not
retroactively change what that normalization run's "why did this change?"
page reports. The explanation always shows the values the math actually
used, not whatever the live table happens to contain at view time.

The snapshot is write-once: it is created alongside the normalization run and
never updated. A new normalization run creates its own independent snapshot.
