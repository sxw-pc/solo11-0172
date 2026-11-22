import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class FiberTests(unittest.TestCase):
    def test_single_end_echo_drives_provisional_splice_visit(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'fiber-import', ROOT / 'fixtures/fiber-ok.json')
            row = report(db)['fiber_triage'][0]
            self.assertEqual((row['distance_km'], row['state']), (1, 'splice_inspection'))
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
