import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class BusTests(unittest.TestCase):
    def test_door_batch_net_load_is_provisional(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'bus-import', ROOT / 'fixtures/bus-stop-ok.json')
            row = report(db)['bus_load'][0]
            self.assertEqual((row['end_load'], row['state']), (10, 'nominal_within'))
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
