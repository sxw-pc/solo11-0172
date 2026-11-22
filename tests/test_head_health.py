import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report
from signalphase.head_health import camera_interval, joint_feasibility

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'

VALUES = [1] * 100


def head_events(controller='ctrl-base'):
    return [
        {'id': 'hs-r1', 'kind': 'head_scan', 'scan_id': 'HS-R1', 'controller_id': controller,
         'head_id': 'R1', 'observed_at': '2026-01-02T08:06:30Z', 'sample_rate_hz': 100,
         'exposure_us': 1000, 'shutter_window_us': 6000, 'values': VALUES[:],
         'recorded_at': '2026-01-02T08:08:00Z'},
        {'id': 'hs-r2', 'kind': 'head_scan', 'scan_id': 'HS-R2', 'controller_id': controller,
         'head_id': 'R2', 'observed_at': '2026-01-02T08:06:30.005Z', 'sample_rate_hz': 100,
         'exposure_us': 1000, 'shutter_window_us': 6000, 'values': VALUES[:],
         'recorded_at': '2026-01-02T08:08:00Z'},
    ]


def group_event(**over):
    e = {'id': 'pg-pair', 'kind': 'power_group', 'group_id': 'PG-RED-PAIR', 'phase_id': 'red-1',
         'controller_id': 'ctrl-base', 'observed_from': '2026-01-02T08:06:30Z',
         'observed_to': '2026-01-02T08:07:30Z', 'budget': 1.0, 'required_luminance': 0.6,
         'power_to_luminance': 1.0, 'sync_skew_us': 5000, 'period_us': 10000,
         'clock_ref': 'cabinet-clock-1',
         'heads': [{'scan_id': 'HS-R1', 'head_id': 'R1'},
                   {'scan_id': 'HS-R2', 'head_id': 'R2'}],
         'recorded_at': '2026-01-02T08:09:00Z'}
    e.update(over)
    return e


def current_event(world, **over):
    if world == 'A':
        r1, r2 = 0.7, 0.3
    else:
        r1, r2 = 0.3, 0.7
    e = {'id': f'cur-{world}', 'kind': 'current_record', 'group_id': 'PG-RED-PAIR',
         'window_from': '2026-01-02T08:06:30Z', 'window_to': '2026-01-02T08:07:30Z',
         'calibrated_clock': 'cabinet-clock-1', 'clock_offset_us': 0, 'complete': True,
         'branches': [{'reading_id': f'br-{world}-1', 'branch_id': 'b1', 'head_id': 'R1',
                       'mean_current': r1},
                      {'reading_id': f'br-{world}-2', 'branch_id': 'b2', 'head_id': 'R2',
                       'mean_current': r2}],
         'recorded_at': '2026-01-02T10:00:00Z'}
    e.update(over)
    return e


def gap_event(**over):
    e = {'id': 'gap-1', 'kind': 'current_gap', 'group_id': 'PG-RED-PAIR',
         'window_from': '2026-01-02T08:06:30Z', 'window_to': '2026-01-02T08:07:30Z',
         'temp_head_id': 'TEMP-R', 'installed_at': '2026-01-02T10:30:00Z',
         'clamps': [{'clamp_id': 'CL-1', 'branch_id': 'b1', 'head_id': 'R1'},
                    {'clamp_id': 'CL-2', 'branch_id': 'b2', 'head_id': 'R2'}],
         'recorded_at': '2026-01-02T10:30:00Z'}
    e.update(over)
    return e


class HeadHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'h.sqlite'
        self.counter = 0

    def send(self, command, *events):
        self.counter += 1
        p = Path(self.temp.name) / f'{self.counter}.json'
        p.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, command, p)

    def base(self, field=True):
        self.send('flow-import', *json.loads((FIX / 'demo/01-flow.json').read_text())['events'])
        self.send('plan-import', *json.loads((FIX / 'demo/02-plan.json').read_text())['events'])
        if field:
            self.send('field-import', *json.loads((FIX / 'demo/03-field.json').read_text())['events'])
        self.send('head-import', *head_events())
        self.send('head-import', group_event())

    def latest_group_verdict(self, group_id='PG-RED-PAIR'):
        rep = report(self.db)
        rev = max(v['revision'] for v in rep['health_verdict'] if v['group_id'] == group_id)
        return rev, [v for v in rep['health_verdict']
                     if v['group_id'] == group_id and v['revision'] == rev]

    # ---- HS1 readback attribution (headscan.py / field.py contract) ----
    def test_hs1_attributed_to_safe_base_revision_one_with_field_readback(self):
        self.send('flow-import', *json.loads((FIX / 'demo/01-flow.json').read_text())['events'])
        self.send('plan-import', *json.loads((FIX / 'demo/02-plan.json').read_text())['events'])
        self.send('field-import', *json.loads((FIX / 'demo/03-field.json').read_text())['events'])
        self.send('head-import', *json.loads((FIX / 'head-scan-ok.json').read_text())['events'])
        row = report(self.db)['head_scan'][0]
        self.assertEqual((row['state'], row['plan_id'], row['plan_revision'],
                          row['sample_rate_hz'], row['exposure_us']),
                         ('attributed', 'safe-base', 1, 100, 1000))
        self.assertEqual(json.loads(row['values_json']), [0, 1, 0, 1, 0, 1, 0, 1])

    def test_hs1_unattributed_without_field_readback(self):
        self.send('flow-import', *json.loads((FIX / 'demo/01-flow.json').read_text())['events'])
        self.send('plan-import', *json.loads((FIX / 'demo/02-plan.json').read_text())['events'])
        self.send('head-import', *json.loads((FIX / 'head-scan-ok.json').read_text())['events'])
        row = report(self.db)['head_scan'][0]
        self.assertEqual((row['state'], row['plan_id'], row['plan_revision']),
                         ('unattributed', None, None))

    def test_capture_persists_shutter_window_and_frame_values(self):
        self.base(field=True)
        rows = report(self.db)['head_scan']
        self.assertEqual({(r['head_id'], r['shutter_window_us'], r['exposure_us'],
                           r['sample_rate_hz']) for r in rows},
                         {('R1', 6000, 1000, 100), ('R2', 6000, 1000, 100)})
        for r in rows:
            self.assertEqual(json.loads(r['values_json']), VALUES)

    def test_power_group_without_readback_is_unattributed_but_still_verdict(self):
        self.base(field=False)
        group = report(self.db)['power_group'][0]
        self.assertEqual((group['state'], group['plan_id'], group['plan_revision']),
                         ('unattributed', None, None))

    # ---- per-head interval vs joint group conclusion ----
    def test_camera_interval_contains_world_levels_for_each_single_head(self):
        lo, hi = camera_interval({'values_json': json.dumps(VALUES),
                                  'exposure_us': 1000, 'shutter_window_us': 6000})
        self.assertAlmostEqual(lo, 1 / 6)
        self.assertEqual(hi, 6.0)
        # Both world levels (0.7 healthy and 0.3 dim) are individually possible.
        self.assertLessEqual(lo, 0.3)
        self.assertGreaterEqual(hi, 0.7)

    def test_joint_set_proves_at_least_one_dim_while_no_single_head_is_named(self):
        self.base()
        rev, verdicts = self.latest_group_verdict()
        heads = {v['head_id']: v for v in verdicts if v['head_id'] != '*group*'}
        for hid in ('R1', 'R2'):
            self.assertEqual(heads[hid]['status'], 'possible_dim')
            self.assertTrue(heads[hid]['max_luminance'] >= 0.7)
            self.assertIsNone(heads[hid]['work_order'])
        group = next(v for v in verdicts if v['head_id'] == '*group*')
        self.assertEqual((group['status'], group['min_dim_heads'], group['group_healthy']),
                         ('group_at_least_one_dim', 1, 0))
        self.assertTrue(group['work_order'].startswith('group-pending:'))
        # Proof: two heads at 0.6 need 1.2 against a shared 1.0 budget.
        with self.assertRaises(Exception):
            joint_feasibility([('R1', 0.6, 6.0), ('R2', 0.6, 6.0)], 1.0, 0.6)

    def test_two_healthy_heads_violate_single_shared_budget_not_per_head_budgets(self):
        # Per-head budgeting would allow each head its own 1.0; the joint set
        # must apply one budget to both heads across all frames.
        analysis = joint_feasibility([('R1', 1 / 6, 6.0), ('R2', 1 / 6, 6.0)], 1.0, 0.6)
        self.assertEqual(analysis['min_dim_heads'], 1)
        # If each head wrongly got its own 1.0 budget, min dim would be 0.
        self.assertEqual(joint_feasibility([('R1', 1 / 6, 6.0)], 1.0, 0.6)['min_dim_heads'], 0)

    def test_group_verdict_closes_lane_and_registers_group_pending_ticket(self):
        self.base()
        actions = report(self.db)['control_action']
        kinds = {a['action'] for a in actions}
        self.assertIn('close_lane', kinds)
        self.assertIn('group_pending_ticket', kinds)
        self.assertNotIn('head_work_order', kinds)

    # ---- worlds A and B share every frame but differ in the dim head ----
    def test_worlds_a_and_b_share_frames_but_swap_the_dim_head(self):
        for world, dim, healthy in (('A', 'R2', 'R1'), ('B', 'R1', 'R2')):
            with self.subTest(world=world):
                self.setUp()
                self.base()
                self.send('head-import', current_event(world))
                _, verdicts = self.latest_group_verdict()
                status = {v['head_id']: v['status'] for v in verdicts if v['head_id'] != '*group*'}
                self.assertEqual(status[dim], 'confirmed_dim')
                self.assertEqual(status[healthy], 'confirmed_healthy')
                tickets = {v['head_id']: v['work_order'] for v in verdicts
                           if v['work_order'] and v['head_id'] != '*group*'}
                self.assertEqual(set(tickets), {dim})

    # ---- route A: recovered calibrated historical currents ----
    def test_route_a_reissues_specific_head_ticket_but_keeps_closure_history(self):
        self.base()
        self.send('head-import', current_event('A'))
        rep = report(self.db)
        # Revision 1 (camera-only) remains on record, revision 2 locates R2.
        revs = {(v['revision'], v['head_id']): v for v in rep['health_verdict']}
        self.assertEqual(revs[(1, '*group*')]['status'], 'group_at_least_one_dim')
        self.assertEqual(revs[(2, 'R2')]['status'], 'confirmed_dim')
        self.assertEqual(revs[(2, 'R1')]['status'], 'confirmed_healthy')
        self.assertTrue(revs[(1, '*group*')]['superseded'] == 0
                        or revs[(2, '*group*')]['superseded'] == 0)
        actions = [a['action'] for a in rep['control_action']]
        # The executed closure is never erased; targeted re-cut sits beside it.
        self.assertIn('close_lane', actions)
        self.assertIn('targeted_historical_work_order', actions)
        self.assertIn('lane_reopen_after_targeted_repair', actions)

    def test_route_a_requires_common_calibrated_clock_and_complete_branches(self):
        self.base()
        bad_clock = current_event('A', calibrated_clock='uncalibrated-field-watch')
        with self.assertRaisesRegex(ValueError, 'common_clock_not_calibrated'):
            self.send('head-import', bad_clock)
        incomplete = current_event('A', branches=[current_event('A')['branches'][0]])
        with self.assertRaisesRegex(ValueError, 'incomplete_branch_records'):
            self.send('head-import', incomplete)
        outside = current_event('A', window_from='2026-01-03T00:00:00Z',
                                window_to='2026-01-03T00:01:00Z')
        with self.assertRaisesRegex(ValueError, 'reading_outside_historical_window'):
            self.send('head-import', outside)

    # ---- route B: missing pages, temporary light, forward-only clamps ----
    def test_route_b_keeps_old_window_unlocated_and_clamps_are_forward_only(self):
        self.base()
        self.send('head-import', gap_event())
        rep = report(self.db)
        actions = [a['action'] for a in rep['control_action']]
        self.assertIn('close_lane_retained', actions)
        self.assertIn('temporary_red_installed', actions)
        self.assertEqual({'current_clamp_installed'},
                         {a['action'] for a in rep['control_action'] if 'clamp' in a['action']})
        # No new verdict names a head for the historical window.
        for v in rep['health_verdict']:
            if v['head_id'] != '*group*':
                self.assertEqual(v['status'], 'possible_dim')
        # A clamp cannot backfill the old window.
        with self.assertRaisesRegex(ValueError, 'clamp_cannot_backfill_historical_window'):
            self.send('head-import', {
                'id': 'cl-old', 'kind': 'clamp_reading', 'clamp_id': 'CL-1',
                'head_id': 'R1', 'mean_current': 0.3,
                'window_from': '2026-01-02T08:06:30Z',
                'window_to': '2026-01-02T08:07:30Z',
                'recorded_at': '2026-01-02T11:00:00Z'})

    def test_route_b_clamp_locates_only_future_window(self):
        self.base()
        self.send('head-import', gap_event())
        self.send('head-import', {
            'id': 'cl-new', 'kind': 'clamp_reading', 'clamp_id': 'CL-1',
            'head_id': 'R1', 'mean_current': 0.3,
            'window_from': '2026-01-02T10:30:00Z', 'window_to': '2026-01-02T11:30:00Z',
            'recorded_at': '2026-01-02T11:31:00Z'})
        rep = report(self.db)
        future = [v for v in rep['health_verdict']
                  if v['window_from'] == '2026-01-02T10:30:00Z']
        self.assertEqual(next(v for v in future if v['head_id'] == 'R1')['status'], 'confirmed_dim')
        historical = [v for v in rep['health_verdict'] if v['head_id'] == 'R1' and v['revision'] == 1]
        self.assertEqual(historical[0]['status'], 'possible_dim')
        self.assertIn('future_window_attribution',
                      [a['action'] for a in rep['control_action']])

    def test_temporary_light_luminance_is_not_counted_in_original_group(self):
        self.base()
        self.send('head-import', gap_event())
        rep = report(self.db)
        temp = next(a for a in rep['control_action'] if a['action'] == 'temporary_red_installed')
        self.assertEqual(temp['scope'], 'temp')
        self.assertEqual(temp['head_id'], 'TEMP-R')
        group_heads = set(json.loads(rep['power_group'][0]['head_ids_json']))
        self.assertNotIn('TEMP-R', group_heads)

    # ---- late optical component / clock correction / fault rollback ----
    def test_late_optical_component_reevaluates_under_original_field_revision(self):
        self.base()
        # A late optical correction widens the brightness window; the verdict
        # must still bind to the safe-base revision 1 activation at capture time.
        corrected = head_events()
        for e in corrected:
            e['id'] = e['id'] + '-late'
            e['scan_id'] = e['scan_id'] + '-LATE'
            e['shutter_window_us'] = 10000
            e['recorded_at'] = '2026-01-02T12:00:00Z'
        self.send('head-import', *corrected)
        g = group_event(id='pg-late', group_id='PG-LATE',
                        heads=[{'scan_id': 'HS-R1-LATE', 'head_id': 'R1'},
                               {'scan_id': 'HS-R2-LATE', 'head_id': 'R2'}],
                        recorded_at='2026-01-02T12:05:00Z')
        self.send('head-import', g)
        row = report(self.db)['power_group']
        late = next(x for x in row if x['id'] == 'PG-LATE')
        self.assertEqual((late['plan_id'], late['plan_revision']), ('safe-base', 1))

    def test_fault_rollback_recomputes_against_original_approved_revision(self):
        self.base()
        # fault-1 rolls the controller back to the previously verified safe-base.
        self.send('field-import', {'id': 'fault-1', 'kind': 'fault',
                                   'at': '2026-01-02T09:20:00Z', 'window_id': 'peak-2',
                                   'actor': 'field-tech-3',
                                   'recorded_at': '2026-01-02T09:21:00Z'})
        rollback = report(self.db)['rollback'][-1]
        self.assertEqual((rollback['restored_plan'], rollback['restored_revision']),
                         ('safe-base', 1))
        # A post-rollback scan still attributes to the original approved version.
        post = {'id': 'hs-post', 'kind': 'head_scan', 'scan_id': 'HS-POST',
                'controller_id': 'ctrl-base', 'head_id': 'R1',
                'observed_at': '2026-01-02T09:30:00Z', 'sample_rate_hz': 100,
                'exposure_us': 1000, 'shutter_window_us': 6000, 'values': [1] * 50,
                'recorded_at': '2026-01-02T09:31:00Z'}
        self.send('head-import', post)
        self.assertEqual(report(self.db)['head_scan'][-1]['plan_revision'], 1)

    # ---- audit tamper detection over the new tables ----
    def test_audit_replay_covers_head_health_tables(self):
        self.base()
        self.send('head-import', current_event('A'))
        result = audit(self.db)
        self.assertTrue(all(result[k] for k in
                            ('replay_ok', 'source_integrity', 'event_integrity')))

    def test_tampered_group_verdict_is_detected_by_audit(self):
        self.base()
        raw = sqlite3.connect(self.db)
        raw.execute("UPDATE health_verdict SET min_dim_heads=0 WHERE head_id='*group*'")
        raw.commit()
        raw.close()
        result = audit(self.db)
        self.assertFalse(result['replay_ok'])
        self.assertTrue(result['source_integrity'])

    def test_tampered_control_action_cannot_hide_executed_closure(self):
        self.base()
        raw = sqlite3.connect(self.db)
        raw.execute("DELETE FROM control_action WHERE action='close_lane'")
        raw.commit()
        raw.close()
        self.assertFalse(audit(self.db)['replay_ok'])

    def test_duplicate_head_power_fixture_is_idempotent(self):
        self.send('flow-import', *json.loads((FIX / 'demo/01-flow.json').read_text())['events'])
        self.send('plan-import', *json.loads((FIX / 'demo/02-plan.json').read_text())['events'])
        self.send('field-import', *json.loads((FIX / 'demo/03-field.json').read_text())['events'])
        first = ingest(self.db, 'head-import', FIX / 'head-power-pair.json')
        second = ingest(self.db, 'head-import', FIX / 'head-power-pair.json')
        self.assertTrue(second['duplicate'])
        self.assertTrue(audit(self.db)['replay_ok'])
        # The attempted duplicate did not double-register the group.
        self.assertEqual(len(report(self.db)['power_group']), 1)
        self.assertFalse(first['duplicate'])


if __name__ == '__main__':
    unittest.main()
