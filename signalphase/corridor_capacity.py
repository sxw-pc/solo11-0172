"""Short-link reserved capacity and cross-junction executable windows.

A hospital shuttle crossing junction A must immediately cross the eastern
junction B.  Each controller's successful readback alone does not prove the
vehicle can cross the short link between them: the link has finite capacity
and only receipted entry/exit events make its occupancy verifiable.  This
module keeps, in one replayable timeline, the A/B field readback versions,
the vehicle/pedestrian windows, B's green window, the shuttle arrival
interval, per-moment link occupancy and the upstream-release /
downstream-departure receipts, and signs 'guaranteed', 'conditional' or
'unavailable' together with explicit evidence coverage.  Plan approval is
never substituted for field state, and pedestrian clearance is never
compressed by any priority rule.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from .store import one, many, log

GUARANTEED = 'guaranteed'
CONDITIONAL = 'conditional'
UNAVAILABLE = 'unavailable'


def _parse(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _activation_at(db, controller_id, at):
    """Field activation covering moment `at` (same semantics as corridor.check)."""
    return one(db, 'SELECT * FROM activation WHERE controller_id=? AND started_at<=? '
               'AND (ended_at IS NULL OR ended_at>?) ORDER BY id DESC LIMIT 1',
               (controller_id, at, at))


def _ped_guard(plan):
    """Pedestrian clearance taken from the readback-version plan row.

    No priority rule may compress it: if the stored plan row falls below the
    safe-clearance requirement the joint window is refused instead of signed.
    """
    if not plan:
        return None
    required = math.ceil(plan['ped_distance_m'] / 0.8)
    return {'required_sec': required, 'readback_sec': plan['ped_green_sec'],
            'compressed': plan['ped_green_sec'] < required}


def _vehicle_intervals(db, link_id):
    """(vehicle, enter_at, exit_at|None) per receipted link events, in order."""
    events = many(db, 'SELECT * FROM link_event WHERE link_id=? ORDER BY at,rowid', (link_id,))
    open_enter = {}
    intervals = []
    for ev in events:
        if ev['direction'] == 'enter':
            open_enter[ev['vehicle']] = ev['at']
        else:
            start = open_enter.pop(ev['vehicle'], None)
            intervals.append((ev['vehicle'], start, ev['at']))
    intervals += [(vehicle, start, None) for vehicle, start in open_enter.items()]
    return intervals


def _occupancy(intervals, moment):
    return sum(1 for _, start, exit_ in intervals
               if _parse(start) <= moment and (exit_ is None or _parse(exit_) > moment))


def _full_segments(intervals, capacity):
    """Time segments where occupancy >= capacity; end None means unbounded."""
    deltas = {}
    for _, start, exit_ in intervals:
        deltas[start] = deltas.get(start, 0) + 1
        if exit_ is not None:
            deltas[exit_] = deltas.get(exit_, 0) - 1
    times = sorted(deltas)
    segments = []
    occ = 0
    for i, t in enumerate(times):
        occ += deltas[t]
        following = times[i + 1] if i + 1 < len(times) else None
        if occ >= capacity:
            segments.append([_parse(t), _parse(following) if following else None])
    merged = []
    for start, end in segments:
        if merged and merged[-1][1] == start:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _conflicts(t0, t1, full):
    """True if shuttle occupying [t0, t1] meets a capacity-full segment."""
    for f0, f1 in full:
        if f1 is not None and t0 >= f1:
            continue
        if f0 <= t1:
            return True
    return False


def _green_span(a_from, a_to, b_from, b_to, tmin, tmax):
    """Release moments whose whole arrival interval fits both green windows."""
    lo = max(_parse(a_from), _parse(b_from) - timedelta(seconds=tmin))
    hi = min(_parse(a_to), _parse(b_to) - timedelta(seconds=tmax))
    return lo, hi


def _capacity_spans(lo, hi, full, tmax_sec):
    """Green span minus release moments blocked by capacity-full segments.

    A release at t keeps the shuttle on the link during [t, t+tmax], so a
    full segment [f0, f1) blocks every release in [f0-tmax, f1).
    """
    spans = [[lo, hi]]
    for f0, f1 in full:
        b0 = f0 - timedelta(seconds=tmax_sec)
        b1 = f1
        nxt = []
        for s, e in spans:
            if (b1 is not None and b1 <= s) or b0 >= e:
                nxt.append([s, e])
                continue
            if b0 > s:
                nxt.append([s, b0])
            if b1 is not None and b1 < e:
                nxt.append([b1, e])
        spans = nxt
    return [[s, e] for s, e in spans if s <= e]


def link(db, e):
    if e['capacity'] < 1:
        raise ValueError('invalid_link_capacity')
    db.execute('INSERT INTO corridor_link VALUES(?,?,?,?,?)',
               (e['link_id'], e['from_controller'], e['to_intersection'], e['capacity'],
                e.get('declared_at') or e.get('recorded_at')))
    log(db, e, 'corridor', e['link_id'], 'declared', 'link_capacity_registered')
    return 'declared'


def link_event(db, e):
    if not one(db, 'SELECT 1 FROM corridor_link WHERE id=?', (e['link_id'],)):
        raise ValueError('unknown_link')
    if e['direction'] not in ('enter', 'exit'):
        raise ValueError('invalid_link_direction')
    if not e.get('receipt'):
        raise ValueError('receipt_required')
    intervals = _vehicle_intervals(db, e['link_id'])
    if e['direction'] == 'enter':
        if any(v == e['vehicle'] and x is None for v, _, x in intervals):
            raise ValueError('vehicle_already_in_link')
    else:
        match = [i for i in intervals if i[0] == e['vehicle'] and i[2] is None]
        if not match:
            raise ValueError('exit_without_enter')
        if e['at'] <= match[0][1]:
            raise ValueError('invalid_link_event_order')
    db.execute('INSERT INTO link_event VALUES(?,?,?,?,?,?)',
               (e['id'], e['link_id'], e['vehicle'], e['direction'], e['at'], e['receipt']))
    log(db, e, 'corridor', e['id'], 'captured', 'link_' + e['direction'] + '_receipt')
    return 'captured'


def meter(db, e):
    if e['meter_from'] >= e['meter_to']:
        raise ValueError('invalid_meter_period')
    if not e.get('receipt'):
        raise ValueError('meter_receipt_required')
    db.execute('INSERT INTO corridor_meter VALUES(?,?,?,?,?,?)',
               (e['meter_id'], e['controller_id'], e['approach'], e['meter_from'],
                e['meter_to'], e['receipt']))
    log(db, e, 'corridor', e['meter_id'], 'captured', 'ordinary_flow_metered_with_receipt')
    return 'captured'


def capacity_eval(db, e):
    """Evaluate the joint window for one corridor request over the short link.

    Endpoint intersection of the two green windows is not enough: every
    possible arrival and occupancy in the same timeline must keep capacity
    and green, otherwise the outcome stays conditional or unavailable.
    """
    route = one(db, 'SELECT * FROM corridor_request WHERE id=?', (e['request_id'],))
    if not route:
        raise ValueError('unknown_corridor_request')
    link_row = one(db, 'SELECT * FROM corridor_link WHERE id=?', (e['link_id'],))
    if not link_row:
        raise ValueError('unknown_link')
    if e['a_vehicle_from'] >= e['a_vehicle_to']:
        raise ValueError('invalid_vehicle_window')
    remote = one(db, 'SELECT * FROM remote_signal_window WHERE id=?', (route['remote_id'],))
    act = _activation_at(db, route['controller_id'], e['release_at'])
    intervals = _vehicle_intervals(db, e['link_id'])
    open_vehicles = sorted(v for v, _, x in intervals if x is None)
    plan = one(db, 'SELECT * FROM phase_plan WHERE id=?', (act['plan_id'],)) if act else None
    ped = _ped_guard(plan)
    tmin, tmax = route['travel_min_sec'], route['travel_max_sec']
    release = _parse(e['release_at'])
    evidence = {
        'a_readback': {'controller_id': act['controller_id'], 'plan_id': act['plan_id'],
                       'revision': act['plan_revision'], 'started_at': act['started_at'],
                       'source': 'field_activation'} if act else None,
        'a_vehicle_window': [e['a_vehicle_from'], e['a_vehicle_to']],
        'b_window': {'remote_id': remote['id'], 'verified': bool(remote['verified']),
                     'green_from': remote['green_from'], 'green_to': remote['green_to'],
                     'source': 'field_readback'},
        'travel_sec': [tmin, tmax],
        'arrival_window': [_iso(release + timedelta(seconds=tmin)),
                           _iso(release + timedelta(seconds=tmax))],
        'link': {'link_id': link_row['id'], 'capacity': link_row['capacity'],
                 'events': len(intervals) + len(open_vehicles), 'open_vehicles': open_vehicles,
                 'occupancy_at_release': _occupancy(intervals, release)},
        'ped_clearance': ped,
    }
    evidence['coverage'] = ('complete' if act and remote['verified'] and not open_vehicles
                            and ped and not ped['compressed'] else 'partial')
    feasible = []
    if not act:
        state, reason = UNAVAILABLE, 'missing_field_evidence'
    elif not remote['verified']:
        state, reason = UNAVAILABLE, 'remote_window_unverified'
    elif ped is None or ped['compressed']:
        state, reason = UNAVAILABLE, 'pedestrian_clearance_compressed'
    else:
        lo, hi = _green_span(e['a_vehicle_from'], e['a_vehicle_to'],
                             remote['green_from'], remote['green_to'], tmin, tmax)
        full = _full_segments(intervals, link_row['capacity'])
        feasible = _capacity_spans(lo, hi, full, tmax) if lo <= hi else []
        containing = [sp for sp in feasible if sp[0] <= release <= sp[1]]
        if lo > hi:
            state, reason = UNAVAILABLE, 'no_feasible_window'
        elif containing:
            state, reason = GUARANTEED, 'capacity_and_green_proven'
            feasible = containing
        elif feasible:
            in_green = lo <= release <= hi
            state = CONDITIONAL
            reason = 'capacity_blocked_until_receipt' if in_green else 'release_outside_green_window'
        elif open_vehicles:
            state, reason = CONDITIONAL, 'awaiting_exit_receipt'
        else:
            state, reason = UNAVAILABLE, 'no_feasible_window'
    db.execute('INSERT INTO corridor_eval VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (e['eval_id'], e['request_id'], e['link_id'], e['release_at'],
                e['a_vehicle_from'], e['a_vehicle_to'], state, reason,
                _iso(feasible[0][0]) if feasible else None,
                _iso(feasible[0][1]) if feasible else None,
                json.dumps(evidence, sort_keys=True)))
    log(db, e, 'corridor', e['eval_id'], state, reason)
    return state


def authorize(db, e):
    """Supervisor-authorized scheduling route for one request.

    route='meter' seals shuttle-dedicated capacity after receipted metering
    of ordinary vehicles and signs a guaranteed window at the cost of general
    flow; route='trigger' keeps ordinary flow and signs a conditional
    commitment released only by live A-entrance/B-exit receipts.  Neither
    route may touch the pedestrian phase.
    """
    route = one(db, 'SELECT * FROM corridor_request WHERE id=?', (e['request_id'],))
    if not route:
        raise ValueError('unknown_corridor_request')
    ev = one(db, 'SELECT * FROM corridor_eval WHERE id=?', (e['eval_id'],))
    if not ev:
        raise ValueError('unknown_eval')
    if e['route'] not in ('meter', 'trigger'):
        raise ValueError('unknown_route')
    link_row = one(db, 'SELECT * FROM corridor_link WHERE id=?', (ev['link_id'],))
    remote = one(db, 'SELECT * FROM remote_signal_window WHERE id=?', (route['remote_id'],))
    tmin, tmax = route['travel_min_sec'], route['travel_max_sec']
    lo, hi = _green_span(ev['a_vehicle_from'], ev['a_vehicle_to'],
                         remote['green_from'], remote['green_to'], tmin, tmax)
    if lo > hi:
        raise ValueError('no_feasible_window')
    window_from = e.get('window_from') or _iso(lo)
    window_to = e.get('window_to') or _iso(hi)
    if window_from >= window_to or not (lo <= _parse(window_from) and _parse(window_to) <= hi):
        raise ValueError('window_outside_green')
    act = _activation_at(db, route['controller_id'], window_from)
    if not act:
        raise ValueError('missing_field_evidence')
    if not remote['verified']:
        raise ValueError('remote_window_unverified')
    plan = one(db, 'SELECT * FROM phase_plan WHERE id=?', (act['plan_id'],))
    ped = _ped_guard(plan)
    if ped is None or ped['compressed']:
        raise ValueError('pedestrian_clearance_compressed')
    base_evidence = {
        'eval_id': ev['id'],
        'a_readback': {'controller_id': act['controller_id'], 'plan_id': act['plan_id'],
                       'revision': act['plan_revision'], 'source': 'field_activation'},
        'b_window': {'remote_id': remote['id'], 'verified': True,
                     'green_from': remote['green_from'], 'green_to': remote['green_to'],
                     'source': 'field_readback'},
        'ped_clearance': ped,
    }
    if e['route'] == 'meter':
        meter_row = one(db, 'SELECT * FROM corridor_meter WHERE id=?', (e.get('meter_id') or '',))
        if not meter_row:
            raise ValueError('unknown_meter')
        if meter_row['controller_id'] != route['controller_id']:
            raise ValueError('metering_wrong_junction')
        intervals = _vehicle_intervals(db, link_row['id'])
        full = _full_segments(intervals, link_row['capacity'])
        spans = _capacity_spans(lo, hi, full, tmax)
        wf, wt = _parse(window_from), _parse(window_to)
        if not any(s <= wf and wt <= x for s, x in spans):
            raise ValueError('window_not_capacity_feasible')
        if _occupancy(intervals, wf) != 0:
            raise ValueError('link_not_verified_empty')
        horizon = _iso(wt + timedelta(seconds=tmax))
        if not (meter_row['meter_from'] <= window_from and meter_row['meter_to'] >= horizon):
            raise ValueError('metering_insufficient')
        if any(wf < _parse(s) <= wt + timedelta(seconds=tmax) for _, s, _ in intervals):
            raise ValueError('sealed_capacity_contaminated')
        state, reason = GUARANTEED, 'metered_capacity_sealed'
        evidence = {**base_evidence,
                    'meter': {'meter_id': meter_row['id'], 'receipt': meter_row['receipt'],
                              'held_from': meter_row['meter_from'], 'held_to': meter_row['meter_to']},
                    'cost': 'ordinary_flow_metered',
                    'empty_at_seal': True,
                    'trap_guard': 'ordinary vehicles are metered at the A stop-line upstream of '
                                  'the junction box; the link is sealed only after exit receipts '
                                  'prove it empty, and vehicles already inside the link are never '
                                  'held, so no ordinary vehicle is trapped in the box'}
    else:
        state, reason = CONDITIONAL, 'release_on_receipt_confirmed_capacity'
        evidence = {**base_evidence,
                    'condition': 'release the shuttle only when A-entrance and B-exit receipts '
                                 'confirm link occupancy below capacity',
                    'late_receipt_guard': 'a late or missing receipt withholds the release; '
                                          'predicted clearance never substitutes a receipt, so the '
                                          'reserved slot is never doubly occupied',
                    'comms_loss': 'loss of control messages falls back to the normal safe plan'}
    db.execute('INSERT INTO corridor_commitment VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['auth_id'], e['request_id'], ev['id'], e['route'], state,
                window_from, window_to, 'pending', reason,
                json.dumps(evidence, sort_keys=True), e.get('at') or e.get('recorded_at'), None))
    log(db, e, 'corridor', e['auth_id'], state, reason)
    return state


def execute(db, e):
    """Command the release of a pending commitment, or withhold it.

    A successful execution never rewrites the commitment state: a conditional
    trigger stays conditional even after receipts confirm it, so an
    after-the-fact success is never back-written as a priori guaranteed.
    """
    c = one(db, 'SELECT * FROM corridor_commitment WHERE id=?', (e['commitment_id'],))
    if not c:
        raise ValueError('unknown_commitment')
    if c['status'] != 'pending':
        raise ValueError('commitment_not_pending')
    route = one(db, 'SELECT * FROM corridor_request WHERE id=?', (c['request_id'],))
    ev = one(db, 'SELECT * FROM corridor_eval WHERE id=?', (c['eval_id'],))
    link_row = one(db, 'SELECT * FROM corridor_link WHERE id=?', (ev['link_id'],))
    remote = one(db, 'SELECT * FROM remote_signal_window WHERE id=?', (route['remote_id'],))
    at = e['at']
    reason = None
    act = _activation_at(db, route['controller_id'], at)
    if not act or not remote['verified']:
        reason = 'field_evidence_stale'
    elif not (c['window_from'] <= at <= c['window_to']):
        reason = 'release_outside_committed_window'
    else:
        t0 = _parse(at)
        t1 = t0 + timedelta(seconds=route['travel_max_sec'])
        intervals = _vehicle_intervals(db, link_row['id'])
        full = _full_segments(intervals, link_row['capacity'])
        if _conflicts(t0, t1, full):
            reason = 'capacity_not_confirmed'
        elif c['route'] == 'meter':
            meter_id = json.loads(c['evidence_json'])['meter']['meter_id']
            meter_row = one(db, 'SELECT * FROM corridor_meter WHERE id=?', (meter_id,))
            if _occupancy(intervals, t0) != 0 or not (meter_row['meter_from'] <= at
                                                      and meter_row['meter_to'] >= _iso(t1)):
                reason = 'capacity_not_confirmed'
    if reason:
        db.execute('INSERT INTO corridor_notice VALUES(?,?,?,?,?)',
                   (e['id'], c['id'], 'release_withheld', at,
                    json.dumps({'reason': reason}, sort_keys=True)))
        log(db, e, 'corridor', c['id'], 'withheld', reason)
        return 'withheld'
    db.execute("UPDATE corridor_commitment SET status='executed',closed_at=? WHERE id=?",
               (at, c['id']))
    db.execute('INSERT INTO corridor_notice VALUES(?,?,?,?,?)',
               (e['id'], c['id'], 'command_executed', at,
                json.dumps({'route': c['route'], 'state': c['state'],
                            'window': [c['window_from'], c['window_to']]}, sort_keys=True)))
    log(db, e, 'corridor', c['id'], 'executed', 'released_within_committed_window')
    return 'executed'


def fallback(db, e):
    """Trigger-route commitment loses control messages: revert to safe plan."""
    c = one(db, 'SELECT * FROM corridor_commitment WHERE id=?', (e['commitment_id'],))
    if not c:
        raise ValueError('unknown_commitment')
    if c['status'] != 'pending':
        raise ValueError('commitment_not_pending')
    if c['route'] != 'trigger':
        raise ValueError('fallback_not_applicable')
    db.execute("UPDATE corridor_commitment SET status='fallback',closed_at=? WHERE id=?",
               (e['at'], c['id']))
    db.execute('INSERT INTO corridor_notice VALUES(?,?,?,?,?)',
               (e['id'], c['id'], 'fallback_to_safe_plan', e['at'],
                json.dumps({'reason': e.get('reason', 'comms_lost')}, sort_keys=True)))
    log(db, e, 'corridor', c['id'], 'fallback', 'comms_lost_fell_back_to_safe_plan')
    return 'fallback'


def notify(db, e):
    """Manual notification about a commitment; kept forever at its version."""
    c = one(db, 'SELECT * FROM corridor_commitment WHERE id=?', (e['commitment_id'],))
    if not c:
        raise ValueError('unknown_commitment')
    db.execute('INSERT INTO corridor_notice VALUES(?,?,?,?,?)',
               (e['id'], c['id'], 'manual_notify', e['at'], e['detail']))
    log(db, e, 'corridor', c['id'], 'notified', 'manual_notice_recorded')
    return 'notified'


def _revoke_pending(db, request_ids, at, reason, e):
    for request_id in request_ids:
        for row in many(db, "SELECT * FROM corridor_commitment WHERE request_id=? AND status='pending'",
                        (request_id,)):
            db.execute("UPDATE corridor_commitment SET status='revoked',reason=?,closed_at=? WHERE id=?",
                       (reason, at, row['id']))
            log(db, e, 'corridor', row['id'], 'revoked', reason)


def invalidate_remote(db, remote_id, at, reason, e):
    """B's readback revoked: unexecuted joint commitments lose their basis.

    Already executed commands and manual notices keep their original rows.
    """
    request_ids = [r['id'] for r in many(db, 'SELECT id FROM corridor_request WHERE remote_id=?',
                                         (remote_id,))]
    _revoke_pending(db, request_ids, at, reason, e)


def invalidate_controller(db, controller_id, at, reason, e):
    """A's field fault rollback: unexecuted joint commitments are revoked."""
    request_ids = [r['id'] for r in many(db, 'SELECT id FROM corridor_request WHERE controller_id=?',
                                         (controller_id,))]
    _revoke_pending(db, request_ids, at, reason, e)
