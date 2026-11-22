"""Volume-conservation attribution for a draining well.

A rising water level fixes only the *net* storage rate.  Writing the well as a
control volume over the whole level interval,

    inflow - outflow = net = area * (depth_end - depth_start) / hours

one equation with two unknowns.  This module enumerates the feasible
(inflow, outflow) pairs that satisfy it, together with the outlet capacity,
catchment boundaries and any evidence, and keeps the evidence gap that each
candidate still needs.  The net rate on its own is never reported as a
blockage volume or a discharge estimate.

Rules enforced here:

* Only a calibrated storage area turns a depth into a volume.
* Inflow and outflow are integrated over the *same* full interval.  A flow
  record that does not cover the interval is unusable; peaks from adjacent
  minutes are never spliced into a trajectory that never happened.
* Pump power is an operating condition, not a discharge, and is never
  counted as outflow.
* Radar rain intensity is converted to m3/h by effective area and loss
  coefficient before it can bound inflow.
"""
import json
from datetime import datetime

from .store import many

TOL = 0.02
CLEARING = ['jet_clearing_truck', 'clearing_crew']
STORAGE = ['temporary_storage']
INSPECT = ['pipe_inspection', 'portable_pump']


def _dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _r(value):
    value = round(float(value) + 0.0, 6)
    return 0.0 if value == 0 else value


def _covers(row, start, end):
    return _dt(row['from_at']) <= start and _dt(row['to_at']) >= end


def _hours(start, end):
    return (end - start).total_seconds() / 3600.0


def _sum(values):
    return _r(sum(values))


def _level_pair(db, well_id, level_from, level_to):
    first = db.execute('SELECT * FROM drain_level WHERE id=?', (level_from,)).fetchone()
    last = db.execute('SELECT * FROM drain_level WHERE id=?', (level_to,)).fetchone()
    if not first or not last:
        raise ValueError('unknown_drain_level')
    if first['well_id'] != well_id or last['well_id'] != well_id:
        raise ValueError('level_well_mismatch')
    if first['state'] != 'captured' or last['state'] != 'captured':
        raise ValueError('level_not_captured')
    if _dt(first['observed_at']) >= _dt(last['observed_at']):
        raise ValueError('level_order_invalid')
    return first, last


def _level_series(db, well_id, first, last):
    """All captured levels between the two originals, endpoints included.

    The whole span must be explained by one inflow/outflow pair.  If an
    intermediate level shows a different net storage rate, that pair cannot
    exist and the caller reports the trajectory as unexplained rather than
    switching outflow from minute to minute without evidence.
    """
    start, end = _dt(first['observed_at']), _dt(last['observed_at'])
    levels, excluded = [], []
    for row in many(db, 'SELECT * FROM drain_level WHERE well_id=? ORDER BY observed_at, id', (well_id,)):
        at = _dt(row['observed_at'])
        if not start <= at <= end:
            continue
        if row['state'] == 'captured':
            levels.append(row)
        else:
            excluded.append(row['id'])
    levels.sort(key=lambda row: (_dt(row['observed_at']), row['id']))
    return levels, excluded


def _physical(db, well_id):
    well = db.execute('SELECT * FROM drain_well WHERE id=?', (well_id,)).fetchone()
    if not well:
        raise ValueError('unknown_drain_well')
    outlets = many(db, 'SELECT * FROM drain_outlet WHERE well_id=? AND state=? ORDER BY outlet_id',
                   (well_id, 'open'))
    catchments = many(db, 'SELECT * FROM drain_catchment WHERE well_id=? ORDER BY catchment_id', (well_id,))
    return well, outlets, catchments


def _inflow_evidence(db, well_id, catchments, start, end, gaps):
    """Metered inflow and radar-derived inflow over the full interval."""
    branch = 0.0
    exact = None
    for row in many(db, "SELECT * FROM drain_flow WHERE well_id=? AND direction='inflow' ORDER BY flow_id",
                    (well_id,)):
        if not _covers(row, start, end):
            gaps.append({'kind': 'unusable_flow_window', 'm3h': _r(row['rate_m3h']),
                         'm3': _r(row['rate_m3h'] * _hours(start, end)),
                         'reason': 'flow_window_does_not_cover_level_interval'})
            continue
        if row['measures_total']:
            exact = _r(row['rate_m3h']) if exact is None else _r(exact + row['rate_m3h'])
        else:
            branch += row['rate_m3h']
    covered = set()
    rain = 0.0
    if catchments:
        marks = ','.join('?' * len(catchments))
        for row in many(db, 'SELECT * FROM drain_rainfall WHERE catchment_id IN (%s) ORDER BY rain_id' % marks,
                        tuple(c['catchment_id'] for c in catchments)):
            if not _covers(row, start, end):
                gaps.append({'kind': 'unusable_flow_window', 'm3h': _r(row['rate_m3h']),
                             'm3': _r(row['rate_m3h'] * _hours(start, end)),
                             'reason': 'rainfall_window_does_not_cover_level_interval'})
                continue
            covered.add(row['catchment_id'])
            rain += row['rate_m3h']
    if exact is not None:
        lower = _r(exact)
    else:
        lower = _r(branch + rain)
    return {'lower': lower, 'exact': exact, 'branch': _r(branch), 'rain': _r(rain),
            'covered': covered}


