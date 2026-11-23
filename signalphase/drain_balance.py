"""Drainage anomaly attribution and crew equipment dispatch.

The solver never turns a rising shaft level directly into a jetting-truck
order.  For one well and one fixed window it:

1. turns the captured level series into a storage-volume change with the
   calibrated shaft area,
2. collects the inflow/outflow evidence whose time integral covers exactly
   that window (clamp-meter readings, metered pump discharge and radar
   rainfall converted through the effective catchment area and loss
   coefficient),
3. checks every registered candidate water balance against volume
   conservation and the evidence bounds, keeping *every* feasible balance
   plus the still-open attribution gaps, and
4. issues equipment only for what the evidence supports.  An energized pump
   is an operating condition, not outflow proof; a radar pixel is rainfall
   intensity, not cubic metres in the well.

The same set of inflow/outflow terms must explain the whole window and each
of its segments.  There is no per-minute swap to an unexplained outlet flow,
and no stitching of adjacent rainfall and discharge peaks into a trajectory
that never happened.  Late evidence creates a new dispatch revision; the
original visit, cleaning, pumping and discharged-volume documents are never
rewritten.
"""
import json
from datetime import datetime

from .store import one, many, log

TOL = 1e-9


def _at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _hours(from_at, to_at):
    return (_at(to_at) - _at(from_at)).total_seconds() / 3600.0


def catchment(db, e):
    if e['effective_area_m2'] <= 0 or not (0.0 < e['loss_coefficient'] <= 1.0):
        raise ValueError('invalid_catchment')
    state, reason = 'registered', 'catchment_boundary_calibrated'
    db.execute('INSERT INTO drain_catchment VALUES(?,?,?,?,?,?,?,?)',
               (e['catchment_id'], e['well_id'], e['name'], e['effective_area_m2'],
                e['loss_coefficient'], 1 if e['radar_covered'] else 0, state, reason))
    log(db, e, 'drain-balance', e['catchment_id'], state, reason)
    return state


def rainfall(db, e):
    row = one(db, 'SELECT * FROM drain_catchment WHERE id=?', (e['catchment_id'],))
    if not row:
        raise ValueError('unknown_catchment')
    if e['intensity_mh'] < 0:
        raise ValueError('invalid_rainfall')
    # Radar intensity is never stored as well inflow directly: it must pass
    # through effective area and the loss coefficient.
    inflow = round(e['intensity_mh'] * row['effective_area_m2'] * row['loss_coefficient'], 9)
    if row['radar_covered']:
        state, reason = 'converted', 'radar_rainfall_converted_by_area_and_loss'
    else:
        state, reason = 'excluded', 'radar_does_not_cover_catchment'
    db.execute('INSERT INTO drain_rainfall VALUES(?,?,?,?,?,?,?,?,?)',
               (e['rainfall_id'], e['catchment_id'], row['well_id'], e['from_at'], e['to_at'],
                e['intensity_mh'], inflow, state, reason))
    log(db, e, 'drain-balance', e['rainfall_id'], state, reason)
    return state


def flow(db, e):
    if e['direction'] not in ('inflow', 'outflow') or e['rate_m3h'] < 0:
        raise ValueError('invalid_flow')
    if not one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)):
        raise ValueError('unknown_drain_well')
    if _hours(e['from_at'], e['to_at']) <= 0:
        raise ValueError('invalid_flow_window')
    state, reason = 'measured', 'clamp_meter_window_integral'
    db.execute('INSERT INTO drain_flow VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['flow_id'], e['well_id'], e['point'], e['direction'], e['from_at'], e['to_at'],
                e['rate_m3h'], e['provenance'], state, reason))
    log(db, e, 'drain-balance', e['flow_id'], state, reason)
    return state


def pump_power(db, e):
    if not one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)):
        raise ValueError('unknown_drain_well')
    # Power only proves the pump *can* run; no outflow row is created.
    state, reason = 'energized', 'powered_operating_condition_no_outflow_proof'
    db.execute('INSERT INTO drain_pump VALUES(?,?,?,?,?,?)',
               (e['pump_id'], e['well_id'], e['outlet_id'], e['energized_at'], state, reason))
    log(db, e, 'drain-balance', e['pump_id'], state, reason)
    return state


