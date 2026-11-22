import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report


ROOT = Path(__file__).resolve().parents[1]


class SignalDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'decisions.sqlite'
        self.counter = 0
        self.events = {}
        for file in (ROOT / 'fixtures' / 'demo').glob('*.json'):
            for event in json.loads(file.read_text(encoding='utf-8'))['events']:
                self.events[event['id']] = event

    def send(self, command, *events):
        self.counter += 1
        p = self.root / f'{self.counter}.json'
        p.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, command, p)

    def load(self, *ids):
        for group in ids:
            command = 'flow-import' if group[0] in 'ds' else 'plan-import' if group[0] == 'p' else 'field-import'
            self.send(command, *(deepcopy(self.events[k]) for k in group.split(',')))

    def flow(self):
        self.load('d1,s1,s2,s3,s4')

    def plans(self):
        self.flow()
        self.load('p1,p2')

    def active(self):
        self.plans()
        self.load('f1,f2')

    def test_demo_replay_and_rollback(self):
        self.active()
        self.send('field-import', self.events['fault-1'])
        self.send('flow-import', self.events['s5'])
        r = report(self.db)
        self.assertEqual(r['activation'][-1]['plan_id'], 'safe-base')
        self.assertEqual(r['rollback'][-1]['actor'], 'field-tech-3')
        self.assertEqual(r['rollback'][-1]['affected_from'], '2026-01-02T09:06:00Z')
        self.assertEqual(r['vehicle_sample'][-1]['reason'], 'field_fault_disrupted_flow_window')
        self.assertTrue(all(audit(self.db)[k] for k in ('replay_ok', 'source_integrity', 'event_integrity')))

    def test_detector_period_excludes_sample(self):
        d = deepcopy(self.events['d1']); d['valid_to'] = '2026-01-02T08:00:00Z'
        self.send('flow-import', d, self.events['s1'])
        self.assertEqual(report(self.db)['vehicle_sample'][0]['state'], 'excluded')

    def test_parallel_detector_revision_conflicts(self):
        self.send('flow-import', self.events['d1'])
        d = deepcopy(self.events['d1']); d.update(id='d2', approach='south')
        self.send('flow-import', d)
        self.assertEqual(report(self.db)['detector'][0]['state'], 'conflict')

    def test_duplicate_vehicle_across_sensors(self):
        self.send('flow-import', self.events['d1'], self.events['s1'])
        s = deepcopy(self.events['s1']); s.update(id='sx', sample_id='sx', detector_id='north-a')
        self.send('flow-import', s)
        self.assertEqual(report(self.db)['vehicle_sample'][-1]['reason'], 'vehicle_duplicate_across_detectors')

    def test_isolated_outlier_excluded(self):
        s = deepcopy(self.events['s1']); s['speed_kph'] = 190
        self.send('flow-import', self.events['d1'], s)
        self.assertEqual(report(self.db)['demand'][0]['valid_count'], 0)

    def test_late_flow_rechecks_held_unpublished_plan(self):
        self.send('flow-import', self.events['d1'], self.events['s1'])
        self.send('plan-import', self.events['p1'])
        self.assertEqual(report(self.db)['phase_plan'][0]['status'], 'held')
        self.send('flow-import', self.events['s2'])
        self.assertEqual(report(self.db)['phase_plan'][0]['reason'], 'late_flow_evidence')

    def test_revoke_changes_demand_but_keeps_published_plan(self):
        self.active()
        rev = {'id': 'revoke-1', 'kind': 'revoke', 'detector_id': 'north-a', 'effective_at': '2026-01-02T09:00:00Z'}
        self.send('flow-import', rev)
        r = report(self.db)
        self.assertEqual(next(d for d in r['demand'] if d['window_id'] == 'peak-2')['valid_count'], 0)
        self.assertEqual(r['phase_plan'][1]['status'], 'approved')
        self.assertEqual(r['activation'][1]['status'], 'active')

    def test_revoked_flow_holds_unpublished_approval(self):
        self.plans()
        rev = {'id': 'revoke-1', 'kind': 'revoke', 'detector_id': 'north-a', 'effective_at': '2026-01-02T08:00:00Z'}
        self.send('flow-import', rev)
        self.assertTrue(all(p['reason'] == 'flow_evidence_revoked' for p in report(self.db)['phase_plan']))

    def test_conflicting_green_blocks_plan(self):
        self.flow()
        p = deepcopy(self.events['p1']); p['phases']['green_groups'] = [['pedestrian', 'north_vehicle']]
        self.send('plan-import', p)
        self.assertEqual(report(self.db)['phase_plan'][0]['reason'], 'conflicting_green_phase')

    def test_pedestrian_clearance_blocks_plan(self):
        self.flow()
        p = deepcopy(self.events['p1']); p['ped_green_sec'] = 8
        self.send('plan-import', p)
        self.assertEqual(report(self.db)['phase_plan'][0]['reason'], 'pedestrian_clearance_short')

    def test_unauthorized_approver_blocks_plan(self):
        self.flow()
        p = deepcopy(self.events['p1']); p['approval_role'] = 'observer'
        self.send('plan-import', p)
        self.assertEqual(report(self.db)['phase_plan'][0]['reason'], 'invalid_approval')

    def test_parallel_plan_revision_is_rejected(self):
        self.plans()
        p = deepcopy(self.events['p1']); p['ped_green_sec'] = 13; p['id'] = 'parallel-p1'
        with self.assertRaisesRegex(ValueError, 'parallel_plan_revision'):
            self.send('plan-import', p)

    def test_plan_revision_history_survives_field_rollback(self):
        self.plans()
        self.send('field-import', self.events['f1'])
        revised = deepcopy(self.events['p1'])
        revised.update(id='p1-rev2', revision=2, ped_green_sec=13)
        self.send('plan-import', revised)
        self.assertEqual(report(self.db)['activation'][0]['plan_revision'], 1)
        f = deepcopy(self.events['f1'])
        f.update(id='f1-rev2', controller_id='ctrl-rev2', uploaded_at='2026-01-02T08:10:00Z',
                 readback_at='2026-01-02T08:11:00Z')
        f['readback']['ped_green_sec'] = 13
        self.send('field-import', f)
        self.send('field-import', self.events['fault-1'])
        r = report(self.db)
        self.assertEqual([x['revision'] for x in r['phase_plan_history'] if x['id'] == 'safe-base'], [1, 2])
        self.assertEqual(r['rollback'][-1]['restored_revision'], 1)

    def test_expired_approval_blocks_upload(self):
        self.plans()
        f = deepcopy(self.events['f1']); f['uploaded_at'] = '2026-01-03T08:05:00Z'
        with self.assertRaisesRegex(ValueError, 'approval_window_expired'):
            self.send('field-import', f)
        self.assertEqual(report(self.db)['controller'], [])

    def test_wrong_readback_preserves_safe_baseline(self):
        self.active()
        f = deepcopy(self.events['f2']); f.update(id='f3', controller_id='wrong-ctrl', actor='tech-8')
        f['readback']['ped_green_sec'] = 20
        self.send('field-import', f)
        r = report(self.db)
        self.assertEqual(r['controller'][-1]['status'], 'rejected')
        self.assertEqual(r['activation'][-1]['plan_id'], 'new-peak')
        self.assertEqual(r['rollback'][-1]['actor'], 'tech-8')

    def test_unconfirmed_upload_never_activates(self):
        self.plans()
        f = deepcopy(self.events['f1']); f['readback_at'] = '2026-01-02T08:04:00Z'
        self.send('field-import', f)
        self.assertEqual(report(self.db)['activation'], [])

    def test_upload_without_readback_is_rejected(self):
        self.plans()
        f = deepcopy(self.events['f1']); f.pop('readback')
        self.send('field-import', f)
        self.assertEqual(report(self.db)['controller'][0]['reason'], 'controller_version_or_timing_mismatch')
        self.assertEqual(report(self.db)['activation'], [])

    def test_fault_without_prior_plan_requires_manual_mode(self):
        self.plans()
        self.send('field-import', self.events['f1'])
        result = self.send('field-import', self.events['fault-1'])
        self.assertEqual(result['decisions'][0]['outcome'], 'needs_manual_safe_mode')

    def test_duplicate_source_is_idempotent(self):
        p = ROOT / 'fixtures' / 'demo' / '01-flow.json'
        first = ingest(self.db, 'flow-import', p)
        second = ingest(self.db, 'flow-import', p)
        self.assertFalse(first['duplicate']); self.assertTrue(second['duplicate'])
        self.assertEqual(audit(self.db)['events'], 8)

    def test_invalid_event_rolls_back_whole_source(self):
        bad = deepcopy(self.events['s1']); bad['detector_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'unknown_detector'):
            self.send('flow-import', self.events['d1'], bad)
        self.assertEqual(audit(self.db)['sources'], 0)

    def test_tampered_source_fails_integrity(self):
        self.flow()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE source_file SET raw=? WHERE id=1", (b'{"events":[]}',)); db.commit(); db.close()
        result = audit(self.db)
        self.assertFalse(result['source_integrity'])
        self.assertFalse(result['replay_ok'])


if __name__ == '__main__':
    unittest.main()
