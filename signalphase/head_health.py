"""Joint visibility and repair-scope verdicts for same-phase signal-head groups.

A single restricted supply line and one camera phase constrain *all* heads over
*all* frames simultaneously. Per-head camera intervals can never be combined
into a group-wide healthy conclusion: the joint feasible set decides how many
heads are provably under-lit, while heads that cannot be located stay on a
group-level pending ticket instead of being named at random.
"""
import json

from .store import one, many, log

EPS = 1e-9


def frame_mean(scan):
    values = json.loads(scan['values_json'])
    return sum(values) / len(values)


def camera_interval(scan):
    """Camera-only feasible period-average luminance interval for one head.

    The shutter integrates light over ``exposure_us`` inside a brightness window
    of ``shutter_window_us``. Energy captured per frame gives the lower bound;
    inverting the duty cycle gives the camera-only upper bound, which the shared
    supply budget later tightens.
    """
    mean = frame_mean(scan)
    duty = scan['exposure_us'] / scan['shutter_window_us']
    return mean * duty, mean / duty


def joint_feasibility(heads, budget, required):
    """Prove per-head and group-wide conclusions over one joint feasible set.

    ``heads`` is a list of ``(head_id, low, high)`` interval tuples. The same
    ``budget`` applies to every head simultaneously: ``sum(L_i) <= budget``.
    """
    intervals = [(hid, float(lo), float(hi)) for hid, lo, hi in heads]
    if any(lo > hi + EPS for _, lo, hi in intervals):
        raise ValueError('inverted_luminance_interval')
    baseline = sum(lo for _, lo, _ in intervals)
    if baseline > budget + EPS:
        raise ValueError('joint_feasible_set_empty')
    result = []
    for hid, lo, hi in intervals:
        others = baseline - lo
        can_dim = lo + EPS < required and baseline <= budget + EPS
        can_healthy = hi + EPS >= required and max(required, lo) + others <= budget + EPS
        if not can_dim:
            status = 'confirmed_healthy'
        elif not can_healthy:
            status = 'confirmed_dim'
        else:
            status = 'possible_dim'
        result.append({'head_id': hid, 'low': lo, 'high': hi, 'status': status,
                       'can_dim': can_dim, 'can_healthy': can_healthy})
    # Minimum number of dim heads: keep as many heads healthy as the budget
    # allows. Heads whose own interval cannot reach the requirement are forced
    # dim; the rest are upgraded cheapest-first under the single shared budget.
    forced = [hid for hid, lo, hi in intervals if hi + EPS < required]
    upgrade = sorted((max(required - lo, 0.0), hid)
                     for hid, lo, hi in intervals if hid not in forced)
    spent = baseline
    healthy = 0
    for delta, _ in upgrade:
        if spent + delta <= budget + EPS:
            spent += delta
            healthy += 1
    min_dim = len(intervals) - healthy
    group_healthy = all(lo + EPS >= required for _, lo, _ in intervals)
    return {
        'feasible': True,
        'heads': result,
        'min_dim_heads': min_dim,
        'forced_dim_heads': forced,
        'group_healthy': group_healthy,
        'budget': budget,
        'required_luminance': required,
    }


def _active_plan(db, controller_id, at):
    return one(db, "SELECT * FROM activation WHERE controller_id=? AND started_at<=? "
                   "AND (ended_at IS NULL OR ended_at>?) ORDER BY id DESC LIMIT 1",
               (controller_id, at, at))


def _next_revision(db, group_id):
    row = one(db, 'SELECT COALESCE(MAX(revision),0) AS r FROM health_verdict WHERE group_id=?', (group_id,))
    return row['r'] + 1


def _supersede(db, group_id, window_from, window_to):
    db.execute('UPDATE health_verdict SET superseded=1 WHERE group_id=? AND superseded=0 '
               'AND COALESCE(window_from,\'\')=COALESCE(?,\'\') AND COALESCE(window_to,\'\')=COALESCE(?,\'\')',
               (group_id, window_from, window_to))


