import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report

ROOT = Path(__file__).resolve().parent.parent


def topo_event(topology_id='T1', revision=1, speed=0.2, trunk=1.0, north=1.0, south=1.0,
               north_min=45, south_min=20, wiring='W-2026-01', templates=None,
               event_id=None, valid_from='2026-01-01T08:00:00Z'):
    return {
        'id': event_id or f'topo-{topology_id}-r{revision}', 'kind': 'branch_topology',
        'topology_id': topology_id, 'controller_id': 'ctrl-base', 'revision': revision,
        'trunk_km': trunk, 'north_branch_km': north, 'south_branch_km': south,
        'propagation_km_per_us': speed, 'wiring_revision': wiring,
        'north_access_control_min': north_min, 'south_access_control_min': south_min,
        'valid_from': valid_from,
        'splice_templates': templates if templates is not None else [
            {'splice_id': 'ST-Y', 'at_km': 1.0, 'amplitude_expected': 0.8, 'kind': 'y_splitter_joint'},
            {'splice_id': 'ST-TRUNK', 'at_km': 0.6, 'amplitude_expected': 0.55, 'kind': 'trunk_joint'},
        ],
        'recorded_at': valid_from,
    }


def echo_event(echo_id='E14', topology_id='T1', revision=1, return_us=14.0, amplitude=0.4,
               measured_at='2026-01-02T08:00:00Z', event_id=None):
    return {
        'id': event_id or f'echo-{echo_id}', 'kind': 'branch_echo', 'echo_id': echo_id,
        'topology_id': topology_id, 'topology_revision': revision,
        'measured_at': measured_at, 'direction': 'forward',
        'trace': [{'return_us': return_us, 'amplitude': amplitude}],
        'remote_terminals': [
            {'branch': 'north', 'visible_echo': False},
            {'branch': 'south', 'visible_echo': False},
        ],
        'recorded_at': measured_at,
    }


class FiberBranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'branch.sqlite'
        self.n = 0

    def send(self, *events, command='fiber-branch-import'):
        self.n += 1
        path = Path(self.temp.name) / f'{self.n}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, command, path)

    def send_legacy(self, route_speed, return_us):
        """Reproduce the provisional fibertriage.check speed dependency:
        identical echo, two libraries with different route calibration."""
        self.n += 1
        path = Path(self.temp.name) / f'legacy-{self.n}.json'
        route = {
            'id': f'fr-{self.n}', 'kind': 'fiber_route', 'route_id': f'F-{self.n}',
            'controller_id': 'ctrl-base', 'propagation_km_per_us': route_speed,
            'splice_km': 1.0, 'north_branch_km': 1.0, 'south_branch_km': 1.0,
            'recorded_at': '2026-01-01T08:00:00Z',
        }
        echo = {
            'id': f'fe-{self.n}', 'kind': 'fiber_echo', 'echo_id': f'FE-{self.n}',
            'route_id': f'F-{self.n}', 'measured_at': '2026-01-02T08:00:00Z',
            'return_us': return_us, 'amplitude': 0.4,
            'recorded_at': '2026-01-02T08:01:00Z',
        }
        check = {
            'id': f'fc-{self.n}', 'kind': 'fiber_check', 'check_id': f'FC-{self.n}',
            'echo_id': f'FE-{self.n}', 'recorded_at': '2026-01-02T08:02:00Z',
        }
        path.write_text(json.dumps({'events': [route, echo, check]}), encoding='utf-8')
        return ingest(self.db, 'fiber-import', path)

    def latest_order(self, wo_id):
        orders = report(self.db)['fiber_work_order']
        return next(o for o in reversed(orders) if o['wo_id'] == wo_id)

    def candidates(self, wo_id, revision):
        return [c for c in report(self.db)['fiber_work_order_candidate']
                if c['wo_id'] == wo_id and c['wo_revision'] == revision]

    def test_calibration_speed_changes_provisional_one_dimensional_triage(self):
        # 0.2 km/us * 10 us / 2 = 1.0 km -> matches the registered splice.
        self.send_legacy(0.2, 10.0)
        row = report(self.db)['fiber_triage'][0]
        self.assertEqual((row['distance_km'], row['state']), (1.0, 'splice_inspection'))
        # Same echo in another library, calibrated at 0.16 km/us -> 0.8 km.
        self.send_legacy(0.16, 10.0)
        row = report(self.db)['fiber_triage'][-1]
        self.assertEqual((row['distance_km'], row['state']), (0.8, 'line_inspection'))
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_symmetric_y_branch_breakpoints_share_one_echo(self):
        self.send(topo_event(), echo_event())
        r = report(self.db)
        stored = json.loads(r['fiber_branch_echo'][0]['trace_json'])
        # Round-trip 14 us at 0.2 km/us is a 1.4 km ONE-WAY distance, never 2.8.
        self.assertEqual(stored[0]['distance_km'], 1.4)
        order = self.latest_order('WO-E14')
        self.assertEqual(order['status'], 'ambiguous_candidates')
        self.assertEqual(order['scope'], 'candidate_segments')
        self.assertEqual(order['equipment'], 'full_branch_inspection_kit')
        rows = self.candidates('WO-E14', order['revision'])
        self.assertEqual({(c['branch'], c['offset_km']) for c in rows},
                         {('north', 0.4), ('south', 0.4)})
        # Two distinct executable candidate boxes, not "both branches broken".
        self.assertEqual(len(rows), 2)
        # Shortest executable order first: south closure is 20 min vs north 45.
        self.assertEqual([c['branch'] for c in rows], ['south', 'north'])
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_swapping_north_and_south_leaves_cabinet_observation_unchanged(self):
        # Same echo, swapped closure metadata: the instrument cannot tell the
        # two worlds apart; only the dispatch order flips with the geography.
        self.send(topo_event(north_min=20, south_min=45, event_id='bt-swap'),
                  echo_event(event_id='be-swap'))
        order = self.latest_order('WO-E14')
        rows = self.candidates('WO-E14', order['revision'])
        self.assertEqual({c['offset_km'] for c in rows}, {0.4})
        self.assertEqual([c['branch'] for c in rows], ['north', 'south'])

    def test_power_meter_narrows_to_whole_branch_without_point_location(self):
        self.send(topo_event(), echo_event())
        probe = {
            'id': 'rp1', 'kind': 'remote_probe', 'probe_id': 'P1', 'echo_id': 'E14',
            'measured_at': '2026-01-02T08:30:00Z', 'direction': 'reverse',
            'disconnected_running': False,
            'readings': [
                {'branch': 'north', 'light_received': False, 'meter': 'PM-N'},
                {'branch': 'south', 'light_received': True, 'meter': 'PM-S'},
            ],
            'recorded_at': '2026-01-02T08:31:00Z',
        }
        self.send(probe)
        order = self.latest_order('WO-E14')
        self.assertEqual(order['status'], 'branch_scoped')
        self.assertEqual(order['scope'], 'whole_branch')
        self.assertEqual(order['equipment'], 'full_branch_inspection_kit')
        self.assertEqual(order['service_impact'], 'none_yet')
        rows = self.candidates('WO-E14', order['revision'])
        self.assertEqual([(c['branch'], c['offset_km']) for c in rows], [('north', None)])
        evidence = report(self.db)['fiber_remote_probe'][0]
        self.assertEqual(evidence['direction'], 'reverse')
        self.assertIn('no_light_cannot_locate_point_along_branch', evidence['inference_json'])
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_running_branch_disconnect_is_rejected(self):
        self.send(topo_event())
        self.send(echo_event())
        probe = {
            'id': 'rp-bad', 'kind': 'remote_probe', 'probe_id': 'P-BAD', 'echo_id': 'E14',
            'measured_at': '2026-01-02T08:30:00Z', 'direction': 'reverse',
            'disconnected_running': True,
            'readings': [
                {'branch': 'north', 'light_received': False},
                {'branch': 'south', 'light_received': True},
            ],
        }
        with self.assertRaisesRegex(ValueError, 'running_branch_must_not_be_disconnected'):
            self.send(probe)
        # The rejected source rolls back entirely; the two earlier sources stay.
        self.assertEqual(audit(self.db)['sources'], 2)

    def test_maintenance_disconnect_cannot_verify_other_branch(self):
        self.send(topo_event())
        self.send(echo_event())
        # North is under maintenance (intentionally opened); the dark north
        # reading is explained by the disconnect, not a fault finding.
        probe = {
            'id': 'rp-maint', 'kind': 'remote_probe', 'probe_id': 'P-M', 'echo_id': 'E14',
            'measured_at': '2026-01-02T08:30:00Z', 'direction': 'reverse',
            'disconnected_running': False, 'maintenance_branch': 'north',
            'readings': [
                {'branch': 'north', 'light_received': False},
                {'branch': 'south', 'light_received': True},
            ],
        }
        with self.assertRaisesRegex(ValueError, 'maintenance_disconnect_is_not_a_fault_finding'):
            self.send(probe)
        # The same maintenance event can never be turned into continuity
        # evidence for the opposite (south) branch either.
        probe['maintenance_branch'] = 'south'
        with self.assertRaisesRegex(ValueError, 'maintenance_disconnect_cannot_verify_other_branch'):
            self.send(probe)

    def _backup_receipt(self, receipt_id='BR1', issued='2026-01-02T08:20:00Z', status='carrying_traffic'):
        return {
            'id': f'bk-{receipt_id}', 'kind': 'backup_switchover', 'receipt_id': receipt_id,
            'link_id': 'standby-L1', 'issued_at': issued, 'status': status,
            'reason': 'standby_link_carrying_traffic',
            'recorded_at': issued,
        }

    def _isolation(self, fault='north', receipt_id='BR1', started='2026-01-02T08:40:00Z'):
        steps = []
        for branch, present in ((fault, False), ('south' if fault == 'north' else 'north', True)):
            steps.append({
                'branch': branch, 'direction': 'forward', 'echo_present': present,
                **({} if not present else {'return_us': 14.0}),
                'from_at': '2026-01-02T08:40:00Z', 'to_at': '2026-01-02T08:48:00Z',
                'interrupted': True, 'reason': f'{branch}_isolation_window',
            })
        return {
            'id': 'bi1', 'kind': 'branch_isolation', 'isolation_id': 'IS1', 'echo_id': 'E14',
            'switch_order_id': 'SO-77', 'receipt_id': receipt_id,
            'started_at': started, 'ended_at': '2026-01-02T08:55:00Z',
            'steps': steps, 'recorded_at': '2026-01-02T08:55:30Z',
        }

    def test_per_branch_isolation_localizes_point_under_backup_receipt(self):
        self.send(topo_event(), echo_event(), self._backup_receipt(), self._isolation())
        r = report(self.db)
        order = self.latest_order('WO-E14')
        self.assertEqual(order['status'], 'point_localized')
        self.assertEqual(order['scope'], 'point_segment')
        self.assertEqual(order['equipment'], 'targeted_splice_crew')
        rows = self.candidates('WO-E14', order['revision'])
        self.assertEqual([(c['branch'], c['offset_km']) for c in rows], [('north', 0.4)])
        windows = r['fiber_service_window']
        self.assertTrue(all(w['interrupted'] == 1 for w in windows))
        # The isolation outage window (08:40) is kept separate from the
        # original single-ended capture (08:00): it never rewrites the echo.
        self.assertEqual(r['fiber_branch_echo'][0]['measured_at'], '2026-01-02T08:00:00Z')
        self.assertEqual(min(w['from_at'] for w in windows), '2026-01-02T08:40:00Z')
        # Receipt evidence and controller readback are separate tables.
        self.assertEqual(r['fiber_backup_receipt'][0]['receipt_id'], 'BR1')
        self.assertEqual(r['controller'], [])
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_isolation_requires_standby_receipt_before_window(self):
        self.send(topo_event(), echo_event())
        with self.assertRaisesRegex(ValueError, 'backup_receipt_required_before_isolation'):
            self.send(self._isolation())
        # A receipt issued only after the window starts cannot authorize it.
        self.send(self._backup_receipt(receipt_id='BR-LATE', issued='2026-01-02T08:45:00Z'))
        with self.assertRaisesRegex(ValueError, 'standby_link_not_confirmed_before_window'):
            self.send(self._isolation(receipt_id='BR-LATE'))
        # A receipt that does not confirm carrying traffic is equally rejected.
        self.send(self._backup_receipt(receipt_id='BR-IDLE', status='requested'))
        with self.assertRaisesRegex(ValueError, 'standby_link_not_confirmed_before_window'):
            self.send(self._isolation(receipt_id='BR-IDLE'))

    def test_late_topology_revision_reprojects_open_but_not_dispatched_orders(self):
        self.send(topo_event(), echo_event())
        # Point-localize E14 first (closed order), then open a second echo.
        self.send(self._backup_receipt(), self._isolation())
        second = echo_event(echo_id='E15', event_id='be2', measured_at='2026-01-02T09:00:00Z')
        self.send(second)
        before = self.latest_order('WO-E15')
        self.assertEqual({c['offset_km'] for c in self.candidates('WO-E15', before['revision'])},
                         {0.4})
        # A late survey corrects the trunk length; both branches remain 1 km.
        revised = topo_event(revision=2, trunk=1.07, event_id='bt2',
                             valid_from='2026-01-02T09:30:00Z')
        outcome = self.send(revised)['decisions'][0]['outcome']
        self.assertEqual(outcome, 'registered_reprojected_1')
        reopened = self.latest_order('WO-E15')
        self.assertEqual(reopened['topology_revision'], 2)
        self.assertEqual(reopened['reason'], 'late_topology_revision_reprojected')
        self.assertEqual({c['offset_km'] for c in self.candidates('WO-E15', reopened['revision'])},
                         {0.33})
        # The already dispatched order keeps its original topology pin.
        closed = self.latest_order('WO-E14')
        self.assertEqual(closed['status'], 'point_localized')
        self.assertEqual(closed['topology_revision'], 1)
        # Both revisions remain replayable.
        history = report(self.db)['fiber_branch_topology_history']
        self.assertEqual(sorted(h['revision'] for h in history if h['topology_id'] == 'T1'), [1, 2])
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_stale_topology_revision_is_rejected(self):
        self.send(topo_event())
        with self.assertRaisesRegex(ValueError, 'stale_topology_revision'):
            self.send(topo_event(revision=1, event_id='bt-dup'))

    def test_old_splice_template_does_not_match_new_wiring(self):
        # Revision 1 had a splice at 1.4 km; the 0.4 echo matched it then.
        self.send(topo_event(templates=[
            {'splice_id': 'ST-OLD', 'at_km': 1.4, 'amplitude_expected': 0.4, 'kind': 'old_joint'},
        ]))
        self.send(echo_event())
        order = self.latest_order('WO-E14')
        self.assertEqual(order['status'], 'splice_known')
        # Re-wiring at the Y: same position geometry, new wiring revision with
        # no matching template -> the old template cannot explain the echo.
        self.send(topo_event(revision=2, wiring='W-2026-02', templates=[], event_id='bt2'))
        order = self.latest_order('WO-E14')
        self.assertEqual(order['status'], 'ambiguous_candidates')
        rows = self.candidates('WO-E14', order['revision'])
        self.assertEqual({c['branch'] for c in rows}, {'north', 'south'})

    def test_tampered_echo_and_work_order_projection_fail_audit(self):
        self.send(topo_event(), echo_event())
        self.assertTrue(all(audit(self.db)[k]
                            for k in ('replay_ok', 'source_integrity', 'event_integrity')))
        # Tamper 1: rewrite the captured echo projection directly.
        db = sqlite3.connect(self.db)
        db.execute("UPDATE fiber_branch_echo SET trace_json=? WHERE echo_id='E14'",
                   (json.dumps([{'return_us': 20.0, 'distance_km': 2.0, 'amplitude': 0.4}]),))
        db.commit(); db.close()
        result = audit(self.db)
        self.assertFalse(result['replay_ok'])
        self.assertTrue(result['source_integrity'])
        # Tamper 2: mutate a work order projection row (still no source change).
        db = sqlite3.connect(self.db)
        db.execute("UPDATE fiber_branch_echo SET trace_json=? WHERE echo_id='E14'",
                   (json.dumps([{'return_us': 14.0, 'distance_km': 1.4, 'amplitude': 0.4}]),))
        db.execute("UPDATE fiber_work_order SET status='point_localized' WHERE wo_id='WO-E14'")
        db.commit(); db.close()
        self.assertFalse(audit(self.db)['replay_ok'])
        # Tamper 3: alter the raw source itself.
        db = sqlite3.connect(self.db)
        db.execute("UPDATE source_file SET raw=? WHERE id=1", (b'{"events":[]}',))
        db.commit(); db.close()
        self.assertFalse(audit(self.db)['source_integrity'])

    def test_non_decisive_isolation_is_rejected(self):
        self.send(topo_event(), echo_event(), self._backup_receipt())
        iso = self._isolation()
        iso['id'] = 'bi-bad'
        # Echo survives every isolation: the fault is not on either branch.
        for step in iso['steps']:
            step['echo_present'] = True
            step['return_us'] = 14.0
        with self.assertRaisesRegex(ValueError, 'isolation_result_not_decisive'):
            self.send(iso)

    def test_measurement_directions_are_enforced(self):
        self.send(topo_event(), echo_event())
        wrong_echo = echo_event(echo_id='EBAD', event_id='be-bad')
        wrong_echo['direction'] = 'reverse'
        with self.assertRaisesRegex(ValueError, 'cabinet_echo_must_be_forward_direction'):
            self.send(wrong_echo)
        wrong_probe = {
            'id': 'rp-bad-dir', 'kind': 'remote_probe', 'probe_id': 'PBAD', 'echo_id': 'E14',
            'measured_at': '2026-01-02T08:30:00Z', 'direction': 'forward',
            'disconnected_running': False,
            'readings': [
                {'branch': 'north', 'light_received': False},
                {'branch': 'south', 'light_received': True},
            ],
        }
        with self.assertRaisesRegex(ValueError, 'remote_probe_must_be_reverse_direction'):
            self.send(wrong_probe)

    def test_full_fixture_narratives_both_field_trials(self):
        ingest(self.db, 'fiber-branch-import', ROOT / 'fixtures/fiber-branch.json')
        r = report(self.db)
        revisions = [o['revision'] for o in r['fiber_work_order'] if o['wo_id'] == 'WO-E14']
        self.assertEqual(revisions, [1, 2, 3])
        statuses = [(o['status'], o['scope'], o['equipment'], o['service_impact'])
                    for o in r['fiber_work_order'] if o['wo_id'] == 'WO-E14']
        self.assertEqual(statuses, [
            ('ambiguous_candidates', 'candidate_segments', 'full_branch_inspection_kit', 'none_yet'),
            ('branch_scoped', 'whole_branch', 'full_branch_inspection_kit', 'none_yet'),
            ('point_localized', 'point_segment', 'targeted_splice_crew',
             'short_outage_under_backup_receipt'),
        ])
        # Test A and B differ in segment width, kit and service impact.
        self.assertEqual(len(r['fiber_remote_probe']), 1)
        self.assertEqual(r['fiber_branch_isolation'][0]['fault_branch'], 'north')
        self.assertTrue(audit(self.db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
