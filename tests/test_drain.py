import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class DrainTests(unittest.TestCase):
    def test_high_level_causes_provisional_visit(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'drain-import', ROOT / 'fixtures/drain-ok.json')
            self.assertEqual(report(db)['drain_visit'][0]['state'], 'inspect')
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