def pump_receipt(db, e):
    pump = one(db, 'SELECT * FROM drain_pump WHERE id=?', (e['pump_id'],))
    if not pump:
        raise ValueError('unknown_pump')
    if e['volume_m3'] <= 0:
        raise ValueError('invalid_pump_receipt')
    hours = _hours(e['from_at'], e['to_at'])
    if hours <= 0:
        raise ValueError('invalid_pump_receipt_window')
    rate = round(e['volume_m3'] / hours, 9)
    state, reason = 'received', 'metered_discharge_volume_kept_original'
    db.execute('INSERT INTO drain_pump_receipt VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['receipt_id'], e['pump_id'], pump['well_id'], e['outlet_id'], e['from_at'],
                e['to_at'], e['volume_m3'], rate, state, reason))
    log(db, e, 'drain-balance', e['receipt_id'], state, reason)
    return state


def scenario(db, e):
    terms = e['terms']
    if not terms:
        raise ValueError('scenario_terms_required')
    for term in terms:
        if term['kind'] not in ('inflow', 'outflow') or term['rate_m3h'] < 0:
            raise ValueError('invalid_scenario_term')
    if _hours(e['from_at'], e['to_at']) <= 0:
        raise ValueError('invalid_scenario_window')
    state, reason = 'registered', 'candidate_balance_pending_conservation_check'
    db.execute('INSERT INTO drain_scenario VALUES(?,?,?,?,?,?,?,?,?)',
               (e['scenario_id'], e['well_id'], e['from_at'], e['to_at'], e['weather'],
                e['outlet_state'], json.dumps(terms, sort_keys=True), state, reason))
    log(db, e, 'drain-balance', e['scenario_id'], state, reason)
    return state


def work_receipt(db, e):
    dispatch = one(db, 'SELECT * FROM drain_dispatch WHERE id=?', (e['dispatch_id'],))
    if not dispatch:
        raise ValueError('unknown_dispatch')
    if e['work_hours'] < 0:
        raise ValueError('invalid_work_receipt')
    db.execute('INSERT INTO drain_work_receipt VALUES(?,?,?,?,?,?)',
               (e['receipt_id'], e['dispatch_id'], dispatch['well_id'], dispatch['action'],
                e['executed_at'], e['work_hours']))
    db.execute("UPDATE drain_dispatch SET state='executed', reason=? WHERE id=?",
               ('field_work_received_original_labour_kept', e['dispatch_id']))
    log(db, e, 'drain-balance', e['dispatch_id'], 'executed', 'field_work_received_original_labour_kept')
    return 'executed'


def _window_evidence(db, well_id, from_at, to_at):
    rain = many(db,
                'SELECT * FROM drain_rainfall WHERE well_id=? AND from_at=? AND to_at=? AND state=?',
                (well_id, from_at, to_at, 'converted'))
    flows = many(db, 'SELECT * FROM drain_flow WHERE well_id=? AND from_at=? AND to_at=?',
                 (well_id, from_at, to_at))
    receipts = many(db,
                    'SELECT * FROM drain_pump_receipt WHERE well_id=? AND from_at=? AND to_at=?',
                    (well_id, from_at, to_at))
    return rain, flows, receipts


def _series_rates(area, levels):
    """One net rate per adjacent level pair; the whole series must share it."""
    rates = []
    for left, right in zip(levels, levels[1:]):
        hours = _hours(left['observed_at'], right['observed_at'])
        if hours <= 0:
            raise ValueError('invalid_level_series')
        rates.append(area * (right['depth_m'] - left['depth_m']) / hours)
    return rates


