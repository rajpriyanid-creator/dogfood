"""Shared test scaffolding.

Every test case gets its own throwaway SQLite file. The database is
produced by the real seed script (not a hand-rolled shortcut), so tests
exercise the same code path a real `docker compose up` would.

Seeding hashes ~128 passwords with PBKDF2, which dominates runtime, so
the real seed runs ONCE per test process into a template file and each
test gets a byte-for-byte copy of it. Isolation is unchanged (every test
mutates only its own copy); only the redundant re-hashing is skipped.
tests/test_seed_atomicity.py deliberately does not use this and calls
seed.main() directly, so the seed path itself stays under test.
"""
import importlib
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(REPO_ROOT, "src", "backend")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

for p in (BACKEND_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


_TEMPLATE_DB = None


def _seeded_template_db():
    """Run the real seed once per process; return the template path."""
    global _TEMPLATE_DB
    if _TEMPLATE_DB is not None and os.path.exists(_TEMPLATE_DB):
        return _TEMPLATE_DB
    tmpdir = tempfile.mkdtemp(prefix="vl_template_")
    path = os.path.join(tmpdir, "template.db")
    os.environ["VERDICT_LEDGER_DB"] = path
    for mod_name in ("db", "auth", "seed"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    import db as db_module
    db_module.DB_PATH = path
    db_module.init_schema()
    import seed as seed_module
    importlib.reload(seed_module)
    seed_module.FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures.json")
    seed_module.main()
    _TEMPLATE_DB = path
    return path


class VerdictLedgerTestCase(unittest.TestCase):
    """Gives each test its own copy of a fully-seeded database, plus a
    Flask test client bound to it."""

    def setUp(self):
        import shutil
        template = _seeded_template_db()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        shutil.copyfile(template, self.db_path)
        os.environ["VERDICT_LEDGER_DB"] = self.db_path

        # Reload db/app modules so they pick up the new DB_PATH env var
        # (module-level constants are read once at import time).
        for mod_name in ("db", "auth", "core", "events", "normalization",
                          "assignment", "audit", "app", "seed"):
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])

        import db as db_module
        db_module.DB_PATH = self.db_path

        import app as app_module
        importlib.reload(app_module)
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- convenience helpers -------------------------------------------------

    def auth_header(self, token):
        """Return kwargs for a request carrying this session token.

        Werkzeug's test client manages its own cookie jar and does not
        reliably forward a manually-constructed `Cookie:` request header
        through `client.get(..., headers=...)` — it can get shadowed by
        the (empty) jar. Using the Authorization/Bearer form instead
        exercises the same `extract_token_from_request` code path
        (auth.py) that a real bearer-token client would use, and is not
        subject to that shadowing.
        """
        return {"Authorization": f"Bearer {token}"}

    def api_header(self, token):
        """Auth plus Accept: application/json - what a real API client
        sends. Routes that serve both browsers and API callers choose
        their response format from this, not from guessing."""
        return {**self.auth_header(token), "Accept": "application/json"}

    JUDGE_A = "jdg_a_demo_token"
    JUDGE_B = "jdg_b_demo_token"
    ORGANIZER = "org_demo_token"
    PARTICIPANT = "prt_demo_token"
