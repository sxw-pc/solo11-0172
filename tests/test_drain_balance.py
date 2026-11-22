"""Drain attribution: well validity, conservation counter-examples and dispatch.

The tests deliberately keep the *same* well, the same two level originals and
the same provisional visit while changing only physical evidence.  A routine
solution that stops at ``inflow - outflow = 6 m3/h`` cannot tell world A from
world B; these tests pin the two feasible balances and the equipment each one
implies.
"""
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase import drain_balance
from signalphase.engine import audit, ingest, report

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'drain-balance.json'
DL1 = '2026-01-02T08:00:00Z'
DL2 = '2026-01-02T08:10:00Z'


def flow_event(event_id, direction, rate, *, outlet=None, start=DL1, end=DL2, total=True):
    event = {'id': event_id, 'kind': 'drain_flow', 'flow_id': event_id,
             'well_id': 'north-curb', 'direction': direction, 'source': 'field_meter',
             'from_at': start, 'to_at': end, 'rate_m3h': rate,
             'measures_total': total, 'recorded_at': end}
    if outlet:
        event['outlet_id'] = outlet
    return event


def rain_event(event_id, intensity, *, catchment='CB-ROOF', area=1000, loss=0.5,
               start=DL1, end=DL2):
    return {'id': event_id, 'kind': 'drain_rainfall', 'rain_id': event_id,
            'catchment_id': catchment, 'from_at': start, 'to_at': end,
            'intensity_mm_h': intensity, 'effective_area_m2': area,
            'loss_coefficient': loss, 'recorded_at': end}


def dispatch_event(event_id, dispatch_id, revision, *, executed=False):
    event = {'id': event_id, 'kind': 'drain_dispatch', 'dispatch_id': dispatch_id,
             'revision': revision, 'well_id': 'north-curb',
             'level_from': 'DL1', 'level_to': 'DL2', 'recorded_at': DL2}
    if executed:
        event['executed_clearing'] = True
    return event


def work_event(event_id, kind, *, hours=1.0, volume=0.0, dispatch='JOB', revision=1):
    return {'id': event_id, 'kind': 'drain_work', 'work_id': event_id,
            'well_id': 'north-curb', 'work_kind': kind, 'at': '2026-01-02T08:30:00Z',
            'crew': 'crew-1', 'hours': hours, 'volume_m3': volume,
            'dispatch_id': dispatch, 'dispatch_revision': revision,
            'recorded_at': '2026-01-02T08:31:00Z'}


class DrainBalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'drain.sqlite'
        self.counter = 0

    def send(self, *events):
        self.counter += 1
        path = self.root / f'{self.counter}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, 'drain-import', path)

    def send_to(self, db, *events):
        self.counter += 1
        path = self.root / f'{self.counter}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(db, 'drain-import', path)

    def base(self):
        return self.send(*deepcopy(json.loads(FIXTURE.read_text(encoding='utf-8'))['events']))

    def rows(self, table):
        return report(self.db)[table]

    def explain(self):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        try:
            return drain_balance.explain(db, 'north-curb', 'DL1', 'DL2')
        finally:
            db.close()

    # --- well validity -------------------------------------------------
    def test_valid_level_still_produces_provisional_visit(self):
        self.base()
        self.assertEqual(self.rows('drain_level')[0]['state'], 'captured')
        self.assertEqual(self.rows('drain_visit')[0]['state'], 'inspect')

    def test_expired_well_moves_visit_from_inspect_to_held(self):
        original = json.loads((ROOT / 'fixtures' / 'drain-ok.json').read_text(encoding='utf-8'))
        self.send(*deepcopy(original['events']))
        self.assertEqual(self.rows('drain_visit')[0]['state'], 'inspect')

        other = self.root / 'expired.sqlite'
        expired = deepcopy(original)
        expired['events'][0]['valid_to'] = '2026-01-02T00:00:00Z'
        path = self.root / 'expired.json'
        path.write_text(json.dumps(expired, ensure_ascii=False), encoding='utf-8')
        ingest(other, 'drain-import', path)
        result = report(other)
        self.assertEqual(result['drain_level'][0]['state'], 'excluded')
        self.assertEqual(result['drain_visit'][0]['state'], 'held')
        self.assertEqual(result['drain_visit'][0]['reason'], 'level_not_valid')
        self.assertTrue(audit(other)['replay_ok'])

    def test_negative_level_is_excluded(self):
        self.send(dict(deepcopy(json.loads(FIXTURE.read_text(encoding='utf-8'))['events'][0])),
                  {'id': 'neg', 'kind': 'drain_level', 'level_id': 'NEG',
                   'well_id': 'north-curb', 'observed_at': DL1, 'depth_m': -0.1,
                   'recorded_at': DL1})
        self.assertEqual(self.rows('drain_level')[0]['state'], 'excluded')

    # --- conservation counter-examples ---------------------------------
    def test_net_rate_alone_leaves_two_feasible_balances(self):
        self.base()
        explanation = self.explain()
        self.assertEqual(explanation['delta_v_m3'], 1.0)
        self.assertEqual(explanation['net_m3h'], 6.0)
        pairs = [(c['inflow_m3h'], c['outflow_m3h']) for c in explanation['candidates']]
        self.assertEqual(pairs, [(6.0, 0.0), (18.0, 12.0)])
        for candidate in explanation['candidates']:
            self.assertAlmostEqual(candidate['inflow_m3'] - candidate['outflow_m3'], 1.0)
            self.assertEqual(candidate['residual_m3'], 0.0)
            self.assertEqual(candidate['storage_m3'], 1.0)

    def test_same_water_rise_different_equipment(self):
        base_events = json.loads(FIXTURE.read_text(encoding='utf-8'))['events']
        # World A: the outlet carries 12 m3/h, so inflow is 18 and the rise is
        # upstream rain, not a blockage.
        self.send(*deepcopy(base_events))
        self.send(flow_event('FA', 'outflow', 12, outlet='OUT1'),
                  dispatch_event('DA', 'JOB-A', 1))
        a = self.rows('drain_dispatch')[0]
        self.assertEqual(a['action'], 'detain_upstream')
        self.assertEqual(a['status'], 'confirmed')
        self.assertIn('temporary_storage', a['equipment_json'])
        self.assertIn('jet_clearing_truck', a['withdraw_json'])

        # World B: the same rise with a measured zero outlet flow is a blockage.
        other = self.root / 'world-b.sqlite'
        ingest(other, 'drain-import', self.root / '1.json')
        self.send_to(other, flow_event('FB', 'outflow', 0, outlet='OUT1'),
                     dispatch_event('DB', 'JOB-B', 1))
        b = report(other)['drain_dispatch'][0]
        self.assertEqual(b['action'], 'clear_outlet_and_pump')
        self.assertEqual(b['status'], 'confirmed')
        self.assertIn('clearing_crew', b['equipment_json'])
        self.assertIn('portable_pump', b['equipment_json'])
        self.assertEqual(report(other)['drain_visit'][0]['state'], 'inspect')

    def test_route_outlet_meter_and_route_radar_differ_in_scope(self):
        base_events = json.loads(FIXTURE.read_text(encoding='utf-8'))['events']
        self.send(*deepcopy(base_events))
        self.send(flow_event('FA', 'outflow', 12, outlet='OUT1'),
                  dispatch_event('DA', 'JOB-A', 1))
        a_scope = json.loads(self.rows('drain_dispatch')[0]['scope_json'])
        self.assertEqual(a_scope['outlets_measured'], ['OUT1'])
        self.assertEqual(a_scope['catchments_covered'], [])
        self.assertEqual(set(a_scope['catchments_uncovered']), {'CB-ROOF', 'CB-TUNNEL'})
        # Upstream storage is needed, but the sub-basin that feeds the 18
        # m3/h cannot be located from a single outlet meter.
        self.assertEqual(set(a_scope['storage_areas']), {'CB-ROOF', 'CB-TUNNEL'})
        balance = self.rows('drain_balance')[0]
        self.assertEqual((balance['inflow_m3h'], balance['outflow_m3h']), (18.0, 12.0))
        gap_kinds = {gap['kind'] for gap in self.rows('drain_gap')}
        self.assertIn('inflow_unattributed', gap_kinds)

        # Route B starts from the identical levels and the identical visit.
        other = self.root / 'route-b.sqlite'
        ingest(other, 'drain-import', self.root / '1.json')
        self.send_to(other, rain_event('RB', 12), dispatch_event('DB', 'JOB-B', 1))
        result = report(other)
        b = result['drain_dispatch'][0]
        b_scope = json.loads(b['scope_json'])
        self.assertEqual(b['action'], 'inspect_and_pump')
        self.assertEqual(b['status'], 'provisional')
        self.assertIn('portable_pump', b['equipment_json'])
        self.assertEqual(b_scope['catchments_covered'], ['CB-ROOF'])
        self.assertEqual(b_scope['catchments_uncovered'], ['CB-TUNNEL'])
        self.assertEqual(b_scope['outlets_measured'], [])
        self.assertEqual(b_scope['outlets_unmeasured'], ['OUT1'])
        self.assertNotEqual(a_scope['catchments_uncovered'], b_scope['catchments_uncovered'])
        self.assertEqual(result['drain_visit'][0]['state'], 'inspect')

    def test_radar_rain_is_converted_by_area_and_loss(self):
        self.base()
        self.send(rain_event('RB', 12))
        rain = self.rows('drain_rainfall')[0]
        self.assertEqual(rain['rate_m3h'], 6.0)
        self.assertEqual(self.explain()['inflow_bounds']['lower_m3h'], 6.0)
        with self.assertRaisesRegex(ValueError, 'invalid_rainfall_evidence'):
            self.send(rain_event('BAD', 12, area=5000))
        with self.assertRaisesRegex(ValueError, 'invalid_rainfall_evidence'):
            self.send(rain_event('BAD2', 12, loss=1.5))

    def test_partial_window_flow_is_not_spliced_into_the_ten_minutes(self):
        self.base()
        # A peak measured only from 08:05 to 08:06 cannot explain 08:00-08:10.
        self.send(flow_event('FRAG', 'outflow', 12, outlet='OUT1',
                             start='2026-01-02T08:05:00Z', end='2026-01-02T08:06:00Z'),
                  dispatch_event('D', 'JOB-FRAG', 1))
        explanation = self.explain()
        self.assertEqual(explanation['outflow_bounds']['high_m3h'], 12.0)
        self.assertEqual([c['measured_outflow'] for c in explanation['candidates']], [False, False])
        kinds = [gap['kind'] for gap in self.rows('drain_gap')]
        self.assertIn('unusable_flow_window', kinds)
        dispatch = self.rows('drain_dispatch')[0]
        self.assertEqual(dispatch['action'], 'inspect_and_pump')

    def test_every_candidate_uses_the_same_ten_minute_interval(self):
        self.base()
        self.send(rain_event('RB', 12))
        explanation = self.explain()
        for candidate in explanation['candidates']:
            self.assertAlmostEqual(candidate['inflow_m3h'] - candidate['outflow_m3h'], 6.0)
            self.assertAlmostEqual(candidate['inflow_m3'] - candidate['outflow_m3'], 1.0)
            self.assertEqual(candidate['storage_m3'], 1.0)
        rates = [c['outflow_m3h'] for c in explanation['candidates']]
        self.assertEqual(rates, [0.0, 12.0])

    def test_pump_power_is_not_discharge(self):
        self.base()
        pump = self.rows('drain_pump')[0]
        self.assertEqual(pump['powered'], 1)
        explanation = self.explain()
        self.assertEqual([(c['inflow_m3h'], c['outflow_m3h']) for c in explanation['candidates']],
                         [(6.0, 0.0), (18.0, 12.0)])
        self.assertEqual(self.rows('drain_flow'), [])
        self.assertFalse(any(c['measured_outflow'] for c in explanation['candidates']))

    # --- dispatch revisions and executed work ---------------------------
    def test_late_flow_evidence_adds_revision_without_rewriting_work(self):
        self.base()
        self.send(dispatch_event('D1', 'JOB', 1),
                  work_event('W1', 'arrival', hours=0.5),
                  work_event('W2', 'clearing', hours=1.0, volume=4))
        self.assertEqual(self.rows('drain_dispatch')[0]['action'], 'inspect_and_pump')

        self.send(flow_event('FLATE', 'outflow', 12, outlet='OUT1'),
                  dispatch_event('D2', 'JOB', 2))
        dispatches = self.rows('drain_dispatch')
        self.assertEqual([d['revision'] for d in dispatches], [1, 2])
        self.assertEqual(dispatches[0]['action'], 'inspect_and_pump')
        self.assertEqual(dispatches[1]['action'], 'detain_upstream')
        # The executed clearing was already done; the new revision may not
        # pretend it can be withdrawn.
        self.assertEqual(json.loads(dispatches[1]['withdraw_json']), [])
        work = {row['work_id']: row for row in self.rows('drain_work')}
        self.assertEqual(work['W1']['hours'], 0.5)
        self.assertEqual(work['W2']['hours'], 1.0)
        self.assertEqual(work['W2']['volume_m3'], 4.0)
        self.assertEqual(work['W2']['dispatch_revision'], 1)
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_dispatch_revision_must_be_monotonic(self):
        self.base()
        self.send(dispatch_event('D1', 'JOB', 2))
        with self.assertRaisesRegex(ValueError, 'dispatch_revision_not_monotonic'):
            self.send(dispatch_event('D2', 'JOB', 1))
        with self.assertRaisesRegex(ValueError, 'dispatch_revision_not_monotonic'):
            self.send(dispatch_event('D3', 'JOB', 2))

    def test_redispatch_appends_revision_from_late_evidence(self):
        self.base()
        self.send(dispatch_event('D1', 'JOB', 1))
        self.send(flow_event('FLATE', 'outflow', 12, outlet='OUT1'))
        result = self.send({'id': 'R1', 'kind': 'drain_redispatch', 'dispatch_id': 'JOB',
                            'well_id': 'north-curb', 'recorded_at': DL2})
        self.assertEqual(result['decisions'][0]['outcome'], 'revised:2:detain_upstream')
        dispatches = self.rows('drain_dispatch')
        self.assertEqual([d['revision'] for d in dispatches], [1, 2])
        self.assertEqual(dispatches[0]['action'], 'inspect_and_pump')
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_unverified_outlet_then_zero_outflow_switches_to_clearing(self):
        # Route B dispatch: pipe inspection and a portable pump, not a
        # confirmed blockage.  Crew arrives and signs the arrival record.
        self.base()
        self.send(rain_event('RB', 12), dispatch_event('D1', 'JOB', 1),
                  work_event('W-ARRIVE', 'arrival', hours=0.5))
        first = self.rows('drain_dispatch')[0]
        self.assertEqual(first['action'], 'inspect_and_pump')
        self.assertEqual(first['status'], 'provisional')

        # The field check later reads zero at the outlet: a new work order is
        # appended for clearing, while the arrival record is untouched.
        self.send(flow_event('FB', 'outflow', 0, outlet='OUT1'))
        result = self.send({'id': 'R1', 'kind': 'drain_redispatch', 'dispatch_id': 'JOB',
                            'well_id': 'north-curb', 'recorded_at': DL2})
        self.assertEqual(result['decisions'][0]['outcome'], 'revised:2:clear_outlet_and_pump')
        dispatches = self.rows('drain_dispatch')
        self.assertEqual(dispatches[1]['action'], 'clear_outlet_and_pump')
        self.assertEqual(dispatches[1]['status'], 'confirmed')
        self.assertIn('clearing_crew', dispatches[1]['equipment_json'])
        arrival = self.rows('drain_work')[0]
        self.assertEqual((arrival['work_id'], arrival['work_kind'], arrival['hours']),
                         ('W-ARRIVE', 'arrival', 0.5))
        self.assertEqual(arrival['dispatch_revision'], 1)
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_redispatch_requires_existing_dispatch(self):
        self.base()
        with self.assertRaisesRegex(ValueError, 'unknown_drain_dispatch'):
            self.send({'id': 'R1', 'kind': 'drain_redispatch', 'dispatch_id': 'MISSING',
                       'well_id': 'north-curb', 'recorded_at': DL2})

    def test_mid_interval_level_with_different_net_rate_is_not_spliced(self):
        self.base()
        # 08:05 shows 1.22 m: the first five minutes rose 0.02 m (2.4 m3/h net)
        # while the full ten minutes rose 0.10 m (6 m3/h net).  No single
        # inflow/outflow pair explains both, so nothing may be dispatched.
        self.send({'id': 'DL-MID', 'kind': 'drain_level', 'level_id': 'DLMID',
                   'well_id': 'north-curb', 'observed_at': '2026-01-02T08:05:00Z',
                   'depth_m': 1.22, 'recorded_at': '2026-01-02T08:06:00Z'})
        explanation = self.explain()
        self.assertEqual(explanation['candidates'], [])
        kinds = {gap['kind'] for gap in explanation['shared_gaps']}
        self.assertIn('trajectory_not_integrated', kinds)
        self.send(dispatch_event('D1', 'JOB', 1))
        dispatch = self.rows('drain_dispatch')[0]
        self.assertEqual(dispatch['action'], 'inspect_and_pump')
        self.assertEqual(dispatch['status'], 'unsolved')

    def test_metered_inflow_that_breaks_capacity_emits_no_candidate(self):
        self.base()
        # 100 m3/h in with a 12 m3/h outlet and +6 m3/h storage requires
        # 94 m3/h out; no outlet can carry it, so nothing may be dispatched.
        self.send(flow_event('FIN', 'inflow', 100))
        explanation = self.explain()
        self.assertEqual(explanation['candidates'], [])
        kinds = {gap['kind'] for gap in explanation['shared_gaps']}
        self.assertIn('conservation_inconsistent', kinds)
        self.send(dispatch_event('D1', 'JOB', 1))
        dispatch = self.rows('drain_dispatch')[0]
        self.assertEqual(dispatch['action'], 'inspect_and_pump')
        self.assertEqual(dispatch['status'], 'unsolved')

    def test_consistent_intermediate_level_keeps_both_balances(self):
        self.base()
        # 08:05 at 1.25 m is exactly the 6 m3/h net rate of the full interval.
        self.send({'id': 'DL-MID', 'kind': 'drain_level', 'level_id': 'DLMID',
                   'well_id': 'north-curb', 'observed_at': '2026-01-02T08:05:00Z',
                   'depth_m': 1.25, 'recorded_at': '2026-01-02T08:06:00Z'})
        explanation = self.explain()
        self.assertEqual([(c['inflow_m3h'], c['outflow_m3h']) for c in explanation['candidates']],
                         [(6.0, 0.0), (18.0, 12.0)])
        self.assertNotIn('trajectory_not_integrated',
                         {gap['kind'] for gap in explanation['shared_gaps']})

    # --- audit ----------------------------------------------------------
    def test_tampered_balance_row_fails_replay(self):
        self.base()
        self.send(dispatch_event('D1', 'JOB', 1))
        self.assertTrue(audit(self.db)['replay_ok'])
        db = sqlite3.connect(self.db)
        db.execute("UPDATE drain_balance SET outflow_m3h=99 WHERE candidate=1")
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertTrue(result['source_integrity'])
        self.assertFalse(result['replay_ok'])

    def test_tampered_source_fails_integrity(self):
        self.base()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE source_file SET raw=? WHERE id=1", (b'{"events":[]}',))
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertFalse(result['source_integrity'])
        self.assertFalse(result['replay_ok'])

    def test_tampered_work_hours_fails_replay(self):
        self.base()
        self.send(dispatch_event('D1', 'JOB', 1),
                  work_event('W1', 'clearing', hours=1.0, volume=4))
        db = sqlite3.connect(self.db)
        db.execute("UPDATE drain_work SET hours=0.25 WHERE work_id='W1'")
        db.commit()
        db.close()
        self.assertFalse(audit(self.db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
