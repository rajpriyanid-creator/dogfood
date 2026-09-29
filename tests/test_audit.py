import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestAuditChain(VerdictLedgerTestCase):
    """Regression coverage for the audited P0 defect: seq was never
    populated (NULL on every row), so ORDER BY seq was ordering nothing
    in particular and verify_chain() returned False as soon as a second
    record existed. schema.sql now makes seq an autoincrementing rowid
    alias, which the fix in audit.py relies on.
    """

    def test_first_record_chains_to_genesis(self):
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        audit_module.record(conn, "test_action", "ok")
        rows = conn.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()
        conn.close()
        self.assertEqual(rows[-1]["prev_hash"], "genesis")

    def test_seq_is_populated_and_monotonic(self):
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        audit_module.record(conn, "event_created", "ok", resource="ev1")
        audit_module.record(conn, "event_created", "ok", resource="ev2")
        audit_module.record(conn, "event_created", "ok", resource="ev3")
        rows = conn.execute("SELECT seq FROM audit_events ORDER BY seq ASC").fetchall()
        conn.close()

        seqs = [r["seq"] for r in rows]
        self.assertTrue(all(s is not None for s in seqs),
                         "every row must have a non-NULL seq")
        # Strictly increasing, no gaps required, but must be sorted and unique.
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(len(seqs), len(set(seqs)), "seq values must be unique")

    def test_three_records_verify_as_intact_chain(self):
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        audit_module.record(conn, "event_created", "ok", resource="ev1")
        audit_module.record(conn, "event_created", "ok", resource="ev2")
        audit_module.record(conn, "event_created", "ok", resource="ev3")
        self.assertTrue(audit_module.verify_chain(conn))
        conn.close()

    def test_tampering_with_earlier_record_breaks_verification(self):
        """This is the exact regression the audit asked for: create
        three records, verify true, corrupt the middle one, verify
        false."""
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        audit_module.record(conn, "event_created", "ok", resource="ev1")
        audit_module.record(conn, "event_created", "ok", resource="ev2")
        audit_module.record(conn, "event_created", "ok", resource="ev3")
        self.assertTrue(audit_module.verify_chain(conn))

        # Tamper with the second record's resource field directly,
        # bypassing audit.record() entirely - simulating an out-of-band
        # database edit.
        conn.execute(
            "UPDATE audit_events SET resource = 'ev2-tampered' "
            "WHERE resource = 'ev2'"
        )
        conn.commit()

        self.assertFalse(audit_module.verify_chain(conn))
        conn.close()

    def test_chain_survives_many_records_in_sequence(self):
        """The original bug manifested specifically 'after the second
        record' per the audit - this exercises well past that count."""
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        for i in range(20):
            audit_module.record(conn, "score_submitted", "ok", resource=f"prj_{i}")
        self.assertTrue(audit_module.verify_chain(conn))
        conn.close()

    def test_empty_chain_verifies_true(self):
        """No records yet is a valid (vacuously intact) chain."""
        import audit as audit_module
        import db as db_module
        conn = db_module.get_connection()
        # This test DB was seeded, which itself writes audit records
        # (logins etc. do not happen during seed, but let's not assume -
        # check directly against a table we clear first).
        conn.execute("DELETE FROM audit_events")
        conn.commit()
        self.assertTrue(audit_module.verify_chain(conn))
        conn.close()

    def test_real_application_actions_produce_verifiable_chain(self):
        """End-to-end: drive real routes that call audit.record()
        internally (login, score submission) and confirm the resulting
        chain verifies - not just direct audit.record() calls."""
        import db as db_module
        import audit as audit_module

        resp = self.client.post("/login", data={
            "email": "organizer@verdictledger.local",
            "password": "organizer-demo",
        })
        self.assertEqual(resp.status_code, 302)

        self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))

        conn = db_module.get_connection()
        self.assertTrue(audit_module.verify_chain(conn))
        rows = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()
        conn.close()
        self.assertGreater(rows["n"], 0)


if __name__ == "__main__":
    unittest.main()