def balance(db, e):
    levels = [one(db, 'SELECT * FROM drain_level WHERE id=?', (level_id,))
              for level_id in e['level_ids']]
    if any(row is None for row in levels):
        raise ValueError('unknown_drain_level')
    well_id = levels[0]['well_id']
    if any(row['well_id'] != well_id for row in levels) or len(levels) < 2:
        raise ValueError('balance_levels_invalid')
    if any(row['state'] != 'captured' for row in levels):
        raise ValueError('balance_levels_not_valid')
    levels = sorted(levels, key=lambda row: row['observed_at'])
    well = one(db, 'SELECT * FROM drain_well WHERE id=?', (well_id,))
    from_at, to_at = levels[0]['observed_at'], levels[-1]['observed_at']
    window_hours = _hours(from_at, to_at)
    change = round(well['storage_area_m2'] * (levels[-1]['depth_m'] - levels[0]['depth_m']), 9)
    net_rate = round(change / window_hours, 9)

    segment_rates = _series_rates(well['storage_area_m2'], levels)
    series_ok = all(abs(rate - net_rate) <= TOL for rate in segment_rates)

    rain, flows, receipts = _window_evidence(db, well_id, from_at, to_at)
    meter_inflow = round(sum(row['rate_m3h'] for row in flows if row['direction'] == 'inflow'), 9)
    meter_outflow = round(sum(row['rate_m3h'] for row in flows if row['direction'] == 'outflow'), 9)
    pumped_outflow = round(sum(row['rate_m3h'] for row in receipts), 9)
    known_inflow = round(meter_inflow + sum(row['inflow_m3h'] for row in rain), 9)
    known_outflow = round(meter_outflow + pumped_outflow, 9)
    has_outflow_proof = bool(flows and any(r['direction'] == 'outflow' for r in flows)) or bool(receipts)

    scenarios = many(db,
                     'SELECT * FROM drain_scenario WHERE well_id=? AND from_at=? AND to_at=?',
                     (well_id, from_at, to_at))
    solutions = []
    for candidate in scenarios:
        terms = json.loads(candidate['terms_json'])
        inflow = round(sum(t['rate_m3h'] for t in terms if t['kind'] == 'inflow'), 9)
        outflow = round(sum(t['rate_m3h'] for t in terms if t['kind'] == 'outflow'), 9)
        reasons = []
        # One term set must explain the whole window and every segment.  When
        # the segment rates diverge, no constant inflow/outflow pair survives.
        if not series_ok:
            reasons.append('no_single_term_set_explains_all_segments')
        if abs((inflow - outflow) - net_rate) > TOL:
            reasons.append('violates_volume_conservation')
        if has_outflow_proof and abs(outflow - known_outflow) > TOL:
            reasons.append('conflicts_with_measured_outflow')
        if inflow + TOL < known_inflow:
            reasons.append('below_evidence_lower_bound_inflow')
        if meter_inflow > 0 and abs(inflow - known_inflow) > TOL:
            reasons.append('conflicts_with_measured_inflow')
        feasible = not reasons
        solutions.append({'scenario': candidate, 'inflow': inflow, 'outflow': outflow,
                          'feasible': feasible,
                          'state': 'feasible' if feasible else 'rejected',
                          'reason': 'conservation_and_evidence_satisfied' if feasible
                          else ';'.join(reasons)})

    revision = one(db, 'SELECT COUNT(*) AS n FROM drain_balance WHERE well_id=?', (well_id,))['n'] + 1
    feasible = [s for s in solutions if s['feasible']]
    if not series_ok:
        state, reason = 'inconsistent_series', 'segment_rates_differ_no_single_term_set_explains_window'
    elif not feasible:
        state, reason = 'unattributed', 'no_candidate_balance_survives_conservation_and_evidence'
    else:
        state, reason = 'balanced', f'{len(feasible)}_feasible_balances_retained'

    inflow_gap = round(max((s['inflow'] for s in feasible), default=0.0) - known_inflow, 9)
    db.execute('INSERT INTO drain_balance VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['balance_id'], well_id, from_at, to_at, len(levels), change, net_rate,
                known_inflow, known_outflow, inflow_gap, revision, state, reason))
    for solution in solutions:
        candidate = solution['scenario']
        db.execute('INSERT INTO drain_balance_solution VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (e['balance_id'], candidate['id'], candidate['weather'],
                    candidate['outlet_state'], solution['inflow'], solution['outflow'],
                    1 if solution['feasible'] else 0, solution['state'], solution['reason'],
                    candidate['terms_json']))
    gaps = _gaps(db, e['balance_id'], well_id, feasible, rain, has_outflow_proof,
                 known_inflow, inflow_gap)
    if series_ok:
        _dispatch(db, e['balance_id'], well_id, revision, feasible, has_outflow_proof,
                  known_outflow, gaps, bool(rain) or meter_inflow > 0)
    log(db, e, 'drain-balance', e['balance_id'], state, reason)
    return state


