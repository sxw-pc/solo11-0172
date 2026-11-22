import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report
from signalphase.ice import surface_temperature, temperature_range

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'ice-time.json'

AT_0600 = '2026-01-02T06:00:00Z'
AT_0610 = '2026-01-02T06:10:00Z'
AT_0620 = '2026-01-02T06:20:00Z'


class IcePeriodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / 'ice.sqlite'
        self.counter = 0
        self.seq = 0
        self.events = json.loads(FIXTURE.read_text(encoding='utf-8'))['events']

    def send(self, *events):
        self.counter += 1
        path = self.dir / f'{self.counter}.json'
        path.write_text(json.dumps({'events': list(events)}, ensure_ascii=False), encoding='utf-8')
        return ingest(self.db, 'ice-import', path)

    def base(self):
        return ingest(self.db, 'ice-import', FIXTURE)

    def rows(self, table):
        return report(self.db)[table]

    def _id(self, prefix):
        self.seq += 1
        return f'{prefix}-{self.seq}'

    def certificate(self, emissivity=0.9, retained_at='2025-12-20T00:00:00Z'):
        return {'id': self._id('ice-cert'), 'kind': 'certificate', 'certificate_id': self._id('cert'),
                'lane_id': 'north-lane', 'cycle_id': 'cycle-2026-01', 'emissivity': emissivity,
                'retained_at': retained_at, 'recorded_at': '2026-01-03T09:00:00Z'}

    def treatment(self, at='2026-01-02T06:15:00Z'):
        return {'id': self._id('ice-tr'), 'kind': 'treatment', 'treatment_id': self._id('tr'),
                'lane_id': 'north-lane', 'at': at, 'material': 'anti_skid_grit',
                'actor': 'road-crew-2', 'recorded_at': at}

    def contact(self, temperature=1.0, water=False, at=AT_0620):
        return {'id': self._id('ice-cm'), 'kind': 'contact', 'measure_id': self._id('cm'),
                'lane_id': 'north-lane', 'measured_at': at, 'temperature_c': temperature,
                'water_present': water, 'recorded_at': at}

    def reopen(self, from_at, basis):
        return {'id': self._id('ice-ro'), 'kind': 'reopen', 'reopen_id': self._id('ro'),
                'lane_id': 'north-lane', 'from_at': from_at, 'basis': basis,
                'recorded_at': '2026-01-02T06:25:00Z'}

    def rain(self, at='2026-01-02T07:00:00Z'):
        return {'id': self._id('ice-w'), 'kind': 'water', 'water_id': self._id('w'),
                'lane_id': 'north-lane', 'observed_at': at, 'state': 'present',
                'method': 'patrol_visual', 'recorded_at': at}

    # --- calibration model and the two emissivity worlds ---

    def test_calibration_model_inverts_brightness(self):
        # duty-desk reference values for SF1 (B=-2, S=-15) and SF2 (B=-1, S=-15)
        self.assertAlmostEqual(surface_temperature(-2, -15, 0.85), 0.2941, places=3)
        self.assertAlmostEqual(surface_temperature(-2, -15, 0.9), -0.5556, places=3)
        self.assertAlmostEqual(surface_temperature(-1, -15, 0.85), 1.4706, places=3)
        self.assertAlmostEqual(surface_temperature(-1, -15, 0.9), 0.5556, places=3)
        low, high = temperature_range(-2, -15, 0.85, 0.9)
        self.assertLess(low, 0)
        self.assertGreater(high, 0)
        low, high = temperature_range(-1, -15, 0.85, 0.9)
        self.assertGreater(low, 0)

    def test_same_frames_keep_both_emissivity_worlds_open(self):
        result = self.base()
        self.assertEqual(result['decisions'][-1]['outcome'], 'possible_ice')
        r = report(self.db)
        # identical evidence in both worlds: negative brightness only raised a conservative watch
        self.assertEqual([w['state'] for w in r['lane_watch']], ['watch', 'watch'])
        frames = {f['frame_id']: f for f in r['ice_frame']}
        sf1, sf2 = frames['SF1'], frames['SF2']
        # 06:00: the range crosses freezing, so the record stays possible_ice;
        # a naive B<0 rule would have fabricated a confirmed ice record here
        self.assertEqual(sf1['state'], 'possible_ice')
        self.assertAlmostEqual(sf1['t_min'], -0.5556, places=3)
        self.assertAlmostEqual(sf1['t_max'], 0.2941, places=3)
        # 06:10: every admissible emissivity is above freezing although B<0 still
        self.assertEqual(sf2['state'], 'no_ice')
        self.assertGreater(sf2['t_min'], 0)
        window = r['ice_window'][0]
        self.assertEqual(window['state'], 'possible_ice')
        self.assertEqual(window['responsibility'], 'undetermined')
        self.assertEqual(window['danger_from'], AT_0600)
        # the provable melt boundary is the interval between the two observations
        self.assertEqual((window['melt_from'], window['melt_to']), (AT_0600, AT_0610))
        self.assertLess(window['melt_from'], window['melt_to'])
        todo = r['ice_todo'][0]
        self.assertEqual((todo['kind'], todo['state']), ('emissivity_certificate', 'pending'))

    # --- station validity drives the frame -> check -> assessment chain ---

    def test_station_validity_drives_frame_and_check_chain(self):
        self.base()
        r = report(self.db)
        self.assertEqual([f['state'] for f in r['surface_frame']], ['captured', 'captured'])
        self.assertEqual([w['state'] for w in r['lane_watch']], ['watch', 'watch'])
        self.assertEqual(r['lane_watch'][0]['reason'], 'negative_brightness_temperature')

    def test_station_gap_excludes_frame_holds_check_and_limits_assessment(self):
        events = deepcopy(self.events)
        for e in events:
            if e['kind'] == 'station':
                e['valid_from'] = '2026-01-02T06:05:00Z'
        self.send(*events)
        r = report(self.db)
        self.assertEqual([f['state'] for f in r['surface_frame']], ['excluded', 'captured'])
        self.assertEqual([w['state'] for w in r['lane_watch']], ['held', 'watch'])
        self.assertEqual(r['lane_watch'][0]['reason'], 'uncalibrated_surface_frame')
        # the excluded 06:00 frame yields no conclusion at all, not a clearance
        self.assertEqual([f['frame_id'] for f in r['ice_frame']], ['SF2'])
        window = r['ice_window'][0]
        self.assertEqual(window['state'], 'no_ice')
        self.assertIn('excluded_frame_gap', window['reason'])
        self.assertIsNone(window['danger_from'])

    def test_station_invalid_for_all_frames_blocks_assessment(self):
        events = deepcopy(self.events)
        for e in events:
            if e['kind'] == 'station':
                e['valid_to'] = AT_0600
        head, assess_event = events[:-1], events[-1]
        self.send(*head)
        with self.assertRaisesRegex(ValueError, 'no_captured_frames'):
            self.send(assess_event)
        r = report(self.db)
        self.assertEqual([f['state'] for f in r['surface_frame']], ['excluded', 'excluded'])
        self.assertEqual([w['state'] for w in r['lane_watch']], ['held', 'held'])
        self.assertEqual(r['ice_window'], [])

    def test_assess_requires_registered_coating(self):
        events = [e for e in deepcopy(self.events) if e['kind'] != 'coating']
        with self.assertRaisesRegex(ValueError, 'unknown_coating_cycle'):
            self.send(*events)
        self.assertEqual(self.rows('ice_window'), [])

    # --- route A: late original certificate revises history ---

    def test_certificate_pins_one_emissivity_for_the_whole_cycle(self):
        self.base()
        result = self.send(self.certificate(0.9))
        self.assertEqual(result['decisions'][0]['outcome'], 'history_revised')
        r = report(self.db)
        frames = {f['frame_id']: f for f in r['ice_frame']}
        # the same certified value decides both moments, never one value per frame
        self.assertEqual(frames['SF1']['state'], 'ice')
        self.assertAlmostEqual(frames['SF1']['t_min'], -0.5556, places=3)
        self.assertEqual(frames['SF1']['t_min'], frames['SF1']['t_max'])
        self.assertEqual(frames['SF2']['state'], 'no_ice')
        self.assertAlmostEqual(frames['SF2']['t_min'], 0.5556, places=3)
        window = r['ice_window'][0]
        self.assertEqual(window['state'], 'confirmed_ice')
        self.assertEqual(window['danger_from'], AT_0600)
        # even with the certificate the melt moment stays the bounded interval
        self.assertEqual((window['melt_from'], window['melt_to']), (AT_0600, AT_0610))
        # closure at 06:05 came after the danger started at 06:00
        self.assertEqual(window['responsibility'], 'maintenance_gap')
        self.assertEqual(r['ice_todo'][0]['state'], 'fulfilled')
        # revision history keeps both versions; nothing is rewritten silently
        self.assertEqual([h['state'] for h in r['ice_frame_history'] if h['frame_id'] == 'SF1'],
                         ['possible_ice', 'ice'])
        self.assertEqual([w['revision'] for w in r['ice_window_history']], [1, 2])

    def test_certificate_other_world_clears_history(self):
        self.base()
        self.send(self.certificate(0.85))
        r = report(self.db)
        window = r['ice_window'][0]
        self.assertEqual(window['state'], 'no_ice')
        self.assertEqual(window['responsibility'], 'none')
        self.assertIsNone(window['danger_from'])
        self.assertEqual({f['state'] for f in r['ice_frame']}, {'no_ice'})

    def test_certificate_must_match_sample_and_predate_footage(self):
        self.base()
        with self.assertRaisesRegex(ValueError, 'certificate_outside_sample_range'):
            self.send(self.certificate(0.95))
        with self.assertRaisesRegex(ValueError, 'certificate_not_predating_footage'):
            self.send(self.certificate(0.9, retained_at='2026-01-02T06:30:00Z'))
        r = report(self.db)
        self.assertEqual(r['emissivity_certificate'], [])
        self.assertEqual(r['ice_window'][0]['state'], 'possible_ice')

    def test_late_original_never_deletes_executed_records(self):
        self.base()
        self.send(self.certificate(0.9))
        self.send(self.reopen(AT_0610, 'certificate_melt_boundary'))
        r = report(self.db)
        self.assertEqual([c['id'] for c in r['lane_closure']], ['cl-1'])
        self.assertEqual(r['lane_closure'][0]['closed_at'], '2026-01-02T06:05:00Z')
        self.assertEqual([d['id'] for d in r['lane_detour']], ['det-1'])
        self.assertEqual(r['lane_reopen'][0]['state'], 'published')

    def test_certificate_route_reopens_from_melt_boundary(self):
        self.base()
        unconfirmed = self.send(self.reopen(AT_0610, 'certificate_melt_boundary'))
        self.assertEqual(unconfirmed['decisions'][0]['outcome'], 'held')
        self.assertEqual(self.rows('lane_reopen')[0]['reason'], 'certificate_not_applied')
        self.send(self.certificate(0.9))
        early = self.send(self.reopen('2026-01-02T06:05:00Z', 'certificate_melt_boundary'))
        self.assertEqual(early['decisions'][0]['outcome'], 'held')
        self.assertEqual(self.rows('lane_reopen')[1]['reason'], 'melt_boundary_not_reached')
        published = self.send(self.reopen(AT_0610, 'certificate_melt_boundary'))
        self.assertEqual(published['decisions'][0]['outcome'], 'published')
        self.assertEqual(self.rows('lane_reopen')[2]['from_at'], AT_0610)

    # --- route B: contact measurements support reopening but never rewrite history ---

    def test_contact_route_publishes_reopen_without_rewriting_history(self):
        self.base()
        # no anti-skid treatment yet: the reopening is held
        early = self.send(self.reopen('2026-01-02T06:15:00Z', 'contact_measurement'))
        self.assertEqual(early['decisions'][0]['outcome'], 'held')
        self.assertEqual(self.rows('lane_reopen')[0]['reason'], 'anti_skid_treatment_required')
        self.send(self.treatment())
        dry = self.send(self.contact(1.0, water=False))
        self.assertEqual(dry['decisions'][0]['outcome'], 'safe')
        # the 06:20 measurement cannot backdate a reopening to 06:15
        backdated = self.send(self.reopen('2026-01-02T06:15:00Z', 'contact_measurement'))
        self.assertEqual(backdated['decisions'][0]['outcome'], 'held')
        self.assertEqual(self.rows('lane_reopen')[1]['reason'], 'no_safe_contact_measurement')
        published = self.send(self.reopen(AT_0620, 'contact_measurement'))
        self.assertEqual(published['decisions'][0]['outcome'], 'published')
        r = report(self.db)
        self.assertEqual(r['lane_reopen'][2]['from_at'], AT_0620)
        # future measurements never rewrite the historical assessment
        window = r['ice_window'][0]
        self.assertEqual((window['state'], window['revision']), ('possible_ice', 1))
        self.assertEqual({f['frame_id']: f['state'] for f in r['ice_frame']},
                         {'SF1': 'possible_ice', 'SF2': 'no_ice'})
        # the missing certificate stays an open supplementary-evidence todo
        self.assertEqual(r['ice_todo'][0]['state'], 'pending')

    def test_unsafe_contact_cannot_support_reopen(self):
        self.base()
        self.send(self.treatment())
        self.send(self.contact(-0.5, water=True))
        result = self.send(self.reopen(AT_0620, 'contact_measurement'))
        self.assertEqual(result['decisions'][0]['outcome'], 'held')
        self.assertEqual(self.rows('lane_reopen')[0]['reason'], 'no_safe_contact_measurement')

    def test_new_water_film_suspends_reopening(self):
        self.base()
        self.send(self.treatment())
        self.send(self.contact(1.0, water=False))
        self.send(self.reopen(AT_0620, 'contact_measurement'))
        self.send(self.rain())
        r = report(self.db)
        # the executed reopening is kept on record but no longer valid
        reopen = r['lane_reopen'][0]
        self.assertEqual(reopen['state'], 'superseded')
        self.assertEqual(reopen['reason'], 'new_water_film_requires_recheck')
        todos = {(t['kind'], t['state']) for t in r['ice_todo']}
        self.assertIn(('ice_reassessment', 'pending'), todos)
        self.assertIn(('emissivity_certificate', 'pending'), todos)
        self.assertEqual(r['ice_window'][0]['state'], 'possible_ice')

    # --- audit: tampered evidence or tampered conclusions are both caught ---

    def test_full_package_replays_clean(self):
        self.base()
        self.send(self.certificate(0.9))
        self.send(self.reopen(AT_0610, 'certificate_melt_boundary'))
        self.send(self.treatment())
        self.send(self.contact(1.0, water=False))
        self.send(self.reopen(AT_0620, 'contact_measurement'))
        self.send(self.rain())
        result = audit(self.db)
        self.assertTrue(all(result[k] for k in ('source_integrity', 'event_integrity', 'replay_ok')))

    def test_tampered_source_fails_integrity(self):
        self.base()
        db = sqlite3.connect(self.db)
        db.execute('UPDATE source_file SET raw=? WHERE id=1', (b'{"events":[]}',))
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertFalse(result['source_integrity'])
        self.assertFalse(result['replay_ok'])

    def test_tampered_conclusion_projection_fails_replay(self):
        self.base()
        self.send(self.certificate(0.9))
        db = sqlite3.connect(self.db)
        db.execute("UPDATE ice_window SET state='no_ice', responsibility='none'")
        db.commit()
        db.close()
        result = audit(self.db)
        self.assertTrue(result['source_integrity'])
        self.assertTrue(result['event_integrity'])
        self.assertFalse(result['replay_ok'])


if __name__ == '__main__':
    unittest.main()