def _outflow_evidence(db, well_id, outlets, start, end, gaps):
    """Measured outflow and the capacity still unmeasured."""
    known = 0.0
    exact = None
    named = set()
    for row in many(db, "SELECT * FROM drain_flow WHERE well_id=? AND direction='outflow' ORDER BY flow_id",
                    (well_id,)):
        if not _covers(row, start, end):
            gaps.append({'kind': 'unusable_flow_window', 'm3h': _r(row['rate_m3h']),
                         'm3': _r(row['rate_m3h'] * _hours(start, end)),
                         'reason': 'flow_window_does_not_cover_level_interval'})
            continue
        if row['outlet_id']:
            named.add(row['outlet_id'])
        if row['measures_total']:
            exact = _r(row['rate_m3h']) if exact is None else _r(exact + row['rate_m3h'])
        else:
            known += row['rate_m3h']
    open_capacity = _sum(row['capacity_m3h'] for row in outlets if row['outlet_id'] not in named)
    if exact is not None:
        return {'low': exact, 'high': exact, 'measured': True, 'known': exact, 'open_capacity': 0.0,
                'measured_total': True}
    high = _r(known + open_capacity)
    return {'low': _r(known), 'high': high, 'measured': bool(named) and open_capacity <= TOL,
            'known': _r(known), 'open_capacity': _r(open_capacity), 'measured_total': False}


def _label(outflow, capacity, measured):
    if outflow <= TOL:
        return 'outlet_blocked'
    if capacity > TOL and abs(outflow - capacity) <= TOL:
        return 'outlet_at_capacity' if not measured else 'outlet_measured'
    return 'outlet_partial'


def _equipment(label):
    return list(STORAGE) if label != 'outlet_blocked' else list(CLEARING) + ['portable_pump']


