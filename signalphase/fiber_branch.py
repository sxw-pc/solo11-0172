"""Branched fiber topology, directional evidence and dispatch work orders.

A single-ended echo from the main cabinet yields one *one-dimensional*
distance v*t/2.  Past a Y splitter that distance maps to several physical
segments (the two branches are indistinguishable to that instrument), so a
fault that cannot be told apart is reported as an ordered set of executable
candidate segments rather than a single map point.  Remote-end reverse
trials and per-branch isolation each carry their own measurement direction
and narrow the work order by different amounts, with different service cost.
"""
import json

from .store import log, many, one

# Same matching tolerance as the provisional one-dimensional triage.
SPLICE_TOL_KM = 0.05
AMPLITUDE_TOL = 0.05
TRUNK = 'trunk'
NORTH = 'north'
SOUTH = 'south'
BRANCHES = (NORTH, SOUTH)
OPEN_STATES = ('ambiguous_candidates', 'single_candidate', 'splice_known')


def _pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _latest_topology(db, topology_id):
    return one(db, 'SELECT * FROM fiber_branch_topology WHERE id=?', (topology_id,))


def _validate_geometry(e):
    if min(e['trunk_km'], e['north_branch_km'], e['south_branch_km'],
           e['propagation_km_per_us']) <= 0:
        raise ValueError('invalid_branch_geometry')
    if e['revision'] < 1 or min(e['north_access_control_min'], e['south_access_control_min']) < 0:
        raise ValueError('invalid_branch_revision')
    if not e['wiring_revision']:
        raise ValueError('wiring_revision_required')


def topology(db, e):
    """Register a versioned topology; every work order pins the revision it
    was derived from so later replay sees the same geometry."""
    _validate_geometry(e)
    old = one(db, 'SELECT * FROM fiber_branch_topology WHERE topology_id=?',
              (e['topology_id'],))
    if old and e['revision'] <= old['revision']:
        raise ValueError('stale_topology_revision')
    db.execute('INSERT INTO fiber_branch_topology VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(topology_id) DO UPDATE SET controller_id=excluded.controller_id,'
               'revision=excluded.revision,trunk_km=excluded.trunk_km,'
               'north_branch_km=excluded.north_branch_km,south_branch_km=excluded.south_branch_km,'
               'propagation_km_per_us=excluded.propagation_km_per_us,'
               'wiring_revision=excluded.wiring_revision,'
               'north_access_control_min=excluded.north_access_control_min,'
               'south_access_control_min=excluded.south_access_control_min,'
               'valid_from=excluded.valid_from,status=excluded.status',
               (e['topology_id'], e['controller_id'], e['revision'], e['trunk_km'],
                e['north_branch_km'], e['south_branch_km'], e['propagation_km_per_us'],
                e['wiring_revision'], e['north_access_control_min'],
                e['south_access_control_min'], e['valid_from'], 'current'))
    db.execute('INSERT OR REPLACE INTO fiber_branch_topology_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['topology_id'], e['revision'], e['controller_id'], e['trunk_km'],
                e['north_branch_km'], e['south_branch_km'], e['propagation_km_per_us'],
                e['wiring_revision'], e['north_access_control_min'],
                e['south_access_control_min'], e['valid_from'], 'current'))
    for t in e.get('splice_templates', []):
        if min(t['at_km'], t['amplitude_expected']) <= 0:
            raise ValueError('invalid_splice_template')
        db.execute('INSERT INTO fiber_splice_template VALUES(?,?,?,?,?,?,?)',
                   (t['splice_id'], e['topology_id'], e['revision'], e['wiring_revision'],
                    t['at_km'], t['amplitude_expected'], t.get('kind', 'splice')))
    outcome = 'registered'
    if old:
        outcome = _reproject_open_orders(db, e, old)
    log(db, e, 'fiber_branch', e['topology_id'], outcome,
        'topology_revision' if old else 'branch_topology_registered')
    return outcome


