import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class SurfaceTests(unittest.TestCase):
    def test_brightness_triggers_provisional_watch(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'surface-import', ROOT / 'fixtures/surface-ok.json')
            self.assertEqual(report(db)['surface_frame'][0]['state'], 'captured')
            self.assertEqual(report(db)['lane_watch'][0]['state'], 'watch')
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
