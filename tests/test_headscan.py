import tempfile
import unittest
from pathlib import Path
from signalphase.engine import audit, ingest, report


ROOT = Path(__file__).resolve().parent.parent


class HeadScanTests(unittest.TestCase):
    def test_capture_links_only_to_field_verified_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'flow-import', ROOT / 'fixtures/demo/01-flow.json')
            ingest(db, 'plan-import', ROOT / 'fixtures/demo/02-plan.json')
            ingest(db, 'field-import', ROOT / 'fixtures/demo/03-field.json')
            ingest(db, 'head-import', ROOT / 'fixtures/head-scan-ok.json')
            row = report(db)['head_scan'][0]
            self.assertEqual((row['state'], row['plan_id'], row['plan_revision']), ('attributed', 'safe-base', 1))
            self.assertTrue(audit(db)['replay_ok'])

    def test_capture_without_field_readback_is_unattributed(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'flow-import', ROOT / 'fixtures/demo/01-flow.json')
            ingest(db, 'plan-import', ROOT / 'fixtures/demo/02-plan.json')
            ingest(db, 'head-import', ROOT / 'fixtures/head-scan-ok.json')
            self.assertEqual(report(db)['head_scan'][0]['state'], 'unattributed')


if __name__ == '__main__':
    unittest.main()