def _reproject_open_orders(db, e, old):
    """A late topology revision only changes still-open orders; an order
    already dispatched on point evidence keeps its original pin."""
    reprojected = 0
    for wo in many(db, 'SELECT DISTINCT wo_id FROM fiber_work_order WHERE topology_id=?',
                   (e['topology_id'],)):
        current = one(db, 'SELECT * FROM fiber_work_order WHERE wo_id=? ORDER BY revision DESC LIMIT 1',
                      (wo['wo_id'],))
        if current['status'] not in OPEN_STATES:
            continue
        echo = one(db, 'SELECT * FROM fiber_branch_echo WHERE echo_id=?', (current['echo_id'],))
        _project_echo(db, e, echo, echo_id=current['echo_id'],
                      wo_id=current['wo_id'], prior_revision=current['revision'],
                      reason='late_topology_revision_reprojected')
        reprojected += 1
    return f'registered_reprojected_{reprojected}'


def _matching_splice(db, topo, distance_km, amplitude):
    """Splice templates are bound to the wiring revision they were measured
    on; a template from an older splice job never explains new wiring."""
    if amplitude is None:
        return None
    return one(db, """SELECT * FROM fiber_splice_template
                      WHERE topology_id=? AND topology_revision=? AND wiring_revision=?
                        AND ABS(at_km-?)<=? AND ABS(amplitude_expected-?)<=?""",
               (topo['topology_id'], topo['revision'], topo['wiring_revision'],
                distance_km, SPLICE_TOL_KM, amplitude, AMPLITUDE_TOL))


def _candidate_rows(db, topo, distance_km, amplitude):
    """Turn a 1-D distance into physical candidate segments on the pinned
    topology revision.  Distance beyond both branches lands on the trunk."""
    splice = _matching_splice(db, topo, distance_km, amplitude)
    if splice:
        return [{
            'branch': TRUNK, 'offset_km': None, 'distance_km': splice['at_km'],
            'label': f"{TRUNK}@{splice['at_km']}:{splice['splice_id']}",
            'traffic_control_min': 0, 'reason': 'known_splice_reflection',
        }], 'splice_known'
    if distance_km <= topo['trunk_km'] + SPLICE_TOL_KM:
        rows = [{
            'branch': TRUNK,
            'offset_km': round(distance_km, 6),
            'distance_km': round(distance_km, 6),
            'label': f'{TRUNK}@{round(distance_km, 3)}km',
            'traffic_control_min': 0,
            'reason': 'trunk_candidate',
        }]
        return rows, 'single_candidate'
    offset = distance_km - topo['trunk_km']
    rows = []
    for branch, branch_len, control in (
            (NORTH, topo['north_branch_km'], topo['north_access_control_min']),
            (SOUTH, topo['south_branch_km'], topo['south_access_control_min'])):
        if offset <= branch_len + SPLICE_TOL_KM:
            rows.append({
                'branch': branch,
                'offset_km': round(offset, 6),
                'distance_km': round(topo['trunk_km'] + min(offset, branch_len), 6),
                'label': f'{branch}@{round(offset, 3)}km',
                'traffic_control_min': control,
                'reason': 'symmetric_y_branch_candidate',
            })
    state = 'ambiguous_candidates' if len(rows) > 1 else 'single_candidate'
    return rows, state


def _next_wo_revision(db, wo_id):
    row = one(db, 'SELECT MAX(revision) AS r FROM fiber_work_order WHERE wo_id=?', (wo_id,))
    return 1 if row['r'] is None else row['r'] + 1


def _store_order(db, wo_id, echo_id, topo, rows, status, scope, equipment,
                 service_impact, reason, prior_revision=0):
    revision = prior_revision + 1 if prior_revision else _next_wo_revision(db, wo_id)
    db.execute('INSERT INTO fiber_work_order VALUES(?,?,?,?,?,?,?,?,?,?)',
               (wo_id, revision, echo_id, topo['topology_id'], topo['revision'],
                status, scope, equipment, service_impact, reason))
    # Shortest executable check order: lower traffic-control closure first,
    # branch name as the deterministic tie breaker.
    ordered = sorted(rows, key=lambda r: (r['traffic_control_min'], r['branch']))
    for seq, row in enumerate(ordered, start=1):
        db.execute('INSERT INTO fiber_work_order_candidate'
                   '(wo_id,wo_revision,branch,offset_km,distance_km,label,traffic_control_min,sequence_no,reason)'
                   ' VALUES(?,?,?,?,?,?,?,?,?)',
                   (wo_id, revision, row['branch'], row['offset_km'], row['distance_km'],
                    row['label'], row['traffic_control_min'], seq, row['reason']))
    return revision


