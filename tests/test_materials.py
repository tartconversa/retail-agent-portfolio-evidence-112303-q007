import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'candidate_scenarios.csv': (['scenario_id', 'department', 'process', 'monthly_volume', 'baseline_hours', 'error_rate', 'target_kpi', 'owner_state'], 36), 'dependencies.csv': (['scenario_id', 'dependency', 'capacity_units', 'shared_group', 'readiness_state', 'version'], 72), 'governance_reviews.csv': (['review_id', 'scenario_id', 'data_scope', 'purpose', 'risk_level', 'decision', 'reviewed_at'], 48), 'funding_stages.csv': (['stage_id', 'scenario_id', 'amount', 'release_condition', 'committee_decision', 'released_amount', 'result_state'], 54), 'pilot_results.csv': (['result_id', 'scenario_id', 'period', 'observed_volume', 'observed_hours', 'outcome_value', 'measurement_state', 'source_version'], 60)}

class MaterialTests(unittest.TestCase):
    def test_declared_csv_contracts(self):
        for filename, (columns, count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))

if __name__ == "__main__":
    unittest.main()
