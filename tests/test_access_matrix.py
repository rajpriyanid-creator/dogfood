import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import re
import unittest
from base import VerdictLedgerTestCase

# --- The access policy, stated once, explicitly ---------------------------
# For each (method, url_rule): which identities may NOT be refused on
# authorization grounds. "anon" = no credentials. Anything not listed
# for a rule must get 401 (anon) or 403 (authenticated but wrong role).
#
# Roles: anon, participant, judge, organizer, admin.
PUBLIC = {"anon", "participant", "judge", "organizer", "admin"}
ORG = {"organizer", "admin"}
ANY_AUTH = {"participant", "judge", "organizer", "admin"}

POLICY = {
    ("GET", "/"): PUBLIC,
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/projects"): PUBLIC,
    ("GET", "/projects/<project_id>"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("POST", "/login"): PUBLIC,
    ("POST", "/logout"): PUBLIC,
    ("GET", "/dashboard"): ANY_AUTH,

    ("GET", "/team"): {"participant"},
    ("POST", "/team/create"): {"participant"},
    ("POST", "/team/join"): {"participant"},
    ("GET", "/projects/new"): {"participant"},
    ("POST", "/projects/new"): {"participant"},
    ("GET", "/projects/<project_id>/edit"): {"participant"},
    ("POST", "/projects/<project_id>/edit"): {"participant"},
    ("POST", "/projects/<project_id>/submit"): {"participant"},

    ("GET", "/judge"): {"judge"},
    ("GET", "/judge/review/<project_id>"): {"judge"},
    ("POST", "/judge/review/<project_id>"): {"judge"},
    ("GET", "/api/judge/scores"): {"judge"},
    ("GET", "/api/judges/<judge_id>/scores"): {"judge"},

    ("GET", "/judge/progress"): ORG,
    ("GET", "/judge/progress/<event_id>"): ORG,
    ("GET", "/organizer"): ORG,
    ("GET", "/organizer/audit"): ORG,
    ("GET", "/organizer/events/new"): ORG,
    ("POST", "/organizer/events/new"): ORG,
    ("GET", "/organizer/events/<event_id>/judges"): ORG,
    ("POST", "/organizer/events/<event_id>/judges"): ORG,
    ("GET", "/organizer/events/<event_id>/assignments"): ORG,
    ("POST", "/organizer/events/<event_id>/assignments"): ORG,
    ("GET", "/organizer/events/<event_id>/rubric"): ORG,
    ("POST", "/organizer/events/<event_id>/rubric"): ORG,
    ("POST", "/organizer/events/<event_id>/tracks"): ORG,
    ("POST", "/organizer/events/<event_id>/prizes"): ORG,
    ("GET", "/organizer/integrity/<event_id>"): ORG,
    ("POST", "/organizer/normalize/<event_id>"): ORG,
    ("POST", "/organizer/publish/<event_id>"): ORG,
    ("GET", "/organizer/results/<event_id>"): ORG,
    ("GET", "/organizer/results/<event_id>/explain/<project_id>"): ORG,
    ("GET", "/api/export.csv"): ORG,
}

# Placeholder values. IDs belong to nothing the caller owns, so a route
# that got past its role guard still can't succeed by accident - we only
# assert on the guard's decision (401/403 vs "not 401/403").
# prj_07 is one jdg_01 is genuinely ASSIGNED to, so for the judge role a
# 403 on the review route can only be a role decision, not the separate
# (also correct) "not assigned to this project" resource denial.
FILL = {"project_id": "prj_07", "event_id": "evt_01", "judge_id": "jdg_01"}


def _url(rule):
    return re.sub(r"<(\w+)>", lambda m: FILL[m.group(1)], rule)


class TestAccessMatrix(VerdictLedgerTestCase):

    def _identities(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        out = {
            "anon": None,
            "participant": self.PARTICIPANT,
            "judge": self.JUDGE_A,
            "organizer": self.ORGANIZER,
            "admin": auth_module.create_session(conn, "admin"),
        }
        conn.close()
        return out

    def _routes(self):
        for r in self.app_module.app.url_map.iter_rules():
            if r.endpoint == "static":
                continue
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield m, r.rule

    def test_every_route_has_an_explicit_policy(self):
        """A new route added without deciding who may call it fails here,
        rather than silently shipping unguarded."""
        actual = set(self._routes())
        self.assertEqual(actual - set(POLICY), set(),
                         "routes with no access policy declared in this test")
        self.assertEqual(set(POLICY) - actual, set(),
                         "policy entries for routes that no longer exist")

    # Logout is public by design and REVOKES whatever session it is given,
    # so it is exercised separately (below) with throwaway sessions
    # instead of destroying the shared ones the rest of the matrix uses.
    MATRIX_SKIP = {("POST", "/logout")}

    def test_route_x_role_matrix(self):
        ids = self._identities()
        failures = []
        for method, rule in sorted(POLICY):
            if (method, rule) in self.MATRIX_SKIP:
                continue
            allowed = POLICY[(method, rule)]
            for who, token in ids.items():
                # Fresh state each call would be ideal; mutating routes
                # here use ids that resolve to nothing writable, and we
                # only look at the authorization decision.
                headers = self.auth_header(token) if token else {}
                if method == "GET":
                    resp = self.client.get(_url(rule), headers=headers)
                else:
                    resp = self.client.post(_url(rule), headers=headers, json={})
                # The oracle must be the ROLE GUARD's own refusal, not "any
                # 401/403": other correct refusals share those codes
                # (bad login credentials -> 401; closed event, published
                # event, or not-assigned -> 403) and are not role
                # decisions. The guard's refusals have exactly these
                # bodies (core.py); everything else has a specific message.
                body = resp.get_json(silent=True) or {}
                guard_refused = (resp.status_code == 401 and body.get("error") == "authentication required") \
                    or (resp.status_code == 403 and body.get("error") == "forbidden")
                should_be_refused = who not in allowed
                if guard_refused != should_be_refused:
                    failures.append(f"{method} {rule} as {who}: got {resp.status_code} {body.get('error')!r}, "
                                    f"expected {'a role-guard refusal' if should_be_refused else 'to pass the role guard'}")
                elif should_be_refused:
                    want = (401, "authentication required") if who == "anon" else (403, "forbidden")
                    if (resp.status_code, body.get("error")) != want:
                        failures.append(f"{method} {rule} as {who}: got {resp.status_code} {body.get('error')!r}, expected {want}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_logout_is_callable_by_everyone_and_only_revokes_the_callers_own_session(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        mine = auth_module.create_session(conn, "jdg_01")
        theirs = auth_module.create_session(conn, "jdg_02")
        conn.close()
        self.assertEqual(self.client.post("/logout").status_code, 302)          # anon: harmless
        self.assertEqual(self.client.post("/logout", headers=self.auth_header(mine)).status_code, 302)
        self.assertEqual(self.client.get("/api/judge/scores", headers=self.auth_header(mine)).status_code, 401)
        self.assertEqual(self.client.get("/api/judge/scores", headers=self.auth_header(theirs)).status_code, 200,
                         "logging out one session must not revoke another user's")

    def test_admin_is_not_a_participant_or_judge(self):
        """Explicit permission-model decision: admin inherits organizer
        powers only."""
        ids = self._identities()
        for method, rule in (("GET", "/team"), ("POST", "/team/create"),
                             ("POST", "/projects/new"), ("GET", "/api/judge/scores"),
                             ("GET", "/judge")):
            fn = self.client.get if method == "GET" else self.client.post
            kw = {} if method == "GET" else {"json": {}}
            self.assertEqual(fn(rule, headers=self.auth_header(ids["admin"]), **kw).status_code, 403, rule)


if __name__ == "__main__":
    unittest.main()
