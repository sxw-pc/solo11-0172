import hashlib
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report

ROOT = Path(__file__).resolve().parents[1]


def event_sha(e):
    return hashlib.sha256(json.dumps(e, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class BusPeakTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'peak.sqlite'
        self.counter = 0
        self.events = {}
        for event in json.loads((ROOT / 'fixtures/bus-door-order.json').read_text(encoding='utf-8'))['events']:
            self.events[event['id']] = event

    def send(self, *events, db=None):
        self.counter += 1
        path = self.root / f'{self.counter}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(db or self.db, 'bus-import', path)

    def base(self, db=None):
        self.send(self.events['bt1'], self.events['db1'], self.events['bl1'], self.events['cmp1'], db=db)

    def segment(self, sid, door, directions, **kw):
        times = kw.pop('times', ['2026-01-02T08:10:02Z', '2026-01-02T08:10:04Z', '2026-01-02T08:10:07Z'])
        gseqs = kw.pop('gseqs', [None] * len(directions))
        events = []
        for i, direction in enumerate(directions):
            ev = {'seq': i + 1, 'at': times[i], 'direction': direction}
            if gseqs[i] is not None:
                ev['gateway_seq'] = gseqs[i]
            events.append(ev)
        return {'id': sid.lower(), 'kind': 'door_segment', 'segment_id': sid,
                'batch_id': kw.pop('batch_id', 'DB1'), 'door': door,
                'gateway_id': kw.pop('gateway_id', 'GW-BUS1'), 'clock_synced': kw.pop('clock_synced', True),
                'door_open_at': kw.pop('door_open_at', '2026-01-02T08:10:01Z'),
                'door_close_at': kw.pop('door_close_at', '2026-01-02T08:10:09Z'),
                'events': events, 'recorded_at': '2026-01-02T09:30:00Z', **kw}

    def assess(self, aid, batch='DB1'):
        return {'id': aid.lower(), 'kind': 'peak_assess', 'assessment_id': aid, 'batch_id': batch,
                'recorded_at': '2026-01-02T10:05:00Z'}

    def peak(self, db, aid):
        return next(p for p in report(db or self.db)['bus_stop_peak'] if p['id'] == aid)

    def test_fixture_replays_complaint_story(self):
        ingest(self.db, 'bus-import', ROOT / 'fixtures/bus-door-order.json')
        r = report(self.db)
        load = r['bus_load'][0]
        self.assertEqual((load['start_load'], load['end_load'], load['chain'], load['state']),
                         (10, 10, 'trip_initial', 'nominal_within'))
        peaks = {p['id']: p for p in r['bus_stop_peak']}
        self.assertEqual((peaks['PK1']['peak_lo'], peaks['PK1']['peak_hi'], peaks['PK1']['verdict'],
                          peaks['PK1']['coverage']), (10, 13, 'pending_verification', 'none'))
        self.assertEqual((peaks['PK2']['peak_lo'], peaks['PK2']['peak_hi'], peaks['PK2']['verdict'],
                          peaks['PK2']['coverage']), (10, 13, 'pending_verification', 'endpoints_only'))
        self.assertEqual((peaks['PK3']['peak_lo'], peaks['PK3']['peak_hi'], peaks['PK3']['verdict'],
                          peaks['PK3']['coverage']), (13, 13, 'overload_confirmed', 'continuous'))
        rulings = r['bus_complaint_ruling']
        self.assertEqual([(x['revision'], x['verdict'], x['peak_lo'], x['peak_hi']) for x in rulings],
                         [(1, 'pending_verification', 10, 13), (2, 'overload_confirmed', 13, 13)])
        self.assertEqual(rulings[0]['responsibility'], '')
        self.assertEqual(rulings[1]['responsibility'], 'stop_boarding_management')
        self.assertTrue(all(audit(self.db)[k] for k in ('replay_ok', 'source_integrity', 'event_integrity')))

    def test_initial_load_drives_nominal_state(self):
        ok = json.loads((ROOT / 'fixtures/bus-stop-ok.json').read_text(encoding='utf-8'))['events']
        ingest(self.db, 'bus-import', ROOT / 'fixtures/bus-stop-ok.json')
        row = report(self.db)['bus_load'][0]
        self.assertEqual((row['start_load'], row['end_load'], row['state']), (10, 10, 'nominal_within'))
        db2 = self.root / 'shifted.sqlite'
        shifted = [deepcopy(e) for e in ok]
        shifted[0]['initial_load'] = 13
        self.send(*shifted, db=db2)
        row = report(db2)['bus_load'][0]
        self.assertEqual((row['start_load'], row['end_load'], row['state']), (13, 13, 'nominal_outside'))

    def test_same_summary_worlds_alpha_and_beta(self):
        worlds, dbs = {}, {}
        for name, left, right in (('alpha', ['alight', 'alight', 'board'], ['alight', 'board', 'board']),
                                  ('beta', ['board', 'board', 'alight'], ['board', 'alight', 'alight'])):
            db = dbs[name] = self.root / f'world-{name}.sqlite'
            self.base(db)
            self.send(self.segment(f'DS-{name}-L', 'left', left, gseqs=[1, 2, 5]),
                      self.segment(f'DS-{name}-R', 'right', right, gseqs=[3, 6, 7],
                                   times=['2026-01-02T08:10:03Z', '2026-01-02T08:10:08Z',
                                          '2026-01-02T08:10:09Z']), db=db)
            self.send(self.assess(f'PK-{name}'), db=db)
            worlds[name] = report(db)
        self.assertEqual(worlds['alpha']['bus_door_batch'], worlds['beta']['bus_door_batch'])
        self.assertEqual(worlds['alpha']['bus_load'], worlds['beta']['bus_load'])

        def stored_sha(db, event_id):
            conn = sqlite3.connect(db)
            row = conn.execute('SELECT sha FROM event WHERE id=?', (event_id,)).fetchone()
            conn.close()
            return row[0]

        for event_id in ('db1', 'bl1'):
            self.assertEqual(stored_sha(dbs['alpha'], event_id), stored_sha(dbs['beta'], event_id))
            self.assertEqual(stored_sha(dbs['alpha'], event_id), event_sha(self.events[event_id]))
        alpha, beta = worlds['alpha']['bus_stop_peak'][0], worlds['beta']['bus_stop_peak'][0]
        self.assertEqual((alpha['peak_lo'], alpha['peak_hi'], alpha['verdict']), (10, 10, 'overload_excluded'))
        self.assertEqual((beta['peak_lo'], beta['peak_hi'], beta['verdict']), (13, 13, 'overload_confirmed'))
        for db in dbs.values():
            self.assertTrue(audit(db)['replay_ok'])

    def test_shared_cabin_single_start(self):
        batch = deepcopy(self.events['db1'])
        batch.update(id='db4', batch_id='DB4', board_count=4, alight_count=0)
        check = {'id': 'bl4', 'kind': 'load_check', 'check_id': 'BL4', 'batch_id': 'DB4',
                 'recorded_at': '2026-01-02T08:12:00Z'}
        self.send(self.events['bt1'], batch, check)
        self.send(self.segment('DS4-L', 'left', ['board', 'board'], batch_id='DB4'),
                  self.segment('DS4-R', 'right', ['board', 'board'], batch_id='DB4',
                               times=['2026-01-02T08:10:03Z', '2026-01-02T08:10:05Z']))
        self.send(self.assess('PK4', 'DB4'))
        peak = self.peak(self.db, 'PK4')
        self.assertEqual(peak['start_load'], 10)
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict']), (14, 14, 'overload_confirmed'))

    def test_cross_stop_chain_and_gap(self):
        db2 = deepcopy(self.events['db1'])
        db2.update(id='db2', batch_id='DB2', stop_id='north-library', stop_seq=2,
                   window_from='2026-01-02T08:20:00Z', window_to='2026-01-02T08:20:10Z',
                   board_count=1, alight_count=0)
        bl2 = {'id': 'bl2', 'kind': 'load_check', 'check_id': 'BL2', 'batch_id': 'DB2',
               'recorded_at': '2026-01-02T08:22:00Z'}
        db4 = deepcopy(db2)
        db4.update(id='db4', batch_id='DB4', stop_id='north-depot', stop_seq=4,
                   window_from='2026-01-02T08:30:00Z', window_to='2026-01-02T08:30:10Z',
                   board_count=0, alight_count=0)
        bl4 = {'id': 'bl4', 'kind': 'load_check', 'check_id': 'BL4', 'batch_id': 'DB4',
               'recorded_at': '2026-01-02T08:32:00Z'}
        self.send(self.events['bt1'], self.events['db1'], self.events['bl1'], db2, bl2, db4, bl4)
        self.send(self.assess('PK2', 'DB2'), self.assess('PK4', 'DB4'))
        loads = {row['batch_id']: row for row in report(self.db)['bus_load']}
        self.assertEqual((loads['DB2']['start_load'], loads['DB2']['end_load'], loads['DB2']['chain']),
                         (10, 11, 'chained'))
        self.assertEqual((loads['DB4']['start_load'], loads['DB4']['end_load'], loads['DB4']['chain']),
                         (11, 11, 'chain_gap'))
        self.assertEqual((self.peak(self.db, 'PK2')['start_load'], self.peak(self.db, 'PK2')['verdict']),
                         (10, 'overload_excluded'))
        self.assertEqual(self.peak(self.db, 'PK4')['verdict'], 'pending_verification')
        self.assertIn('unverified_start_load', self.peak(self.db, 'PK4')['reason'])

    def test_unsynced_overlapping_doors_stay_pending(self):
        self.base()
        self.send(self.segment('DS-U-L', 'left', ['board'] * 3, clock_synced=False,
                               times=['2026-01-02T08:10:02Z', '2026-01-02T08:10:04Z', '2026-01-02T08:10:06Z'],
                               door_open_at='2026-01-02T08:10:02Z', door_close_at='2026-01-02T08:10:06Z'),
                  self.segment('DS-U-R', 'right', ['alight'] * 3, clock_synced=False,
                               times=['2026-01-02T08:10:04Z', '2026-01-02T08:10:06Z', '2026-01-02T08:10:08Z'],
                               door_open_at='2026-01-02T08:10:04Z', door_close_at='2026-01-02T08:10:08Z'))
        self.send(self.assess('PK-U'))
        peak = self.peak(self.db, 'PK-U')
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict']), (10, 13, 'pending_verification'))

    def test_same_gateway_sequence_locks_global_order(self):
        for name, lseq, rseq, expect in (('boards-first', [1, 2, 3], [4, 5, 6], ('overload_confirmed', 13, 13)),
                                         ('alights-first', [4, 5, 6], [1, 2, 3], ('overload_excluded', 10, 10))):
            db = self.root / f'gw-{name}.sqlite'
            self.base(db)
            self.send(self.segment(f'DS-G-{name}-L', 'left', ['board'] * 3, clock_synced=False, gseqs=lseq,
                                   times=['2026-01-02T08:10:02Z', '2026-01-02T08:10:04Z',
                                          '2026-01-02T08:10:06Z']),
                      self.segment(f'DS-G-{name}-R', 'right', ['alight'] * 3, clock_synced=False, gseqs=rseq,
                                   times=['2026-01-02T08:10:03Z', '2026-01-02T08:10:05Z',
                                          '2026-01-02T08:10:07Z']), db=db)
            self.send(self.assess(f'PK-G-{name}'), db=db)
            peak = self.peak(db, f'PK-G-{name}')
            self.assertEqual((peak['verdict'], peak['peak_lo'], peak['peak_hi']), expect)

    def test_sparse_snapshot_cannot_dismiss_or_be_continuous(self):
        self.base()
        self.send(deepcopy(self.events['eva1']), self.assess('PK-A'))
        peak = self.peak(self.db, 'PK-A')
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict'], peak['coverage']),
                         (10, 13, 'pending_verification', 'endpoints_only'))
        db2 = self.root / 'dense.sqlite'
        self.base(db2)
        dense = deepcopy(self.events['eva1'])
        dense.update(id='eva-dense', evidence_id='EVA-DENSE')
        dense['snapshots'] = ([{'at': '2026-01-02T08:09:59Z', 'count': 10}]
                              + [{'at': f'2026-01-02T08:10:0{i}Z', 'count': 10} for i in range(1, 10)]
                              + [{'at': '2026-01-02T08:10:11Z', 'count': 10}])
        self.send(dense, self.assess('PK-DENSE'), db=db2)
        peak = self.peak(db2, 'PK-DENSE')
        self.assertEqual(peak['coverage'], 'partial')
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict']), (10, 13, 'pending_verification'))
        db3 = self.root / 'caught.sqlite'
        self.base(db3)
        caught = deepcopy(self.events['eva1'])
        caught.update(id='eva-caught', evidence_id='EVA-CAUGHT')
        caught['snapshots'] = [{'at': '2026-01-02T08:09:59Z', 'count': 10},
                               {'at': '2026-01-02T08:10:05Z', 'count': 13},
                               {'at': '2026-01-02T08:10:11Z', 'count': 10}]
        self.send(caught, self.assess('PK-CAUGHT'), db=db3)
        peak = self.peak(db3, 'PK-CAUGHT')
        self.assertEqual((peak['peak_lo'], peak['verdict']), (13, 'overload_confirmed'))

    def test_continuous_video_decides_and_partial_video_falls_back(self):
        self.base()
        self.send(deepcopy(self.events['evb1']), self.assess('PK-B'))
        peak = self.peak(self.db, 'PK-B')
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict'], peak['coverage']),
                         (13, 13, 'overload_confirmed', 'continuous'))
        db2 = self.root / 'video-clear.sqlite'
        self.base(db2)
        clear = deepcopy(self.events['evb1'])
        clear.update(id='evb-clear', evidence_id='EVB-CLEAR')
        clear['order_marks'] = ['alight', 'alight', 'alight', 'board', 'board', 'board']
        self.send(clear, self.assess('PK-CLEAR'), db=db2)
        peak = self.peak(db2, 'PK-CLEAR')
        self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict']), (10, 10, 'overload_excluded'))
        for name, patch in (('occluded', {'occluded': True}), ('timecode', {'timecode_closed': False})):
            db3 = self.root / f'video-{name}.sqlite'
            self.base(db3)
            bad = deepcopy(self.events['evb1'])
            bad.update(id=f'evb-{name}', evidence_id=f'EVB-{name}'.upper(), **patch)
            self.send(bad, self.assess(f'PK-{name}'), db=db3)
            peak = self.peak(db3, f'PK-{name}')
            self.assertEqual((peak['peak_lo'], peak['peak_hi'], peak['verdict'], peak['coverage']),
                             (10, 13, 'pending_verification', 'partial'))

    def test_published_ruling_survives_late_door_data(self):
        self.send(self.events['bt1'], self.events['db1'], self.events['bl1'], self.events['cmp1'],
                  self.events['pk1'], self.events['rp1'])
        self.send(self.events['ds1'], self.events['ds2'], self.events['evb1'])
        self.send(self.events['pk3'])
        self.assertEqual(self.peak(self.db, 'PK3')['verdict'], 'overload_confirmed')
        rulings = report(self.db)['bus_complaint_ruling']
        self.assertEqual(len(rulings), 1)
        self.assertEqual((rulings[0]['verdict'], rulings[0]['peak_lo'], rulings[0]['peak_hi']),
                         ('pending_verification', 10, 13))
        self.send(self.events['rp2'])
        rulings = report(self.db)['bus_complaint_ruling']
        self.assertEqual([(r['revision'], r['verdict']) for r in rulings],
                         [(1, 'pending_verification'), (2, 'overload_confirmed')])
        self.assertEqual((rulings[0]['peak_lo'], rulings[0]['peak_hi']), (10, 13))
        self.assertEqual(rulings[1]['responsibility'], 'stop_boarding_management')
        self.assertTrue(audit(self.db)['replay_ok'])

    def test_video_evidence_stays_out_of_flow_samples(self):
        ingest(self.db, 'bus-import', ROOT / 'fixtures/bus-door-order.json')
        r = report(self.db)
        self.assertEqual(r['vehicle_sample'], [])
        self.assertEqual(r['demand'], [])

    def test_reboard_offset_requires_identity(self):
        self.base()
        seg = self.segment('DS-RB-L', 'left', ['alight', 'board'])
        seg['events'][1]['reboard_of'] = 'DS-RB-L#1'
        with self.assertRaisesRegex(ValueError, 'reboard_identity_required'):
            self.send(seg)
        seg['events'][1]['identity_ref'] = 'anon-ticket-7'
        self.send(seg)
        row = report(self.db)['bus_door_segment'][0]
        self.assertEqual((row['board_count'], row['alight_count']), (1, 1))

    def test_segment_totals_cannot_exceed_summary(self):
        self.base()
        self.send(self.segment('DS-X-L', 'left', ['board', 'board']),
                  self.segment('DS-X-R', 'right', ['board', 'board'],
                               times=['2026-01-02T08:10:03Z', '2026-01-02T08:10:05Z']))
        self.send(self.assess('PK-X'))
        peak = self.peak(self.db, 'PK-X')
        self.assertEqual(peak['verdict'], 'pending_verification')
        self.assertIn('segment_batch_mismatch', peak['reason'])

    def test_audit_detects_tampered_door_order(self):
        ingest(self.db, 'bus-import', ROOT / 'fixtures/bus-door-order.json')
        db = sqlite3.connect(self.db)
        payload = json.loads(db.execute('SELECT raw FROM source_file WHERE id=1').fetchone()[0])
        for event in payload['events']:
            if event['id'] == 'evb1':
                event['order_marks'] = ['alight', 'alight', 'alight', 'board', 'board', 'board']
        db.execute('UPDATE source_file SET raw=? WHERE id=1',
                   (json.dumps(payload, ensure_ascii=False).encode(),))
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertFalse(result['source_integrity'])
        self.assertFalse(result['replay_ok'])

    def test_audit_detects_tampered_event_record(self):
        ingest(self.db, 'bus-import', ROOT / 'fixtures/bus-door-order.json')
        db = sqlite3.connect(self.db)
        event = json.loads(db.execute("SELECT raw FROM event WHERE id='db1'").fetchone()[0])
        event['board_count'] = 2
        db.execute("UPDATE event SET raw=? WHERE id='db1'", (json.dumps(event).encode(),))
        db.commit()
        db.close()
        self.assertFalse(audit(self.db)['event_integrity'])


if __name__ == '__main__':
    unittest.main()
