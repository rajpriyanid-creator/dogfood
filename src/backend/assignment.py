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
                       target_reviews_per_project=3, initial_workload=None,
                       existing_pairs=None):
    """projects: list of dicts with id, track_id, team_id.
    judges_by_track: {track_id: [judge_id, ...]} (already track-eligible).
    team_owner_by_project: {project_id: set(user_id in that team)}.

    Returns list of (judge_id, project_id) pairs.
    """
    projects = sorted(projects, key=lambda p: p["id"])
    workload = defaultdict(int)
    if initial_workload:
        workload.update(initial_workload)
    existing_pairs = set(existing_pairs or ())
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

        already_assigned = {j for j in eligible if (j, project["id"]) in existing_pairs}
        n_target = max(0, min(target_reviews_per_project, len(eligible)) - len(already_assigned))
        eligible = [j for j in eligible if j not in already_assigned]
        if not eligible or not n_target:
            continue

        # Choose judges preferring the least-loaded ones first,
        # breaking ties by judge id.
        n_target = min(n_target, len(eligible))
        ordered = sorted(eligible, key=lambda j: (workload[j], j))
        chosen = ordered[:n_target]

        for jid in chosen:
            pairs.append((jid, project["id"]))
            workload[jid] += 1

    return pairs
