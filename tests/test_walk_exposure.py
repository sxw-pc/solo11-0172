import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from signalphase.engine import audit, ingest, report
from signalphase.walk_exposure import linearly_dependent, mixture_bounds, nnls_point, nullspace

ROOT = Path(__file__).resolve().parent.parent

D = [4, 6, 2]
U = [7, 2, 5]
B = [5.5, 4, 3.5]
A3 = [list(col) for col in zip(D, U, B)]


def _ev(kind, **kw):
    ev = {'kind': kind, 'recorded_at': '2026-01-02T08:00:00Z'}
    ev.update(kw)
    return ev


class WalkExposureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.db = self.dir / 'walk.sqlite'
        self.n = 0

    def send(self, *events, command='walk-import'):
        self.n += 1
        p = self.dir / f'{self.n}.json'
        p.write_text(json.dumps({'events': list(events)}), encoding='utf-8')
        return ingest(self.db, command, p)

    def base(self, signatures=True, calibration=True, routes=True, fourth=True, reading=True):
        ev = []
        if signatures:
            ev += [
                _ev('air_signature', id='ws-d', signature_id='diesel', source_kind='diesel_exhaust',
                    channels=D, revision=1),
                _ev('air_signature', id='ws-u', signature_id='dust', source_kind='road_dust',
                    channels=U, revision=1),
                _ev('air_signature', id='ws-b', signature_id='bg', source_kind='secondary_background',
                    channels=B, revision=1),
            ]
        if calibration:
            ev.append(_ev('walk_calibration', id='wc-1', calibration_id='CAL-1',
                          batch_id='batch-2026-01-02', bias=[0, 0, 0, 0],
                          valid_from='2026-01-02T00:00:00Z', valid_to='2026-01-03T00:00:00Z', revision=1))
        if routes:
            ev += [
                _ev('walk_route', id='wr-e', route_id='east', name='east-via-gate', revision=1),
                _ev('walk_route', id='wr-w', route_id='west', name='west-bypass', revision=1),
                _ev('walk_segment', id='wseg-e1', segment_id='east-gate', route_id='east',
                    seq_no=1, length_m=900, walk_seconds=1200),
                _ev('walk_segment', id='wseg-w1', segment_id='west-north', route_id='west',
                    seq_no=1, length_m=540, walk_seconds=720),
                _ev('walk_segment', id='wseg-w2', segment_id='west-south', route_id='west',
                    seq_no=2, length_m=540, walk_seconds=720),
            ]
        if fourth:
            ev += [
                _ev('walk_source_channel4', id='wc4-d', signature_id='diesel',
                    fingerprint_version='fp-2026-01-02', value=9),
                _ev('walk_source_channel4', id='wc4-u', signature_id='dust',
                    fingerprint_version='fp-2026-01-02', value=1),
                _ev('walk_source_channel4', id='wc4-b', signature_id='bg',
                    fingerprint_version='fp-2026-01-02', value=3),
            ]
        if reading:
            ev.append(_ev('walk_reading', id='wr-e1', reading_id='WR-E1', segment_id='east-gate',
                          observed_at='2026-01-02T08:05:00Z', channels=B, wind_direction='north',
                          calibration_batch='batch-2026-01-02',
                          fingerprint_version='fp-2026-01-02', station_coverage='fixed-AO1'))
        self.send(*ev)

    def assess(self, assessment_id='WA-1', wind='north', batch='batch-2026-01-02',
               version='fp-2026-01-02', routes=('east', 'west')):
        return self.send(_ev('walk_assessment', id=assessment_id, assessment_id=assessment_id,
                             routes=list(routes), wind_direction=wind,
                             calibration_batch=batch, fingerprint_version=version))

    def recommendation(self):
        return report(self.db)['walk_recommendation'][-1]

    def exposure(self, segment):
        rows = report(self.db)['walk_exposure']
        return next(r for r in rows if r['segment_id'] == segment)

    # ------------------------------------------------------------------ #
    def test_ao1_category_changes_with_reference_library(self):
        # full library: AO1=[5,6,2] is nearest to diesel_exhaust at distance 1
        ingest(self.db, 'air-import', ROOT / 'fixtures/air-ok.json')
        r = report(self.db)
        adv = r['air_advisory'][0]
        self.assertEqual((adv['nearest_source'], adv['distance'], adv['state']),
                         ('diesel_exhaust', 1.0, 'provisional'))

        # library containing only road_dust=[7,2,5]: the same observation is
        # forced onto road_dust
        db2 = self.dir / 'dust-only.sqlite'
        dust_only = {'events': [
            {'id': 'as2', 'kind': 'air_signature', 'signature_id': 'dust',
             'source_kind': 'road_dust', 'channels': [7, 2, 5], 'revision': 1,
             'recorded_at': '2026-01-01T08:00:00Z'},
            {'id': 'ao1', 'kind': 'air_sample', 'sample_id': 'AO1', 'site_id': 'school-curb',
             'observed_at': '2026-01-02T08:05:00Z', 'channels': [5, 6, 2],
             'wind_direction': 'north', 'recorded_at': '2026-01-02T08:06:00Z'},
            {'id': 'ac1', 'kind': 'air_check', 'check_id': 'AC1', 'sample_id': 'AO1',
             'recorded_at': '2026-01-02T08:07:00Z'},
        ]}
        p = self.dir / 'dust.json'
        p.write_text(json.dumps(dust_only), encoding='utf-8')
        ingest(db2, 'air-import', p)
        adv2 = report(db2)['air_advisory'][0]
        self.assertEqual(adv2['nearest_source'], 'road_dust')
        self.assertAlmostEqual(adv2['distance'], (2 ** 2 + 4 ** 2 + 3 ** 2) ** 0.5)

    def test_vectors_linearly_dependent_with_exact_nullspace(self):
        self.assertTrue(linearly_dependent([D, U, B]))
        self.assertFalse(linearly_dependent([D, U]))
        self.assertEqual(nullspace(A3), [[-0.5, -0.5, 1.0]])
        # b is literally the mean of d and u
        for k in range(3):
            self.assertAlmostEqual(B[k], (D[k] + U[k]) / 2.0)

    def test_two_zero_residual_decompositions_give_different_east_integrals(self):
        lo, hi, vertices, _ = mixture_bounds(A3, B, 0)
        self.assertEqual(sorted(vertices), sorted([[0.0, 0.0, 1.0], [0.5, 0.5, 0.0]]))
        self.assertEqual((lo, hi), (0.0, 0.5))
        # east walk takes 20 minutes through the gate
        minutes = 20
        self.assertEqual((lo * minutes, hi * minutes), (0.0, 10.0))

        self.base()
        self.assess()
        rec = self.recommendation()
        east = json.loads(rec['route_bounds_json'])['east']
        self.assertEqual((east['lower'], east['upper']), (0.0, 10.0))
        # both zero-residual families survive: no single coefficient point is stored
        self.assertEqual(self.exposure('east-gate')['basis_state'], 'three_channel_bounded')
        # west has no raw observation at all
        self.assertEqual(self.exposure('west-north')['basis_state'], 'uncovered')
        self.assertEqual(rec['comparable'], 0)
        self.assertIsNone(rec['winner_route_id'])

        # the naive point-estimate implementation would have picked a winner:
        # vertex (0,0,1) says east carries no diesel and east is shorter, so a
        # collapsed NNLS point recommends east - the recommendation our product
        # refuses to publish while the family stays open
        point = nnls_point(A3, B)
        self.assertAlmostEqual(point[0], 0.0)

    def test_fourth_channel_continuous_coverage_resolves_route_choice(self):
        self.base()
        # mobile fourth channel runs continuously along BOTH routes. The gate
        # reading y4=5 equals (9+1)/2: a d/u mixture, not background; the west
        # segments read y4=3: pure secondary background, zero true diesel
        self.send(
            _ev('walk_channel4', id='c4-e1', reading_id='C4-E1', segment_id='east-gate',
                observed_at='2026-01-02T08:05:00Z', value=5, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w1', reading_id='C4-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', value=3, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w2', reading_id='C4-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', value=3, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-4')
        rec = self.recommendation()
        self.assertEqual(rec['comparable'], 1)
        # west wins even though it takes 24 minutes vs east's 20: exposure, not
        # length or raw channel totals, decides the route
        self.assertEqual(rec['winner_route_id'], 'west')
        bounds = json.loads(rec['route_bounds_json'])
        self.assertEqual(bounds['east'], {'lower': 10.0, 'upper': 10.0})
        self.assertEqual(bounds['west'], {'lower': 0.0, 'upper': 0.0})
        self.assertEqual(self.exposure('east-gate')['basis_state'], 'fourth_channel_resolved')

    def test_shielded_fourth_channel_segment_still_gives_bounds(self):
        self.base()
        # one west segment has a 3-channel reading but the mobile channel is
        # shielded there; the other stays fully uncovered
        self.send(
            _ev('walk_channel4', id='c4-e1', reading_id='C4-E1', segment_id='east-gate',
                observed_at='2026-01-02T08:05:00Z', value=5, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w1', reading_id='C4-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', value=3, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-shield')
        rec = self.recommendation()
        self.assertEqual(rec['comparable'], 0)
        self.assertIsNone(rec['winner_route_id'])
        uncovered = json.loads(rec['uncovered_json'])
        self.assertEqual(uncovered, {'west': ['west-south']})

    def test_filter_confirms_point_diesel_but_cannot_fill_west_route(self):
        self.base()
        self.send(_ev('walk_filter', id='wf-e1', filter_id='WF-E1', segment_id='east-gate',
                      observed_at='2026-01-02T08:10:00Z', diesel_confirmed=True,
                      fingerprint_version='fp-2026-01-02', detail={'chemistry': 'ec-marker'}))
        self.assess('WA-filter')
        rec = self.recommendation()
        # single-point chemistry cannot manufacture concentrations on unmeasured
        # west segments: route comparison stays open
        self.assertEqual(rec['comparable'], 0)
        self.assertIsNone(rec['winner_route_id'])
        gate = self.exposure('east-gate')
        self.assertEqual(gate['filter_finding'], 'diesel_confirmed')
        # the filter does not overwrite the three-channel feasible range
        self.assertEqual((gate['diesel_lower'], gate['diesel_upper']), (0.0, 10.0))
        self.assertEqual(self.exposure('west-north')['filter_finding'], None)

    def test_wind_reversal_excludes_upwind_mobile_samples(self):
        self.base()
        self.send(
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='south',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='south',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w1', reading_id='C4-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', value=3, wind_direction='south',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w2', reading_id='C4-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', value=3, wind_direction='south',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
        )
        # assessing under north wind must not merge the south-wind samples
        self.assess('WA-wind', wind='north')
        rec = self.recommendation()
        self.assertEqual(rec['comparable'], 0)
        self.assertIn('wind_regime_mismatch', rec['reason'])
        rows = report(self.db)
        self.assertTrue(all(r['state'] == 'excluded' and r['state_reason'] == 'wind_regime_mismatch'
                            for r in rows['walk_reading'] if r['id'].startswith('WR-W')))
        self.assertTrue(all(r['state'] == 'excluded'
                            for r in rows['walk_channel4'] if r['id'].startswith('C4-W')))
        self.assertEqual(json.loads(rec['uncovered_json']),
                         {'west': ['west-north', 'west-south']})

    def test_shared_calibration_batch_is_required(self):
        self.base()
        self.send(
            _ev('walk_calibration', id='wc-2', calibration_id='CAL-2', batch_id='batch-other',
                bias=[0, 0, 0, 0], valid_from='2026-01-02T00:00:00Z',
                valid_to='2026-01-03T00:00:00Z', revision=1),
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-other', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-other', fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-batch')
        rec = self.recommendation()
        self.assertEqual(rec['comparable'], 0)
        self.assertIn('calibration_batch_mismatch', rec['reason'])
        # an assessment naming an unregistered batch is rejected outright
        with self.assertRaisesRegex(ValueError, 'unknown_calibration_batch'):
            self.assess('WA-bad-batch', batch='batch-ghost')

    def test_fingerprint_version_mismatch_is_excluded(self):
        self.base()
        self.send(
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2025-12-01'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2025-12-01'),
        )
        self.assess('WA-ver')
        rec = self.recommendation()
        self.assertIn('fingerprint_version_mismatch', rec['reason'])
        self.assertEqual(rec['comparable'], 0)

    def test_calibration_bias_shifts_every_channel_uniformly(self):
        # a second batch with an additive instrument bias is applied before
        # decomposition; readings from differently calibrated instruments may
        # not be pooled into one assessment
        self.base(calibration=False, reading=False)
        self.send(
            _ev('walk_calibration', id='wc-1', calibration_id='CAL-1', batch_id='batch-biased',
                bias=[0.5, 0.0, 0.0, 1.0], valid_from='2026-01-02T00:00:00Z',
                valid_to='2026-01-03T00:00:00Z', revision=1),
            # instrument reads 0.5 high on channel 1, so true vector is B
            _ev('walk_reading', id='wr-e1', reading_id='WR-E1', segment_id='east-gate',
                observed_at='2026-01-02T08:05:00Z', channels=[6.0, 4.0, 3.5],
                wind_direction='north', calibration_batch='batch-biased',
                fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-bias', batch='batch-biased')
        gate = self.exposure('east-gate')
        # bias-corrected observation is again B -> diesel range [0, 0.5] * 20 min
        self.assertEqual((gate['diesel_lower'], gate['diesel_upper']), (0.0, 10.0))

    def test_inconsistent_reading_is_labelled_nnls_not_a_hard_family(self):
        self.base(reading=False)
        self.send(
            _ev('walk_reading', id='wr-e1', reading_id='WR-E1', segment_id='east-gate',
                observed_at='2026-01-02T08:05:00Z', channels=[100, 0, 0], wind_direction='north',
                calibration_batch='batch-2026-01-02',
                fingerprint_version='fp-2026-01-02', station_coverage='fixed-AO1'),
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-nnls')
        gate = self.exposure('east-gate')
        self.assertEqual(gate['basis_state'], 'nnls_point_estimate')
        self.assertIsNotNone(gate['diesel_upper'])

    def test_audit_replays_walk_ledger_and_detects_tampering(self):
        self.base()
        self.send(
            _ev('walk_channel4', id='c4-e1', reading_id='C4-E1', segment_id='east-gate',
                observed_at='2026-01-02T08:05:00Z', value=5, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w1', reading_id='WR-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_reading', id='wr-w2', reading_id='WR-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', channels=B, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w1', reading_id='C4-W1', segment_id='west-north',
                observed_at='2026-01-02T08:06:00Z', value=3, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
            _ev('walk_channel4', id='c4-w2', reading_id='C4-W2', segment_id='west-south',
                observed_at='2026-01-02T08:07:00Z', value=3, wind_direction='north',
                calibration_batch='batch-2026-01-02', fingerprint_version='fp-2026-01-02'),
        )
        self.assess('WA-final')
        result = audit(self.db)
        self.assertTrue(all(result[k] for k in ('replay_ok', 'source_integrity', 'event_integrity')))

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE source_file SET raw=? WHERE id=1", (b'{"events":[]}',))
        conn.commit()
        conn.close()
        tampered = audit(self.db)
        self.assertFalse(tampered['source_integrity'])
        self.assertFalse(tampered['replay_ok'])

    def test_fixture_replay_is_stable_and_advisories_are_never_overwritten(self):
        ingest(self.db, 'walk-import', ROOT / 'fixtures' / 'walk-air.json')
        rep = report(self.db)
        self.assertEqual(len(rep['walk_assessment']), 1)
        self.assertTrue(audit(self.db)['replay_ok'])
        # re-import is idempotent via source SHA
        again = ingest(self.db, 'walk-import', ROOT / 'fixtures' / 'walk-air.json')
        self.assertTrue(again['duplicate'])

        # nearest-neighbour air advisory produced by the old product remains a
        # raw recorded result: walk products never overwrite it with mixture
        # shares, and the two ledgers coexist in one database
        air_db = self.dir / 'air.sqlite'
        ingest(air_db, 'air-import', ROOT / 'fixtures' / 'air-ok.json')
        advisories = report(air_db)['air_advisory']
        self.assertEqual([(a['nearest_source'], a['state']) for a in advisories],
                         [('diesel_exhaust', 'provisional')])
        self.assertTrue(audit(air_db)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