def _persist(db, e, group, analysis, revision, scope, window_from, window_to,
             work_orders, control, evidence):
    for head in analysis['heads']:
        db.execute('INSERT INTO health_verdict(group_id,revision,head_id,scope,window_from,window_to,'
                   'status,min_luminance,max_luminance,required_luminance,min_dim_heads,group_healthy,'
                   'work_order,control_action,evidence_json,superseded,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
                   (group['id'], revision, head['head_id'], scope, window_from, window_to,
                    head['status'], head['low'], head['high'], analysis['required_luminance'],
                    analysis['min_dim_heads'], 1 if analysis['group_healthy'] else 0,
                    work_orders.get(head['head_id']), control,
                    json.dumps(evidence.get(head['head_id'], {}), sort_keys=True),
                    'joint_supply_budget_proof'))
    db.execute('INSERT INTO health_verdict(group_id,revision,head_id,scope,window_from,window_to,'
               'status,min_luminance,max_luminance,required_luminance,min_dim_heads,group_healthy,'
               'work_order,control_action,evidence_json,superseded,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
               (group['id'], revision, '*group*', scope, window_from, window_to,
                        'group_healthy' if analysis['group_healthy'] else 'group_at_least_one_dim',
                        sum(h['low'] for h in analysis['heads']),
                        min(analysis['budget'], sum(h['high'] for h in analysis['heads'])),
                        analysis['required_luminance'], analysis['min_dim_heads'],
                        1 if analysis['group_healthy'] else 0,
                        work_orders.get('*group*'), control,
                        json.dumps(evidence.get('*group*', {}), sort_keys=True),
                        'joint_supply_budget_proof'))


def _action(db, e, group_id, action, scope, head_id, window_from, window_to, at, reason):
    db.execute('INSERT INTO control_action(event_id,group_id,action,scope,head_id,window_from,window_to,at,reason) '
               'VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'], group_id, action, scope, head_id, window_from, window_to, at, reason))


def register_group(db, e):
    """Calibrated supply group: one budget, one camera phase, all frames."""
    budget = float(e['budget']); required = float(e['required_luminance'])
    scale = float(e['power_to_luminance'])
    if budget <= 0 or required <= 0 or scale <= 0:
        raise ValueError('invalid_power_calibration')
    if not isinstance(e['sync_skew_us'], int) or not isinstance(e['period_us'], int) or e['period_us'] <= 0:
        raise ValueError('invalid_camera_phase')
    if one(db, 'SELECT 1 FROM power_group WHERE id=?', (e['group_id'],)):
        raise ValueError('group_already_registered')
    head_specs = e['heads']
    if not isinstance(head_specs, list) or len(head_specs) < 2:
        raise ValueError('group_needs_multiple_heads')
    intervals = []
    rates = set()
    for spec in head_specs:
        scan = one(db, 'SELECT * FROM head_scan WHERE id=? AND controller_id=?',
                   (spec['scan_id'], e['controller_id']))
        if not scan:
            raise ValueError('unknown_head_scan')
        if scan['head_id'] != spec['head_id']:
            raise ValueError('scan_head_mismatch')
        rates.add(scan['sample_rate_hz'])
        lo, hi = camera_interval(scan)
        intervals.append((spec['head_id'], lo, hi))
    if len(rates) != 1:
        raise ValueError('shared_camera_phase_required')
    active = _active_plan(db, e['controller_id'], e['observed_from'])
    state = 'attributed' if active else 'unattributed'
    head_ids = [spec['head_id'] for spec in head_specs]
    db.execute('INSERT INTO power_group VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['group_id'], e['phase_id'], e['controller_id'], budget, required, scale,
                e['sync_skew_us'], e['period_us'], e['clock_ref'], json.dumps(head_ids),
                e['observed_from'], e['observed_to'],
                active['plan_id'] if active else None, active['plan_revision'] if active else None,
                state, 'calibrated_supply_and_camera_phase'))
    analysis = joint_feasibility(intervals, budget, required)
    return _finish_verdict(db, e, e['group_id'], analysis, e['observed_from'], e['observed_to'],
                           'camera_frames', state)