def _gaps(db, balance_id, well_id, feasible, rain, has_outflow_proof, known_inflow, inflow_gap):
    gaps = []
    if not has_outflow_proof:
        for solution in feasible:
            terms = [t for t in json.loads(solution['scenario']['terms_json'])
                     if t['kind'] == 'outflow']
            source = terms[0]['point'] if terms else 'outlet'
            gaps.append(('outflow_unverified', source, solution['outflow'],
                         'outlet_flow_not_measured_pump_power_is_not_outflow_proof'))
    if feasible and rain:
        for row in many(db, 'SELECT * FROM drain_catchment WHERE well_id=?', (well_id,)):
            if not row['radar_covered']:
                gaps.append(('inflow_uncovered', row['id'], inflow_gap,
                             'radar_misses_subcatchment_total_inflow_still_unattributed'))
    elif feasible:
        gaps.append(('inflow_branch_unlocated', 'all-catchments', inflow_gap,
                     'total_inflow_implied_but_no_source_evidence_locates_a_branch_ditch'))
    for kind, source, missing, note in gaps:
        db.execute('INSERT OR IGNORE INTO drain_balance_gap VALUES(?,?,?,?,?)',
                   (balance_id, kind, source, missing, note))
    return gaps


def _dispatch(db, balance_id, well_id, revision, feasible, has_outflow_proof,
              known_outflow, gaps, has_inflow_evidence):
    # A new revision never deletes executed labour; unexecuted orders are
    # withdrawn (cleaning) or superseded by the new evidence revision.
    for old in many(db, 'SELECT * FROM drain_dispatch WHERE well_id=?', (well_id,)):
        if old['state'] == 'executed':
            continue
        new_state = 'withdrawn' if old['action'] == 'cleaning' else 'superseded'
        db.execute('UPDATE drain_dispatch SET state=?, reason=? WHERE id=?',
                   (new_state, f'superseded_by_balance_revision_{revision}', old['id']))
    if not feasible:
        return
    if has_outflow_proof:
        if known_outflow > TOL:
            # Outlet is proven open: store the upstream storm flow, never jet.
            for row in many(db, 'SELECT * FROM drain_catchment WHERE well_id=?', (well_id,)):
                db.execute('INSERT INTO drain_dispatch VALUES(?,?,?,?,?,?,?,?,?)',
                           (f'{balance_id}-retention-{row["id"]}', balance_id, well_id, revision,
                            'retention', row['id'], 'issued',
                            'outlet_confirmed_open_short_term_upstream_retention', ''))
        else:
            # A meter reading of zero is positive blockage evidence.
            db.execute('INSERT INTO drain_dispatch VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{balance_id}-cleaning', balance_id, well_id, revision,
                        'cleaning', well_id, 'issued',
                        'outlet_confirmed_blocked_clean_outlet', 'outflow_measured_zero'))
            db.execute('INSERT INTO drain_dispatch VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{balance_id}-portable-pump', balance_id, well_id, revision,
                        'portable_pump', well_id, 'issued',
                        'temporary_pumping_while_outlet_blocked', 'outflow_measured_zero'))
    elif has_inflow_evidence:
        # Route B: at least one inflow bound is evidenced while the outlet is
        # neither located as blocked nor proven open.  Temporary drainage and
        # pipe inspection only; no located-blockage cleaning order is signed.
        for action, reason in (
                ('portable_pump', 'outlet_unverified_temporary_drainage'),
                ('pipeline_inspection', 'outlet_unverified_locate_blockage_by_inspection')):
            db.execute('INSERT INTO drain_dispatch VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{balance_id}-{action}', balance_id, well_id, revision,
                        action, well_id, 'issued', reason, 'outflow_unverified'))
    # With neither inflow nor outflow evidence the ledger keeps the open gaps
    # and signs no equipment at all: a rising level alone is no dispatch basis.