def _project_echo(db, topo, echo, wo_id, echo_id=None, prior_revision=0, reason=None):
    trace = json.loads(echo['trace_json'])
    rows = []
    status = 'ambiguous_candidates'
    for point in trace:
        rows, status = _candidate_rows(db, topo, point['distance_km'], point.get('amplitude'))
        if len(rows) == 1 and status in ('single_candidate', 'splice_known'):
            break
    if status == 'splice_known':
        scope, equipment, impact = 'known_splice', 'splice_inspection_kit', 'none'
        reason = reason or 'reflection_matches_registered_splice_template'
    elif status == 'single_candidate':
        scope, equipment, impact = 'single_segment', 'targeted_repair_crew', 'none'
        reason = reason or 'one_dimensional_distance_maps_to_one_segment'
    else:
        scope = 'candidate_segments'
        equipment = 'full_branch_inspection_kit'
        impact = 'none_yet'
        reason = reason or 'indistinguishable_y_branch_candidates'
    return _store_order(db, wo_id, echo_id or echo['echo_id'], topo, rows, status, scope,
                        equipment, impact, reason, prior_revision=prior_revision)


def branch_echo(db, e):
    """Persist the full single-ended trace.  Round-trip time is converted to
    one-way distance with v*t/2; every reflection keeps its direction and the
    remote terminal evidence is stored alongside it."""
    topo = one(db, 'SELECT * FROM fiber_branch_topology WHERE topology_id=? AND revision=?',
               (e['topology_id'], e['topology_revision']))
    if not topo:
        raise ValueError('unknown_topology_revision')
    if e.get('direction', 'forward') != 'forward':
        raise ValueError('cabinet_echo_must_be_forward_direction')
    trace = e.get('trace')
    if not isinstance(trace, list) or not trace:
        raise ValueError('full_echo_trace_required')
    if one(db, 'SELECT 1 FROM fiber_branch_echo WHERE echo_id=?', (e['echo_id'],)):
        raise ValueError('event_id_reused')
    measured = []
    for point in trace:
        if point['return_us'] <= 0:
            raise ValueError('invalid_echo_time')
        distance = point['return_us'] * topo['propagation_km_per_us'] / 2
        measured.append({'return_us': point['return_us'],
                         'distance_km': round(distance, 6),
                         'amplitude': point['amplitude']})
    db.execute('INSERT INTO fiber_branch_echo VALUES(?,?,?,?,?,?,?)',
               (e['echo_id'], e['topology_id'], e['topology_revision'], e['measured_at'],
                'forward', _pack(measured), _pack(e.get('remote_terminals', []))))
    wo_id = f'WO-{e["echo_id"]}'
    revision = _project_echo(db, topo,
                             one(db, 'SELECT * FROM fiber_branch_echo WHERE echo_id=?',
                                 (e['echo_id'],)), wo_id)
    log(db, e, 'fiber_branch', wo_id, f'projected_rev{revision}',
        'single_ended_echo_distance_not_unique_location')
    return f'work_order_rev{revision}'


