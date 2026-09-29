"""The Verdict Ledger normalization engine.

This is a transparent, sample-size-shrunk location-scale heuristic
inspired by empirical-Bayes reasoning. It is NOT "full empirical Bayes" —
that phrase is deliberately avoided everywhere in this codebase and in
the docs, per the design brief. It is a documented, defensible design
choice with a fixed shrinkage parameter k, not a universal statistical
constant.

Formula (as specified):

    weighted raw score for a review:
        S = sum(w_i * x_i)                    over rubric criteria i

    global mean / variance of all raw weighted scores:
        mu_g, v_g

    per-judge, over that judge's reviews:
        n_j   = number of reviews
        mu_j  = mean of S across the judge's reviews
        v_j   = variance of S across the judge's reviews  (population variance)

    fixed design parameter:
        k = 3

    shrunk mean:
        mu*_j = (n_j * mu_j + k * mu_g) / (n_j + k)

    shrunk variance:
        v*_j  = (n_j * v_j + k * v_g) / (n_j + k)

    shrunk standard deviation:
        sigma*_j = sqrt(v*_j)

For a review by a judge with shrunk variance > 0:

    z = (S - mu*_j) / sigma*_j
    N = clip(mu_g + z * sigma_g, 1, 5)

For a judge whose *observed* variance is exactly zero (gave every
reviewed project the identical raw score), that judge supplies no
relative discrimination signal, so:

    N = mu_g

sigma_g is sqrt(v_g), the global standard deviation.

Design notes worth stating out loud:
  - k=3 is a fixed design parameter, not derived from the data. It sets
    how many "phantom" global-average reviews a judge's own history is
    blended against. A judge with n_j >> k behaves close to un-shrunk;
    a judge with n_j=1 is pulled hard toward the global distribution.
  - Missing reviews are never imputed as zero. A judge's n_j, mu_j, v_j
    are computed only over reviews that judge actually submitted.
  - Population (not sample) variance is used throughout, matching v_j
    and v_g both being defined the same way, so they are comparable in
    the same blend.
"""
import math
from dataclasses import dataclass

K_DEFAULT = 3.0


def weighted_raw_score(criterion_values: dict, weights: dict) -> float:
    """S = sum(w_i * x_i) for one review."""
    return sum(weights[k] * v for k, v in criterion_values.items() if k in weights)


def _mean(values):
    return sum(values) / len(values)


def _population_variance(values, mean_val=None):
    if len(values) == 0:
        return 0.0
    m = mean_val if mean_val is not None else _mean(values)
    return sum((x - m) ** 2 for x in values) / len(values)


@dataclass
class JudgeStats:
    judge_id: str
    n: int
    mean: float
    variance: float
    shrunk_mean: float
    shrunk_variance: float
    shrunk_sd: float
    zero_variance: bool


@dataclass
class GlobalStats:
    n: int
    mean: float
    variance: float
    sd: float
    k: float = K_DEFAULT


def compute_global_stats(all_raw_scores: list, k: float = K_DEFAULT) -> GlobalStats:
    mu_g = _mean(all_raw_scores) if all_raw_scores else 0.0
    v_g = _population_variance(all_raw_scores, mu_g)
    return GlobalStats(n=len(all_raw_scores), mean=mu_g, variance=v_g,
                        sd=math.sqrt(v_g), k=k)


def compute_judge_stats(judge_id: str, raw_scores_for_judge: list,
                         global_stats: GlobalStats, k: float = K_DEFAULT) -> JudgeStats:
    n_j = len(raw_scores_for_judge)
    mu_j = _mean(raw_scores_for_judge) if n_j else global_stats.mean
    v_j = _population_variance(raw_scores_for_judge, mu_j) if n_j else global_stats.variance
    zero_variance = n_j > 0 and v_j == 0.0

    shrunk_mean = (n_j * mu_j + k * global_stats.mean) / (n_j + k)
    shrunk_var = (n_j * v_j + k * global_stats.variance) / (n_j + k)
    shrunk_sd = math.sqrt(shrunk_var) if shrunk_var > 0 else 0.0

    return JudgeStats(
        judge_id=judge_id, n=n_j, mean=mu_j, variance=v_j,
        shrunk_mean=shrunk_mean, shrunk_variance=shrunk_var,
        shrunk_sd=shrunk_sd, zero_variance=zero_variance,
    )


def normalize_one(raw_score: float, judge_stats: JudgeStats,
                   global_stats: GlobalStats, lo: float = 1.0, hi: float = 5.0):
    """Return (normalized_value, z_score) for one review.

    z_score is None when the judge supplies no discrimination signal
    (zero observed variance), in which case the normalized value is the
    global mean. lo/hi are the rubric's score bounds - callers derive
    them from the rubric actually used, rather than this function
    assuming a 1..5 scale.
    """
    if judge_stats.zero_variance:
        return global_stats.mean, None
    if judge_stats.shrunk_sd == 0:
        # No discrimination signal available even after shrinkage
        # (not reachable with the default k > 0, but guarded rather
        # than dividing by zero).
        return global_stats.mean, None
    z = (raw_score - judge_stats.shrunk_mean) / judge_stats.shrunk_sd
    n = global_stats.mean + z * global_stats.sd
    return max(lo, min(hi, n)), z


def run_normalization(reviews: list, weights: dict, k: float = K_DEFAULT,
                      lo: float = 1.0, hi: float = 5.0):
    """reviews: list of dicts with keys judge_id, project_id, criteria (dict).
    lo/hi: the score bounds of the rubric these reviews were scored on.

    Returns (global_stats, {judge_id: JudgeStats},
             [{**review, raw, normalized, z}]).

    Every review passed in must have been scored against the same
    rubric scale - the caller (app.py) is responsible for refusing to
    pool reviews from rubric versions with different weights/ranges,
    since a global mean over incomparable scales is meaningless.
    """
    enriched = []
    for r in reviews:
        raw = weighted_raw_score(r["criteria"], weights)
        enriched.append({**r, "raw": raw})

    all_raw = [r["raw"] for r in enriched]
    global_stats = compute_global_stats(all_raw, k=k)

    by_judge = {}
    for r in enriched:
        by_judge.setdefault(r["judge_id"], []).append(r["raw"])

    judge_stats = {
        jid: compute_judge_stats(jid, scores, global_stats, k=k)
        for jid, scores in by_judge.items()
    }

    for r in enriched:
        js = judge_stats[r["judge_id"]]
        r["normalized"], r["z"] = normalize_one(r["raw"], js, global_stats, lo=lo, hi=hi)

    return global_stats, judge_stats, enriched
