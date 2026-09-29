import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import csv
import io
import unittest
from base import VerdictLedgerTestCase


class TestCsvExport(VerdictLedgerTestCase):

    def test_organizer_succeeds(self):
        resp = self.client.get("/api/export.csv?event=evt_01",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)

    def test_unauthorized_roles_fail(self):
        for token in (self.JUDGE_A, self.PARTICIPANT):
            resp = self.client.get("/api/export.csv?event=evt_01",
                                    headers=self.auth_header(token))
            self.assertEqual(resp.status_code, 403)
        resp = self.client.get("/api/export.csv?event=evt_01")
        self.assertEqual(resp.status_code, 401)

    def test_valid_csv_format(self):
        resp = self.client.get("/api/export.csv?event=evt_01",
                                headers=self.auth_header(self.ORGANIZER))
        text = resp.data.decode("utf-8")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        self.assertGreater(len(rows), 1)  # header + at least one data row
        header = rows[0]
        self.assertIn("judge_id", header)
        self.assertIn("project_id", header)
        # Every data row should have the same column count as the header.
        for row in rows[1:]:
            self.assertEqual(len(row), len(header))


if __name__ == "__main__":
    unittest.main()