def explain(db, well_id, level_from, level_to):
    """Return every feasible balance plus candidate-specific evidence gaps."""
    first, last = _level_pair(db, well_id, level_from, level_to)
    well, outlets, catchments = _physical(db, well_id)
    area = well['storage_area_m2']
    start, end = _dt(first['observed_at']), _dt(last['observed_at'])
    hours = _hours(start, end)
    delta_v = _r(area * (last['depth_m'] - first['depth_m']))
    net = _r(delta_v / hours)

    shared = []
    inflow = _inflow_evidence(db, well_id, catchments, start, end, shared)
    outflow = _outflow_evidence(db, well_id, outlets, start, end, shared)
    capacity = _sum(row['capacity_m3h'] for row in outlets)

    # Constrain outflow by conservation and the measured inflow floor.
    contradiction = False
    o_lo = max(outflow['low'], _r(inflow['lower'] - net))
    o_hi = outflow['high']
    if inflow['exact'] is not None:
        fixed = _r(inflow['exact'] - net)
        o_lo = o_hi = fixed
        if fixed < outflow['low'] - TOL or fixed > outflow['high'] + TOL:
            contradiction = True
            shared.append({'kind': 'conservation_inconsistent', 'm3h': fixed,
                           'm3': _r(fixed * hours),
                           'reason': 'metered_inflow_and_outlet_capacity_cannot_balance_storage'})
    elif o_lo > o_hi + TOL:
        contradiction = True
        shared.append({'kind': 'conservation_inconsistent', 'm3h': _r(o_lo - o_hi),
                       'm3': _r((o_lo - o_hi) * hours),
                       'reason': 'measured_inflow_floor_exceeds_outlet_capacity_plus_net'})
        o_lo = o_hi

    series, excluded_levels = _level_series(db, well_id, first, last)
    rates = []
    for previous, current in zip(series, series[1:]):
        span = _hours(_dt(previous['observed_at']), _dt(current['observed_at']))
        if span <= TOL:
            continue
        rates.append(_r(area * (current['depth_m'] - previous['depth_m']) / span))
    consistent = not rates or max(rates) - min(rates) <= TOL

    outflows = []
    for value in (o_lo, o_hi):
        value = _r(max(0.0, value))
        if not any(abs(value - v) <= TOL for v in outflows):
            outflows.append(value)

    candidates = []
    if contradiction:
        # Measured evidence contradicts conservation; no pair may be emitted.
        consistent = False
    elif not consistent:
        shared.append({'kind': 'trajectory_not_integrated', 'm3h': _r(max(rates) - min(rates)),
                       'm3': 0.0,
                       'reason': 'intermediate_levels_require_a_different_net_rate'})
    if consistent:
        for outflow_rate in outflows:
            inflow_rate = _r(outflow_rate + net)
            if inflow['exact'] is not None:
                inflow_rate = inflow['exact']
            measured = outflow['measured'] or outflow['measured_total']
            label = _label(outflow_rate, capacity, measured)
            gaps = []
            unattributed = _r(max(0.0, inflow_rate - inflow['lower']))
            if unattributed > TOL:
                gaps.append({'kind': 'inflow_unattributed', 'm3h': unattributed,
                             'm3': _r(unattributed * hours),
                             'reason': 'inflow_composition_or_magnitude_not_measured'})
            if not measured and outflow_rate > TOL:
                gaps.append({'kind': 'outflow_unmeasured', 'm3h': _r(outflow_rate),
                             'm3': _r(outflow_rate * hours),
                             'reason': 'outlet_not_measured'})
            if not measured and outflow_rate <= TOL and inflow['exact'] is None:
                gaps.append({'kind': 'blockage_unmeasured', 'm3h': _r(inflow_rate - inflow['lower']),
                             'm3': _r((inflow_rate - inflow['lower']) * hours),
                             'reason': 'outlet_blockage_inferred_not_measured'})
            for catchment in catchments:
                if catchment['catchment_id'] in inflow['covered']:
                    continue
                gaps.append({'kind': 'catchment_uncovered', 'm3h': 0.0, 'm3': 0.0,
                             'reason': 'catchment_%s_not_covered_by_evidence' % catchment['catchment_id']})
            candidates.append({
                'inflow_m3h': inflow_rate, 'outflow_m3h': _r(outflow_rate),
                'inflow_m3': _r(inflow_rate * hours), 'outflow_m3': _r(outflow_rate * hours),
                'storage_m3': delta_v, 'residual_m3': _r((inflow_rate - outflow_rate) * hours - delta_v),
                'label': label, 'measured_outflow': bool(measured),
                'equipment': _equipment(label), 'gaps': gaps,
                'outlets': sorted(row['outlet_id'] for row in outlets),
            })

    measured_outlets = sorted({row['outlet_id'] for row in many(
        db, "SELECT * FROM drain_flow WHERE well_id=? AND direction='outflow'", (well_id,))
        if row['outlet_id'] and _covers(row, start, end)})
    scope = {
        'outlets_measured': measured_outlets,
        'outlets_unmeasured': sorted(row['outlet_id'] for row in outlets
                                     if row['outlet_id'] not in measured_outlets),
        'catchments_covered': sorted(inflow['covered']),
        'catchments_uncovered': sorted(c['catchment_id'] for c in catchments
                                       if c['catchment_id'] not in inflow['covered']),
        'clearing_confirmed': [],
        'clearing_suspected': [],
        'storage_areas': [],
    }
    if candidates and all(c['outflow_m3h'] <= TOL for c in candidates):
        scope['clearing_suspected'] = sorted({o for c in candidates for o in c['outlets']})
        if all(c['measured_outflow'] for c in candidates):
            scope['clearing_confirmed'] = scope['clearing_suspected']
    elif candidates and all(c['outflow_m3h'] > TOL for c in candidates):
        scope['storage_areas'] = list(scope['catchments_uncovered']) or sorted(
            c['catchment_id'] for c in catchments)
    elif candidates:
        scope['clearing_suspected'] = sorted({o for c in candidates for o in c['outlets']})
        scope['storage_areas'] = list(scope['catchments_uncovered'])

    return {
        'well_id': well_id, 'level_from': level_from, 'level_to': level_to,
        'from_at': first['observed_at'], 'to_at': last['observed_at'],
        'storage_area_m2': area, 'delta_v_m3': delta_v, 'net_m3h': net,
        'inflow_bounds': {'lower_m3h': inflow['lower'], 'upper_m3h': _r(o_hi + net),
                          'exact_m3h': inflow['exact']},
        'outflow_bounds': {'low_m3h': _r(o_lo), 'high_m3h': _r(o_hi), 'capacity_m3h': _r(capacity)},
        'candidates': candidates, 'shared_gaps': shared, 'scope': scope,
    }


