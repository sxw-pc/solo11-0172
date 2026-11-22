import tempfile
import unittest
from pathlib import Path
from signalphase.engine import ingest, report, audit


ROOT = Path(__file__).resolve().parent.parent


class CorridorTests(unittest.TestCase):
    def test_actual_field_activation_controls_provisional_route(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'a.sqlite'
            ingest(db, 'flow-import', ROOT / 'fixtures/demo/01-flow.json')
            ingest(db, 'plan-import', ROOT / 'fixtures/demo/02-plan.json')
            ingest(db, 'field-import', ROOT / 'fixtures/demo/03-field.json')
            ingest(db, 'corridor-import', ROOT / 'fixtures/corridor-ok.json')
            self.assertEqual((report(db)['corridor_check'][0]['active_plan'], report(db)['corridor_check'][0]['state']),
                             ('safe-base', 'provisional'))
            self.assertTrue(audit(db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
