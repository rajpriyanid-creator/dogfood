"""Deterministic, track-aware judge assignment.

Constraints honored:
  1. a judge is only assigned projects in tracks they are eligible for
  2. a judge is never assigned their own team's project
  3. each project gets a configurable target review count
  4. workload is balanced across eligible judges
  5. multiple judges may review the same project (blind overlap; batches
     are not required to be disjoint)
  6. judges never see one another's scores (enforced elsewhere, at the
     API layer — this module only decides who reviews what)

Determinism: judges and projects are processed in a fixed sort order
(by id), and each project's judges are chosen by workload-ascending order 
(breaking ties by judge id). Re-running this against the same inputs produces
the same assignment set, which is what "deterministic assignment"
means here — it does not mean an optimal balance, only a repeatable one.
"""
from collections import defaultdict


def build_assignments(projects, judges_by_track, team_owner_by_project,
                       target_reviews_per_project=3):
    """projects: list of dicts with id, track_id, team_id.
    judges_by_track: {track_id: [judge_id, ...]} (already track-eligible).
    team_owner_by_project: {project_id: set(user_id in that team)}.

    Returns list of (judge_id, project_id) pairs.
    """
    projects = sorted(projects, key=lambda p: p["id"])
    workload = defaultdict(int)  # judge_id -> assigned count
    pairs = []

    for project in projects:
        track_id = project["track_id"]
        pool = sorted(judges_by_track.get(track_id, []))
        if not pool:
            continue
        team_members = team_owner_by_project.get(project["id"], set())
        eligible = [j for j in pool if j not in team_members]
        if not eligible:
            continue

        # Choose judges preferring the least-loaded ones first,
        # breaking ties by judge id.
        n_target = min(target_reviews_per_project, len(eligible))
        ordered = sorted(eligible, key=lambda j: (workload[j], j))
        chosen = ordered[:n_target]

        for jid in chosen:
            pairs.append((jid, project["id"]))
            workload[jid] += 1

    return pairs
