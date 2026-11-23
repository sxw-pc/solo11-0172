import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / 'fixtures' / 'drain-balance.json'
CMD = 'drain-balance-import'
T0, T1 = '2026-01-02T08:00:00Z', '2026-01-02T08:10:00Z'


class DrainBalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.counter = 0

    def send(self, db, *events):
        self.counter += 1
        path = self.dir / f'{self.counter}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(db, CMD, path)

    def base(self, db):
        ingest(db, CMD, FIXTURE)

    def fresh(self):
        self.counter += 1
        return self.dir / f'db-{self.counter}.sqlite'

    def rows(self, db, table):
        return report(db)[table]

    def test_visit_flips_from_inspect_to_held_when_validity_window_moves(self):
        valid_db, held_db = self.fresh(), self.fresh()
        self.base(valid_db)
        visit = self.rows(valid_db, 'drain_visit')[0]
        self.assertEqual(visit['state'], 'inspect')
        self.assertEqual(visit['reason'], 'high_water_level')

        events = deepcopy(json.loads(FIXTURE.read_text(encoding='utf-8'))['events'])
        for event in events:
            if event['kind'] == 'drain_well':
                event['valid_to'] = T0  # DL1 at T0 is now outside [valid_from, valid_to)
        # The balance itself refuses non-captured levels; here we only replay
        # the validity change down to the visit decision.
        events = [event for event in events if event['kind'] != 'drain_balance']
        self.counter += 1
        expired = self.dir / 'expired.json'
        expired.write_text(json.dumps({'events': events}, ensure_ascii=False), encoding='utf-8')
        ingest(held_db, CMD, expired)
        level = next(row for row in self.rows(held_db, 'drain_level') if row['id'] == 'DL1')
        visit = next(row for row in self.rows(held_db, 'drain_visit') if row['id'] == 'DV1')
        self.assertEqual(level['state'], 'excluded')
        self.assertEqual(visit['state'], 'held')
        self.assertEqual(visit['reason'], 'level_not_valid')

    def test_two_worlds_share_levels_but_keep_two_feasible_balances(self):
        db = self.fresh()
        self.base(db)
        balance = self.rows(db, 'drain_balance')[0]
        # 10 m2 * (1.3 - 1.2) m = 1 m3 over 1/6 hour -> +6 m3/h net
        self.assertEqual(balance['storage_change_m3'], 1.0)
        self.assertEqual(balance['net_rate_m3h'], 6.0)
        self.assertEqual(balance['revision'], 1)
        self.assertEqual(balance['state'], 'balanced')
        solutions = self.rows(db, 'drain_balance_solution')
        self.assertEqual({s['scenario_id'] for s in solutions}, {'world-jia', 'world-yi'})
        self.assertTrue(all(s['feasible'] == 1 for s in solutions))
        jia = next(s for s in solutions if s['scenario_id'] == 'world-jia')
        yi = next(s for s in solutions if s['scenario_id'] == 'world-yi')
        self.assertEqual((jia['inflow_m3h'], jia['outflow_m3h']), (18.0, 12.0))
        self.assertEqual((yi['inflow_m3h'], yi['outflow_m3h']), (6.0, 0.0))
        # Net rise alone never locates a blockage: no cleaning order, no pump
        # order before outlet evidence exists.
        self.assertEqual(self.rows(db, 'drain_dispatch'), [])
        # Pump is energized in the fixture but that is not outflow proof.
        self.assertEqual(balance['known_outflow_m3h'], 0.0)
        gap_kinds = {row['kind'] for row in self.rows(db, 'drain_balance_gap')}
        self.assertEqual(gap_kinds, {'outflow_unverified', 'inflow_branch_unlocated'})
        self.assertTrue(audit(db)['replay_ok'])

    def test_one_term_set_must_explain_every_segment_of_the_series(self):
        db = self.fresh()
        self.base(db)
        self.send(db, {'id': 'bl3', 'kind': 'drain_level', 'level_id': 'DL3',
                       'well_id': 'north-curb', 'observed_at': '2026-01-02T08:05:00Z',
                       'depth_m': 1.29, 'recorded_at': '2026-01-02T08:06:00Z'})
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL3', 'DL2'],
                       'recorded_at': '2026-01-02T08:20:00Z'})
        balance = self.rows(db, 'drain_balance')[-1]
        self.assertEqual(balance['state'], 'inconsistent_series')
        # An unexplained per-minute outlet swap must not silently create orders.
        self.assertEqual([d for d in self.rows(db, 'drain_dispatch')
                          if d['balance_id'] == 'BAL2'], [])
        # Candidate balances are still rejected on record rather than erased.
        self.assertTrue(all(s['feasible'] == 0 for s in self.rows(db, 'drain_balance_solution')
                            if s['balance_id'] == 'BAL2'))

    def test_route_a_clamp_meter_proves_open_outlet_and_retention_only(self):
        db = self.fresh()
        self.base(db)
        self.send(db, {'id': 'bf1', 'kind': 'drain_flow', 'flow_id': 'F1',
                       'well_id': 'north-curb', 'point': 'outlet-1', 'direction': 'outflow',
                       'from_at': T0, 'to_at': T1, 'rate_m3h': 12.0,
                       'provenance': 'clamp_meter', 'recorded_at': '2026-01-02T08:20:00Z'})
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:21:00Z'})
        balance = self.rows(db, 'drain_balance')[-1]
        self.assertEqual(balance['known_outflow_m3h'], 12.0)
        jia = next(s for s in self.rows(db, 'drain_balance_solution')
                   if s['balance_id'] == 'BAL2' and s['scenario_id'] == 'world-jia')
        yi = next(s for s in self.rows(db, 'drain_balance_solution')
                  if s['balance_id'] == 'BAL2' and s['scenario_id'] == 'world-yi')
        self.assertEqual(jia['feasible'], 1)
        self.assertEqual(yi['feasible'], 0)
        self.assertIn('conflicts_with_measured_outflow', yi['reason'])
        actions = {d['action'] for d in self.rows(db, 'drain_dispatch')}
        self.assertEqual(actions, {'retention'})
        # The 18 m3/h inflow is implied but no branch ditch is located.
        gap = next(g for g in self.rows(db, 'drain_balance_gap')
                   if g['balance_id'] == 'BAL2' and g['kind'] == 'inflow_branch_unlocated')
        self.assertEqual(gap['missing_m3h'], 18.0)
        self.assertFalse(any(g['balance_id'] == 'BAL2' and g['kind'] == 'outflow_unverified'
                             for g in self.rows(db, 'drain_balance_gap')))

    def test_route_b_radar_covers_roof_but_culvert_and_outlet_stay_open_gaps(self):
        db = self.fresh()
        self.base(db)
        # 10 mm/h = 0.010 m/h over 1000 m2 * 0.6 loss -> exactly 6 m3/h
        self.send(db, {'id': 'br1', 'kind': 'drain_rainfall', 'rainfall_id': 'R1',
                       'catchment_id': 'roof-c', 'well_id': 'north-curb',
                       'from_at': T0, 'to_at': T1, 'intensity_mh': 0.010,
                       'recorded_at': '2026-01-02T08:20:00Z'})
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:21:00Z'})
        rain = self.rows(db, 'drain_rainfall')[0]
        self.assertEqual(rain['inflow_m3h'], 6.0)
        self.assertEqual(rain['state'], 'converted')
        balance = self.rows(db, 'drain_balance')[-1]
        # Total inflow may be anywhere from 6 to 18; both worlds survive.
        self.assertEqual(balance['known_inflow_m3h'], 6.0)
        self.assertEqual({s['scenario_id'] for s in self.rows(db, 'drain_balance_solution')
                          if s['balance_id'] == 'BAL2' and s['feasible'] == 1},
                         {'world-jia', 'world-yi'})
        dispatch = self.rows(db, 'drain_dispatch')
        self.assertEqual({d['action'] for d in dispatch},
                         {'portable_pump', 'pipeline_inspection'})
        self.assertFalse(any(d['action'] == 'cleaning' for d in dispatch))
        gaps = {(g['kind'], g['source']) for g in self.rows(db, 'drain_balance_gap')
                if g['balance_id'] == 'BAL2'}
        self.assertIn(('inflow_uncovered', 'culvert-c'), gaps)
        self.assertIn(('outflow_unverified', 'outlet-1'), gaps)
        self.assertNotIn(('inflow_uncovered', 'roof-c'), gaps)

    def test_late_blockage_evidence_creates_new_revision_without_erasing_old_work(self):
        db = self.fresh()
        self.base(db)
        # Route B first: temporary drainage and pipe inspection.
        self.send(db, {'id': 'br1', 'kind': 'drain_rainfall', 'rainfall_id': 'R1',
                       'catchment_id': 'roof-c', 'well_id': 'north-curb',
                       'from_at': T0, 'to_at': T1, 'intensity_mh': 0.010,
                       'recorded_at': '2026-01-02T08:20:00Z'})
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:21:00Z'})
        inspection = next(d for d in self.rows(db, 'drain_dispatch')
                          if d['action'] == 'pipeline_inspection')
        self.send(db, {'id': 'bwk1', 'kind': 'drain_work_receipt', 'receipt_id': 'WK1',
                       'dispatch_id': inspection['id'], 'well_id': 'north-curb',
                       'executed_at': '2026-01-02T09:00:00Z', 'work_hours': 1.5,
                       'recorded_at': '2026-01-02T09:01:00Z'})
        # Late evidence switches the real world to yi: outlet flow is zero.
        self.send(db, {'id': 'bf1', 'kind': 'drain_flow', 'flow_id': 'F1',
                       'well_id': 'north-curb', 'point': 'outlet-1', 'direction': 'outflow',
                       'from_at': T0, 'to_at': T1, 'rate_m3h': 0.0,
                       'provenance': 'clamp_meter', 'recorded_at': '2026-01-02T09:10:00Z'})
        self.send(db, {'id': 'bb3', 'kind': 'drain_balance', 'balance_id': 'BAL3',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T09:11:00Z'})
        balances = self.rows(db, 'drain_balance')
        self.assertEqual([b['revision'] for b in balances], [1, 2, 3])
        latest = balances[-1]
        self.assertEqual(latest['state'], 'balanced')
        surviving = next(s for s in self.rows(db, 'drain_balance_solution')
                         if s['balance_id'] == 'BAL3' and s['feasible'] == 1)
        self.assertEqual(surviving['scenario_id'], 'world-yi')
        self.assertEqual(surviving['feasible'], 1)
        new_actions = {d['action'] for d in self.rows(db, 'drain_dispatch')
                       if d['balance_id'] == 'BAL3'}
        self.assertEqual(new_actions, {'cleaning', 'portable_pump'})
        # The executed inspection labour and its original hours are untouched.
        receipt = self.rows(db, 'drain_work_receipt')[0]
        self.assertEqual(receipt['work_hours'], 1.5)
        self.assertEqual(self.rows(db, 'drain_visit')[0]['state'], 'inspect')
        executed = next(d for d in self.rows(db, 'drain_dispatch')
                        if d['id'] == inspection['id'])
        self.assertEqual(executed['state'], 'executed')
        self.assertTrue(audit(db)['replay_ok'])

    def test_pump_power_alone_is_not_outflow_but_metered_receipt_is(self):
        db = self.fresh()
        self.base(db)
        # A receipt for another window cannot be stitched onto the 08:00 window.
        self.send(db, {'id': 'bpr0', 'kind': 'drain_pump_receipt', 'receipt_id': 'PR0',
                       'pump_id': 'pump-1', 'well_id': 'north-curb', 'outlet_id': 'outlet-1',
                       'from_at': '2026-01-02T09:00:00Z', 'to_at': '2026-01-02T09:10:00Z',
                       'volume_m3': 5.0, 'recorded_at': '2026-01-02T09:12:00Z'})
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:21:00Z'})
        self.assertEqual(self.rows(db, 'drain_balance')[-1]['known_outflow_m3h'], 0.0)
        # 2 m3 discharged over 10 minutes = 12 m3/h, matching world jia.
        self.send(db, {'id': 'bpr1', 'kind': 'drain_pump_receipt', 'receipt_id': 'PR1',
                       'pump_id': 'pump-1', 'well_id': 'north-curb', 'outlet_id': 'outlet-1',
                       'from_at': T0, 'to_at': T1, 'volume_m3': 2.0,
                       'recorded_at': '2026-01-02T08:22:00Z'})
        self.send(db, {'id': 'bb3', 'kind': 'drain_balance', 'balance_id': 'BAL3',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:23:00Z'})
        balance = self.rows(db, 'drain_balance')[-1]
        self.assertEqual(balance['known_outflow_m3h'], 12.0)
        self.assertEqual({d['action'] for d in self.rows(db, 'drain_dispatch')
                          if d['balance_id'] == 'BAL3'}, {'retention'})
        receipt = next(r for r in self.rows(db, 'drain_pump_receipt') if r['id'] == 'PR1')
        self.assertEqual(receipt['volume_m3'], 2.0)
        self.assertEqual(receipt['rate_m3h'], 12.0)

    def test_radar_pixel_is_rejected_when_catchment_not_covered(self):
        db = self.fresh()
        self.base(db)
        self.send(db, {'id': 'br2', 'kind': 'drain_rainfall', 'rainfall_id': 'R2',
                       'catchment_id': 'culvert-c', 'well_id': 'north-curb',
                       'from_at': T0, 'to_at': T1, 'intensity_mh': 0.020,
                       'recorded_at': '2026-01-02T08:20:00Z'})
        rain = self.rows(db, 'drain_rainfall')[0]
        self.assertEqual(rain['state'], 'excluded')
        # The converted number is retained for the record but never admitted as inflow.
        self.assertEqual(rain['inflow_m3h'], 20.0)
        self.send(db, {'id': 'bb2', 'kind': 'drain_balance', 'balance_id': 'BAL2',
                       'well_id': 'north-curb', 'level_ids': ['DL1', 'DL2'],
                       'recorded_at': '2026-01-02T08:21:00Z'})
        self.assertEqual(self.rows(db, 'drain_balance')[-1]['known_inflow_m3h'], 0.0)

    def test_tampering_with_a_derived_balance_row_breaks_replay_audit(self):
        db = self.fresh()
        self.base(db)
        self.assertTrue(audit(db)['replay_ok'])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE drain_balance SET net_rate_m3h=99 WHERE id='BAL1'")
        conn.commit()
        conn.close()
        result = audit(db)
        self.assertFalse(result['replay_ok'])
        # Original source and event hashes are untouched; the derived ledger is
        # what the replay comparison exposes.
        self.assertTrue(result['source_integrity'])
        self.assertTrue(result['event_integrity'])


if __name__ == '__main__':
    unittest.main()