def remote_probe(db, e):
    """Test A: portable power meters attached at the remote terminals without
    disconnecting the running branch.  Its direction is reverse (terminal ->
    cabinet).  A dark terminal narrows the work order to that whole branch;
    light arriving on the other branch only proves that branch's continuity."""
    echo = one(db, 'SELECT * FROM fiber_branch_echo WHERE echo_id=?', (e['echo_id'],))
    if not echo:
        raise ValueError('unknown_branch_echo')
    if e.get('direction', 'reverse') != 'reverse':
        raise ValueError('remote_probe_must_be_reverse_direction')
    if e.get('disconnected_running'):
        raise ValueError('running_branch_must_not_be_disconnected')
    readings = e.get('readings')
    if not isinstance(readings, list) or {r['branch'] for r in readings} != set(BRANCHES):
        raise ValueError('both_remote_terminal_readings_required')
    topo = one(db, 'SELECT * FROM fiber_branch_topology WHERE topology_id=? AND revision=?',
               (echo['topology_id'], echo['topology_revision']))
    dark = sorted(r['branch'] for r in readings if not r['light_received'])
    light = sorted(r['branch'] for r in readings if r['light_received'])
    if not dark:
        inference, reason = {'dark': [], 'light': light}, 'all_terminals_lighting_no_dark_branch'
        db.execute('INSERT INTO fiber_remote_probe'
                   '(probe_id,echo_id,measured_at,direction,disconnected_running,maintenance_branch,'
                   'readings_json,inference_json,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                   (e['probe_id'], e['echo_id'], e['measured_at'], 'reverse', 0,
                    e.get('maintenance_branch'), _pack(readings), _pack(inference), reason))
        log(db, e, 'fiber_branch', e['probe_id'], 'recorded', reason)
        return 'recorded'
    if len(dark) > 1:
        raise ValueError('probe_did_not_narrow_to_one_branch')
    fault_branch = dark[0]
    # A maintenance disconnect explains darkness on that opened branch, so it
    # cannot be treated as a fault; and it can never be reported as continuity
    # evidence for the other branch either.
    if e.get('maintenance_branch'):
        if e['maintenance_branch'] == fault_branch:
            raise ValueError('maintenance_disconnect_is_not_a_fault_finding')
        raise ValueError('maintenance_disconnect_cannot_verify_other_branch')
    inference = {'dark': dark, 'light': light,
                 'limit': 'no_light_cannot_locate_point_along_branch'}
    reason = 'power_meter_narrows_to_whole_branch'
    db.execute('INSERT INTO fiber_remote_probe'
               '(probe_id,echo_id,measured_at,direction,disconnected_running,maintenance_branch,'
               'readings_json,inference_json,reason) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['probe_id'], e['echo_id'], e['measured_at'], 'reverse', 0,
                e.get('maintenance_branch'), _pack(readings), _pack(inference), reason))
    control = topo[f'{fault_branch}_access_control_min']
    row = {
        'branch': fault_branch, 'offset_km': None,
        'distance_km': round(topo['trunk_km'] + topo[f'{fault_branch}_branch_km'], 6),
        'label': f'{fault_branch}:full_branch',
        'traffic_control_min': control,
        'reason': 'remote_dark_terminal_whole_branch',
    }
    wo_id = f'WO-{e["echo_id"]}'
    current = one(db, 'SELECT * FROM fiber_work_order WHERE wo_id=? ORDER BY revision DESC LIMIT 1',
                  (wo_id,))
    revision = _store_order(db, wo_id, e['echo_id'], topo, [row], 'branch_scoped',
                            'whole_branch', 'full_branch_inspection_kit', 'none_yet',
                            reason, prior_revision=current['revision'])
    log(db, e, 'fiber_branch', wo_id, f'branch_scoped_rev{revision}', reason)
    return f'work_order_rev{revision}'


def backup_switchover(db, e):
    """The receipt that traffic actually moved to the standby link is kept in
    its own evidence table, separate from controller field readback."""
    if one(db, 'SELECT 1 FROM fiber_backup_receipt WHERE receipt_id=?', (e['receipt_id'],)):
        raise ValueError('event_id_reused')
    db.execute('INSERT INTO fiber_backup_receipt VALUES(?,?,?,?,?)',
               (e['receipt_id'], e['link_id'], e['issued_at'], e['status'],
                e.get('reason', 'standby_link_carrying_traffic')))
    log(db, e, 'fiber_branch', e['receipt_id'], e['status'], 'backup_switchover_receipt')
    return e['status']


