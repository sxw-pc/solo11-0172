import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report


ROOT = Path(__file__).resolve().parents[1]


class CorridorCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.db = self.dir / 'corridor.sqlite'
        self.counter = 0
        self.events = {}
        for name in ('demo/01-flow.json', 'demo/02-plan.json', 'demo/03-field.json',
                     'corridor-ok.json', 'corridor-storage.json'):
            for event in json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))['events']:
                self.events[event['id']] = event

    def send(self, command, *events):
        self.counter += 1
        p = self.dir / f'{self.counter}.json'
        p.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, command, p)

    def base(self, with_field=True, field_only_f1=False, with_storage=True):
        ingest(self.db, 'flow-import', ROOT / 'fixtures/demo/01-flow.json')
        ingest(self.db, 'plan-import', ROOT / 'fixtures/demo/02-plan.json')
        if field_only_f1:
            self.send('field-import', self.events['f1'])
        elif with_field:
            ingest(self.db, 'field-import', ROOT / 'fixtures/demo/03-field.json')
        ingest(self.db, 'corridor-import', ROOT / 'fixtures/corridor-ok.json')
        if with_storage:
            ingest(self.db, 'corridor-import', ROOT / 'fixtures/corridor-storage.json')

    def commitment(self, rows, cid):
        return next(c for c in rows if c['id'] == cid)

    def test_field_activation_contrast_cc1_and_eval(self):
        # With A's field readback: CC1 links safe-base and is provisional.
        self.base()
        r = report(self.db)
        self.assertEqual((r['corridor_check'][0]['active_plan'], r['corridor_check'][0]['state']),
                         ('safe-base', 'provisional'))
        # Same route and same B green window originals, but no 03-field: A has
        # no field readback, so CC1 is held and the joint window is unavailable
        # even though the plan itself is approved.
        self.db = self.dir / 'no-field.sqlite'
        self.base(with_field=False, with_storage=False)
        self.send('corridor-import', self.events['lk1'], self.events['le1'],
                  self.events['le2'], self.events['ce1'])
        r = report(self.db)
        self.assertEqual((r['corridor_check'][0]['active_plan'], r['corridor_check'][0]['state']),
                         (None, 'held'))
        self.assertEqual(r['phase_plan'][0]['status'], 'approved')
        ce1 = r['corridor_eval'][0]
        self.assertEqual((ce1['state'], ce1['reason']), ('unavailable', 'missing_field_evidence'))
        evidence = json.loads(ce1['evidence_json'])
        self.assertIsNone(evidence['a_readback'])
        self.assertEqual(evidence['coverage'], 'partial')

    def test_endpoint_intersection_alone_would_overpromise(self):
        self.base()
        ce1 = report(self.db)['corridor_eval'][0]
        # The naive check: A's readback vehicle window covers 08:06:40 and B's
        # green covers the shifted arrival interval [08:06:48, 08:06:54].
        naive_from = max('2026-01-02T08:06:45Z', '2026-01-02T08:06:48Z')
        naive_to = min('2026-01-02T08:07:15Z', '2026-01-02T08:06:54Z')
        self.assertLess(naive_from, naive_to)
        # Endpoint intersection would sign guaranteed, but the ordinary car
        # occupies the capacity-1 link until the 08:06:50 B-exit receipt.
        self.assertEqual((ce1['state'], ce1['reason']), ('conditional', 'capacity_blocked_until_receipt'))
        self.assertEqual((ce1['feasible_from'], ce1['feasible_to']),
                         ('2026-01-02T08:06:50Z', '2026-01-02T08:07:01Z'))
        evidence = json.loads(ce1['evidence_json'])
        self.assertEqual(evidence['coverage'], 'complete')
        self.assertEqual(evidence['link']['capacity'], 1)
        self.assertEqual(evidence['link']['occupancy_at_release'], 1)
        self.assertEqual(evidence['a_readback']['plan_id'], 'safe-base')
        self.assertEqual(evidence['arrival_window'], ['2026-01-02T08:06:48Z', '2026-01-02T08:06:54Z'])

    def test_same_endpoints_cleared_link_permits_guarantee(self):
        # Identical green windows, travel interval, request and A/B readbacks;
        # only the verifiable link events differ (car left at 08:06:38).
        le1 = deepcopy(self.events['le1'])
        le1['at'] = '2026-01-02T08:06:30Z'
        le2 = deepcopy(self.events['le2'])
        le2['at'] = '2026-01-02T08:06:38Z'
        self.base(with_storage=False)
        self.send('corridor-import', self.events['lk1'], le1, le2, self.events['ce1'])
        ce1 = report(self.db)['corridor_eval'][0]
        self.assertEqual((ce1['state'], ce1['reason']), ('guaranteed', 'capacity_and_green_proven'))
        self.assertLessEqual(ce1['feasible_from'], '2026-01-02T08:06:40Z')
        self.assertLessEqual('2026-01-02T08:06:40Z', ce1['feasible_to'])

    def test_meter_route_seals_guaranteed_window_without_trapping(self):
        self.base()
        r = report(self.db)
        au1 = self.commitment(r['corridor_commitment'], 'AU1')
        self.assertEqual((au1['state'], au1['status'], au1['reason']),
                         ('guaranteed', 'executed', 'metered_capacity_sealed'))
        evidence = json.loads(au1['evidence_json'])
        self.assertEqual(evidence['cost'], 'ordinary_flow_metered')
        self.assertTrue(evidence['empty_at_seal'])
        self.assertIn('stop-line', evidence['trap_guard'])
        self.assertFalse(evidence['ped_clearance']['compressed'])
        meter = r['corridor_meter'][0]
        self.assertEqual(meter['receipt'], 'meter-receipt-1')
        # Metering covers the whole sealed occupancy horizon, so ordinary cars
        # wait at A's stop-line and none is trapped inside the junction box.
        self.assertGreaterEqual(meter['meter_to'], '2026-01-02T08:07:15Z')
        notice = next(n for n in r['corridor_notice'] if n['kind'] == 'command_executed')
        self.assertEqual(json.loads(notice['detail'])['state'], 'guaranteed')

    def test_trigger_execution_never_backwrites_guaranteed(self):
        self.base()
        out = self.send('corridor-import',
                        {'id': 'ex2', 'kind': 'execute', 'commitment_id': 'AU2',
                         'at': '2026-01-02T08:06:50Z', 'recorded_at': '2026-01-02T08:06:51Z'})
        self.assertEqual(out['decisions'][0]['outcome'], 'executed')
        au2 = self.commitment(report(self.db)['corridor_commitment'], 'AU2')
        self.assertEqual((au2['state'], au2['status']), ('conditional', 'executed'))

    def test_trigger_withheld_until_exit_receipt_then_releases(self):
        # The B-exit receipt has not arrived: occupancy is unverified, the
        # release is withheld instead of doubly occupying the link.
        self.base(with_storage=False)
        self.send('corridor-import', self.events['lk1'], self.events['le1'],
                  self.events['ce1'], self.events['au2'])
        r = report(self.db)
        self.assertEqual((r['corridor_eval'][0]['state'], r['corridor_eval'][0]['reason']),
                         ('conditional', 'awaiting_exit_receipt'))
        out = self.send('corridor-import',
                        {'id': 'ex2', 'kind': 'execute', 'commitment_id': 'AU2',
                         'at': '2026-01-02T08:06:50Z', 'recorded_at': '2026-01-02T08:06:51Z'})
        self.assertEqual(out['decisions'][0]['outcome'], 'withheld')
        au2 = self.commitment(report(self.db)['corridor_commitment'], 'AU2')
        self.assertEqual(au2['status'], 'pending')
        self.assertEqual(report(self.db)['corridor_notice'][-1]['kind'], 'release_withheld')
        # Once the receipted departure lands, the same release moment executes.
        out = self.send('corridor-import', self.events['le2'],
                        {'id': 'ex3', 'kind': 'execute', 'commitment_id': 'AU2',
                         'at': '2026-01-02T08:06:50Z', 'recorded_at': '2026-01-02T08:06:52Z'})
        self.assertEqual(out['decisions'][-1]['outcome'], 'executed')
        au2 = self.commitment(report(self.db)['corridor_commitment'], 'AU2')
        self.assertEqual((au2['state'], au2['status']), ('conditional', 'executed'))

    def test_occupancy_persisting_waits_for_next_window(self):
        le2 = deepcopy(self.events['le2'])
        le2['at'] = '2026-01-02T08:07:10Z'
        self.base(with_storage=False)
        self.send('corridor-import', self.events['lk1'], self.events['le1'], le2,
                  self.events['ce1'], self.events['au2'])
        ce1 = report(self.db)['corridor_eval'][0]
        self.assertEqual((ce1['state'], ce1['reason']), ('unavailable', 'no_feasible_window'))
        out = self.send('corridor-import',
                        {'id': 'ex2', 'kind': 'execute', 'commitment_id': 'AU2',
                         'at': '2026-01-02T08:06:55Z', 'recorded_at': '2026-01-02T08:06:56Z'})
        self.assertEqual(out['decisions'][0]['outcome'], 'withheld')
        self.assertEqual(self.commitment(report(self.db)['corridor_commitment'], 'AU2')['status'],
                         'pending')

    def test_comms_loss_falls_back_to_safe_plan(self):
        self.base()
        out = self.send('corridor-import',
                        {'id': 'fb1', 'kind': 'fallback', 'commitment_id': 'AU2',
                         'at': '2026-01-02T08:06:53Z', 'reason': 'comms_lost',
                         'recorded_at': '2026-01-02T08:06:54Z'})
        self.assertEqual(out['decisions'][0]['outcome'], 'fallback')
        r = report(self.db)
        self.assertEqual(self.commitment(r['corridor_commitment'], 'AU2')['status'], 'fallback')
        self.assertIn('fallback_to_safe_plan', [n['kind'] for n in r['corridor_notice']])
        with self.assertRaisesRegex(ValueError, 'commitment_not_pending'):
            self.send('corridor-import',
                      {'id': 'ex2', 'kind': 'execute', 'commitment_id': 'AU2',
                       'at': '2026-01-02T08:06:55Z', 'recorded_at': '2026-01-02T08:06:56Z'})

    def test_fault_revokes_pending_but_keeps_executed_and_notices(self):
        self.base(field_only_f1=True)
        self.send('field-import',
                  {'id': 'fx1', 'kind': 'fault', 'controller_id': 'ctrl-base',
                   'at': '2026-01-02T08:06:55Z', 'actor': 'tech-9',
                   'recorded_at': '2026-01-02T08:06:56Z'})
        r = report(self.db)
        au1 = self.commitment(r['corridor_commitment'], 'AU1')
        au2 = self.commitment(r['corridor_commitment'], 'AU2')
        self.assertEqual((au1['status'], au1['state'], au1['window_from']),
                         ('executed', 'guaranteed', '2026-01-02T08:06:50Z'))
        self.assertEqual((au2['status'], au2['reason']), ('revoked', 'field_fault_rollback'))
        self.assertEqual([n['kind'] for n in r['corridor_notice'] if n['commitment_id'] == 'AU2'],
                         ['manual_notify'])
        self.assertEqual(r['corridor_check'][0]['state'], 'provisional')

    def test_remote_revoke_invalidates_pending_only(self):
        self.base()
        self.send('corridor-import',
                  {'id': 'rv1', 'kind': 'remote_revoke', 'remote_id': 'B1',
                   'at': '2026-01-02T08:06:57Z', 'recorded_at': '2026-01-02T08:06:58Z'})
        r = report(self.db)
        self.assertEqual(r['remote_signal_window'][0]['verified'], 0)
        self.assertEqual(self.commitment(r['corridor_commitment'], 'AU2')['status'], 'revoked')
        self.assertEqual(self.commitment(r['corridor_commitment'], 'AU2')['reason'],
                         'remote_readback_revoked')
        self.assertEqual(self.commitment(r['corridor_commitment'], 'AU1')['status'], 'executed')
        self.assertIn('manual_notify', [n['kind'] for n in r['corridor_notice']])

    def test_audit_locates_link_event_tamper(self):
        self.base()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE link_event SET at='2026-01-02T08:06:38Z' WHERE id='le2'")
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertFalse(result['replay_ok'])
        self.assertIn('link_event', result['diverged_tables'])
        self.assertNotIn('corridor_eval', result['diverged_tables'])

    def test_audit_locates_joint_window_tamper(self):
        self.base()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE corridor_eval SET state='guaranteed' WHERE id='CE1'")
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertFalse(result['replay_ok'])
        self.assertEqual(result['diverged_tables'], ['corridor_eval'])

    def test_pedestrian_clearance_never_compressed(self):
        self.base()
        ce1 = report(self.db)['corridor_eval'][0]
        self.assertEqual(json.loads(ce1['evidence_json'])['ped_clearance'],
                         {'required_sec': 10, 'readback_sec': 12, 'compressed': False})
        # A priority change that shortens pedestrian clearance must not be signed.
        db = sqlite3.connect(self.db)
        db.execute("UPDATE phase_plan SET ped_green_sec=4 WHERE id='safe-base'")
        db.commit()
        db.close()
        ce2 = deepcopy(self.events['ce1'])
        ce2.update(id='ce2', eval_id='CE2')
        out = self.send('corridor-import', ce2)
        self.assertEqual(out['decisions'][0]['outcome'], 'unavailable')
        self.assertEqual(report(self.db)['corridor_eval'][-1]['reason'],
                         'pedestrian_clearance_compressed')

    def test_base_replay_and_audit_clean(self):
        self.base()
        result = audit(self.db)
        self.assertTrue(result['replay_ok'])
        self.assertEqual(result['diverged_tables'], [])
        self.assertTrue(result['source_integrity'])
        self.assertTrue(result['event_integrity'])


if __name__ == '__main__':
    unittest.main()