def decision(explanation, executed_clearing=False):
    """Aggregate feasible balances into one dispatch action."""
    candidates = explanation['candidates']
    blocked = [c for c in candidates if c['outflow_m3h'] <= TOL]
    flowing = [c for c in candidates if c['outflow_m3h'] > TOL]
    if not candidates:
        action, status, equipment, withdraw = 'inspect_and_pump', 'unsolved', list(INSPECT), []
        reason = 'no_feasible_balance'
    elif blocked and flowing:
        action, status, equipment, withdraw = 'inspect_and_pump', 'provisional', list(INSPECT), []
        reason = 'multiple_feasible_balances'
    elif blocked:
        equipment = list(CLEARING) + ['portable_pump']
        confirmed = all(c['measured_outflow'] for c in blocked)
        action = 'clear_outlet_and_pump'
        status = 'confirmed' if confirmed else 'provisional'
        withdraw = []
        reason = 'outlet_blocked_confirmed' if confirmed else 'outlet_blockage_hypothesis'
    else:
        equipment = list(STORAGE)
        confirmed = all(c['measured_outflow'] for c in flowing)
        action = 'detain_upstream'
        status = 'confirmed' if confirmed else 'provisional'
        withdraw = []
        reason = 'outlet_flow_accounts_for_rise' if confirmed else 'upstream_inflow_hypothesis'
        if not executed_clearing:
            withdraw = list(CLEARING)
            reason += '_withdraw_unexecuted_clearing'
    return {'action': action, 'status': status, 'equipment': equipment,
            'withdraw': withdraw, 'reason': reason}


def store_explanation(db, dispatch_id, revision, explanation, decision_row, at):
    basis = {
        'net_m3h': explanation['net_m3h'], 'delta_v_m3': explanation['delta_v_m3'],
        'from_at': explanation['from_at'], 'to_at': explanation['to_at'],
        'inflow_bounds': explanation['inflow_bounds'], 'outflow_bounds': explanation['outflow_bounds'],
        'scope': explanation['scope'],
        'candidate_equipment': [c['equipment'] for c in explanation['candidates']],
    }
    for index, candidate in enumerate(explanation['candidates'], 1):
        db.execute('INSERT INTO drain_balance VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (dispatch_id, revision, index, explanation['well_id'],
                    explanation['level_from'], explanation['level_to'],
                    candidate['inflow_m3h'], candidate['outflow_m3h'],
                    candidate['inflow_m3'], candidate['outflow_m3'],
                    candidate['storage_m3'], candidate['residual_m3'], candidate['label'],
                    1 if candidate['measured_outflow'] else 0,
                    json.dumps({'gaps': candidate['gaps'], 'outlets': candidate['outlets'],
                                'equipment': candidate['equipment']},
                               sort_keys=True, ensure_ascii=False)))
        for gap_no, gap in enumerate(candidate['gaps'], 1):
            db.execute('INSERT INTO drain_gap VALUES(?,?,?,?,?,?,?,?,?)',
                       (dispatch_id, revision, index, gap_no, explanation['well_id'],
                        gap['kind'], gap['m3h'], gap['m3'], gap['reason']))
    for gap_no, gap in enumerate(explanation['shared_gaps'], 1):
        db.execute('INSERT INTO drain_gap VALUES(?,?,?,?,?,?,?,?,?)',
                   (dispatch_id, revision, 0, gap_no, explanation['well_id'],
                    gap['kind'], gap['m3h'], gap['m3'], gap['reason']))
    db.execute('INSERT INTO drain_dispatch VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (dispatch_id, revision, explanation['well_id'], explanation['level_from'],
                explanation['level_to'], decision_row['action'],
                json.dumps(decision_row['equipment'], sort_keys=True, ensure_ascii=False),
                json.dumps(explanation['scope'], sort_keys=True, ensure_ascii=False),
                json.dumps(decision_row['withdraw'], sort_keys=True, ensure_ascii=False),
                decision_row['status'], decision_row['reason'], at,
                json.dumps(basis, sort_keys=True, ensure_ascii=False)))