def _finish_verdict(db, e, group_id, analysis, window_from, window_to, evidence_kind, state):
    group = one(db, 'SELECT * FROM power_group WHERE id=?', (group_id,))
    revision = _next_revision(db, group_id)
    _supersede(db, group_id, window_from, window_to)
    confirmed = [h['head_id'] for h in analysis['heads'] if h['status'] == 'confirmed_dim']
    unresolved = analysis['min_dim_heads'] - len(confirmed)
    work_orders = {hid: f'head-work-order:{group_id}:{hid}' for hid in confirmed}
    if unresolved > 0:
        work_orders['*group*'] = f'group-pending:{group_id}:unlocated-{unresolved}'
    if analysis['min_dim_heads'] >= 1:
        control = 'close_lane'
        reason = 'at_least_one_head_provably_dim_lane_closed'
    else:
        control = 'lane_open'
        reason = 'joint_feasible_set_allows_full_group_health'
    evidence = {'*group*': {'evidence': evidence_kind, 'attribution': state,
                            'budget': analysis['budget'], 'sync_skew_us': group['sync_skew_us'],
                            'clock_ref': group['clock_ref']}}
    _persist(db, e, group, analysis, revision, 'group' if window_from is None else 'window',
             window_from, window_to, work_orders, control, evidence)
    at = e.get('recorded_at', e.get('at', window_from))
    for hid in confirmed:
        _action(db, e, group_id, 'head_work_order', 'head', hid, window_from, window_to, at,
                'joint_analysis_confirmed_this_head_dim')
    if unresolved > 0:
        _action(db, e, group_id, 'group_pending_ticket', 'group', None, window_from, window_to, at,
                f'group_level_investigation_unlocated_{unresolved}_head_no_random_attribution')
    if control == 'close_lane':
        _action(db, e, group_id, 'close_lane', 'group', None, window_from, window_to, at, reason)
    else:
        _action(db, e, group_id, 'lane_open', 'group', None, window_from, window_to, at, reason)
    log(db, e, 'head-health', group_id, control,
        f'min_dim_heads={analysis["min_dim_heads"]} confirmed={len(confirmed)} unresolved={unresolved}')
    return {'revision': revision, 'min_dim_heads': analysis['min_dim_heads'],
            'confirmed_dim': confirmed, 'unresolved': unresolved, 'control': control,
            'head_status': {h['head_id']: h['status'] for h in analysis['heads']}}


def _save_current(db, e, group, historical, complete, present):
    for branch in e['branches']:
        if branch['head_id'] not in json.loads(group['head_ids_json']):
            raise ValueError('branch_not_in_group')
        db.execute('INSERT INTO current_reading(id,group_id,branch_id,head_id,window_from,window_to,'
                   'mean_current,calibrated_clock,clock_offset_us,complete,covers_historical_window,'
                   'present,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (branch['reading_id'], group['id'], branch['branch_id'], branch['head_id'],
                    e['window_from'], e['window_to'], float(branch['mean_current']),
                    e['calibrated_clock'], int(e.get('clock_offset_us', 0)),
                    1 if complete else 0, 1 if historical else 0, 1 if present else 0,
                    e['recorded_at']))


def current_record(db, e):
    """Route A: complete, clock-calibrated branch currents for the OLD window."""
    group = one(db, 'SELECT * FROM power_group WHERE id=?', (e['group_id'],))
    if not group:
        raise ValueError('unknown_power_group')
    if e['calibrated_clock'] != group['clock_ref']:
        raise ValueError('common_clock_not_calibrated')
    if not (e['window_from'] >= group['observed_from'] and e['window_to'] <= group['observed_to']):
        raise ValueError('reading_outside_historical_window')
    known = set(json.loads(group['head_ids_json']))
    if {b['head_id'] for b in e['branches']} != known or not e.get('complete', False):
        raise ValueError('incomplete_branch_records')
    _save_current(db, e, group, historical=True, complete=True, present=True)
    intervals = []
    for branch in e['branches']:
        intensity = branch['mean_current'] * group['power_to_luminance']
        intervals.append((branch['head_id'], intensity, intensity))
    analysis = joint_feasibility(intervals, group['budget'], group['required_luminance'])
    outcome = _finish_verdict(db, e, group['id'], analysis, e['window_from'], e['window_to'],
                              'calibrated_historical_branch_current', group['state'])
    at = e.get('recorded_at', e['window_to'])
    # Re-cut only replacement work that has not been executed; executed closures
    # remain on record and are never rewritten as if they never happened.
    _action(db, e, group['id'], 'targeted_historical_work_order', 'group', None,
            e['window_from'], e['window_to'], at, 'historical_current_locates_dim_head')
    _action(db, e, group['id'], 'lane_reopen_after_targeted_repair', 'group', None,
            e['window_from'], e['window_to'], at,
            'prior_close_lane_record_retained_traceably')
    return outcome


