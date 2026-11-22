import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class AirTests(unittest.TestCase):
    def test_single_source_advisory_is_provisional(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'air-import', ROOT / 'fixtures/air-ok.json')
            row = report(db)['air_advisory'][0]
            self.assertEqual((row['nearest_source'], row['distance'], row['state']),
                             ('diesel_exhaust', 1, 'provisional'))
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