def branch_isolation(db, e):
    """Test B: per-branch isolation at the Y splitter following the signed
    switch order, with calibrated forward reflection re-measurement from the
    cabinet after each isolation.  A standby-link receipt issued before the
    window is mandatory; the outage window is recorded separately and is never
    written back as the original fiber break time."""
    echo = one(db, 'SELECT * FROM fiber_branch_echo WHERE echo_id=?', (e['echo_id'],))
    if not echo:
        raise ValueError('unknown_branch_echo')
    receipt = one(db, 'SELECT * FROM fiber_backup_receipt WHERE receipt_id=?', (e['receipt_id'],))
    if not receipt:
        raise ValueError('backup_receipt_required_before_isolation')
    if receipt['status'] != 'carrying_traffic' or receipt['issued_at'] > e['started_at']:
        raise ValueError('standby_link_not_confirmed_before_window')
    topo = one(db, 'SELECT * FROM fiber_branch_topology WHERE topology_id=? AND revision=?',
               (echo['topology_id'], echo['topology_revision']))
    steps = e.get('steps')
    if not isinstance(steps, list) or {s['branch'] for s in steps} != set(BRANCHES):
        raise ValueError('isolation_step_per_branch_required')
    for step in steps:
        if step.get('direction', 'forward') != 'forward':
            raise ValueError('cabinet_remeasurement_must_be_forward_direction')
        if step['echo_present'] and not step.get('return_us', 0) > 0:
            raise ValueError('present_echo_needs_calibrated_return_time')
    fault_branches = sorted(s['branch'] for s in steps if not s['echo_present'])
    # The Y topology makes this decisive only when exactly one isolation
    # removes the echo; both missing or both present mean the fault lives
    # outside either branch and no point dispatch may be produced.
    if len(fault_branches) != 1:
        raise ValueError('isolation_result_not_decisive')
    fault_branch = fault_branches[0]
    db.execute('INSERT INTO fiber_branch_isolation'
               '(isolation_id,echo_id,switch_order_id,receipt_id,started_at,ended_at,'
               'fault_branch,steps_json,reason) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['isolation_id'], e['echo_id'], e['switch_order_id'], e['receipt_id'],
                e['started_at'], e['ended_at'], fault_branch, _pack(steps),
                'isolated_branch_remeasurement'))
    for step in steps:
        db.execute('INSERT INTO fiber_service_window'
                   '(isolation_id,branch,from_at,to_at,interrupted,reason) VALUES(?,?,?,?,?,?)',
                   (e['isolation_id'], step['branch'], step['from_at'], step['to_at'],
                   1 if step['interrupted'] else 0,
                   step.get('reason', 'per_branch_isolation_window')))
    # Re-measurement is forward direction with the same calibration; the
    # original echo's distance is reused, never its raw round-trip time as a
    # one-way value, and never its capture time as the outage moment.
    original_distance = json.loads(echo['trace_json'])[0]['distance_km']
    offset = round(original_distance - topo['trunk_km'], 6)
    row = {
        'branch': fault_branch,
        'offset_km': offset,
        'distance_km': round(original_distance, 6),
        'label': f'{fault_branch}@{round(offset, 3)}km',
        'traffic_control_min': topo[f'{fault_branch}_access_control_min'],
        'reason': 'isolation_localized_point',
    }
    wo_id = f'WO-{e["echo_id"]}'
    current = one(db, 'SELECT * FROM fiber_work_order WHERE wo_id=? ORDER BY revision DESC LIMIT 1',
                  (wo_id,))
    revision = _store_order(db, wo_id, e['echo_id'], topo, [row], 'point_localized',
                            'point_segment', 'targeted_splice_crew',
                            'short_outage_under_backup_receipt',
                            'per_branch_isolation_localized_fault',
                            prior_revision=current['revision'])
    log(db, e, 'fiber_branch', wo_id, f'point_localized_rev{revision}',
        'isolation_window_separate_from_original_fault_time')
    return f'work_order_rev{revision}'