def current_gap(db, e):
    """Route B: missing historical current pages. Keep closure, add temp light
    and forward-only current clamps; the old window stays unlocated."""
    group = one(db, 'SELECT * FROM power_group WHERE id=?', (e['group_id'],))
    if not group:
        raise ValueError('unknown_power_group')
    db.execute('INSERT INTO current_reading(id,group_id,branch_id,head_id,window_from,window_to,'
               'mean_current,calibrated_clock,clock_offset_us,complete,covers_historical_window,'
               'present,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,0,?)',
               (e['id'], group['id'], '*both*', '*group*', e['window_from'], e['window_to'],
                0.0, group['clock_ref'], 0, e['recorded_at']))
    at = e['recorded_at']
    _action(db, e, group['id'], 'close_lane_retained', 'group', None,
            group['observed_from'], group['observed_to'], at,
            'historical_current_pages_missing_old_window_remains_unlocated')
    _action(db, e, group['id'], 'temporary_red_installed', 'temp', e['temp_head_id'],
            None, None, at,
            'independent_temporary_signal_luminance_not_counted_in_original_head_group')
    for branch in e['clamps']:
        if branch['head_id'] not in set(json.loads(group['head_ids_json'])):
            raise ValueError('clamp_branch_not_in_group')
        db.execute('INSERT INTO clamp_reading(id,group_id,branch_id,head_id,installed_at,'
                   'window_from,window_to,mean_current,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (branch['clamp_id'], group['id'], branch['branch_id'], branch['head_id'],
                    e['installed_at'], e['installed_at'], e['installed_at'], 0.0, e['recorded_at']))
        _action(db, e, group['id'], 'current_clamp_installed', 'head', branch['head_id'],
                e['installed_at'], None, at,
                'clamp_locates_only_future_windows_cannot_backfill_historical_event')
    log(db, e, 'head-health', group['id'], 'unlocated', 'historical_current_gap_forward_only_clamps')
    last = one(db, 'SELECT MAX(revision) AS r FROM health_verdict WHERE group_id=?', (group['id'],))
    return {'revision': last['r'], 'min_dim_heads': None,
            'confirmed_dim': [], 'unresolved': None, 'control': 'close_lane_retained',
            'head_status': {}}


def clamp_reading(db, e):
    """Post-install clamp evidence: valid only for windows after installation."""
    clamp = one(db, 'SELECT * FROM clamp_reading WHERE id=?', (e['clamp_id'],))
    if not clamp:
        raise ValueError('unknown_clamp')
    group = one(db, 'SELECT * FROM power_group WHERE id=?', (clamp['group_id'],))
    if e['window_from'] < clamp['installed_at']:
        raise ValueError('clamp_cannot_backfill_historical_window')
    peers = many(db, 'SELECT DISTINCT head_id,branch_id FROM clamp_reading WHERE group_id=?', (group['id'],))
    known = {row['head_id'] for row in peers}
    if e['head_id'] not in known:
        raise ValueError('clamp_branch_not_in_group')
    intervals = []
    measured = {e['head_id']: float(e['mean_current']) * group['power_to_luminance']}
    for hid in known:
        if hid in measured:
            intervals.append((hid, measured[hid], measured[hid]))
        else:
            scan = one(db, 'SELECT * FROM head_scan WHERE head_id=? AND controller_id=? LIMIT 1',
                       (hid, group['controller_id']))
            lo, hi = camera_interval(scan)
            intervals.append((hid, lo, hi))
    analysis = joint_feasibility(intervals, group['budget'], group['required_luminance'])
    db.execute('UPDATE clamp_reading SET window_from=?,window_to=?,mean_current=?,recorded_at=? WHERE id=?',
               (e['window_from'], e['window_to'], float(e['mean_current']), e['recorded_at'], e['clamp_id']))
    outcome = _finish_verdict(db, e, group['id'], analysis, e['window_from'], e['window_to'],
                              'post_install_current_clamp', group['state'])
    _action(db, e, group['id'], 'future_window_attribution', 'window', e['head_id'],
            e['window_from'], e['window_to'], e['recorded_at'],
            'clamp_evidence_binds_only_post_install_window')
    return outcome
