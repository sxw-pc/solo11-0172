"""Ice-period verification and reopening publication for one lane.

A coating keeps one emissivity for a whole maintenance cycle, so every
frame of a cycle is evaluated against the same retained-sample range, and
a laboratory certificate pins one value for the whole cycle - never one
value per frame.  Freeze/melt boundaries are stored as the interval
between two observations, never as a pseudo-precise instant.  Future
contact measurements can support a reopening from their own time onward
but never rewrite the historical assessment, and a late original
certificate revises history without deleting executed closures,
reopenings or detours.  A watch is only a conservative patrol reminder,
not measured proof of ice.
"""
from .store import one, many, log

FREEZING_C = 0.0


def surface_temperature(brightness_c, sky_c, emissivity):
    """Invert the narrow-band calibration B = eps*T + (1-eps)*S."""
    return (brightness_c - sky_c) / emissivity + sky_c


def temperature_range(brightness_c, sky_c, emissivity_min, emissivity_max):
    """Possible surface temperatures over the retained-sample range."""
    pair = (surface_temperature(brightness_c, sky_c, emissivity_min),
            surface_temperature(brightness_c, sky_c, emissivity_max))
    return min(pair), max(pair)


def classify(t_min, t_max, water):
    """Ice state for one moment from a temperature range and water evidence."""
    if t_min > FREEZING_C:
        return 'no_ice', 'temperatures_above_freezing'
    if water == 'absent':
        return 'no_ice', 'water_film_absent'
    if t_max < FREEZING_C:
        if water == 'present':
            return 'ice', 'sub_freezing_temperatures_with_water_film'
        return 'possible_ice', 'sub_freezing_temperatures_water_unproven'
    return 'possible_ice', 'temperature_range_crosses_freezing'


def water_state_at(db, lane_id, at):
    """Latest independent water evidence at or before `at`.

    A water film only counts as gone when some sensor or patrol explicitly
    reported it gone; silence never proves disappearance.
    """
    film = one(db, 'SELECT observed_at AS at, state FROM water_film WHERE lane_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1',
               (lane_id, at))
    contact = one(db, 'SELECT measured_at AS at, water_present FROM contact_measure WHERE lane_id=? AND measured_at<=? ORDER BY measured_at DESC LIMIT 1',
                  (lane_id, at))
    candidates = []
    if film:
        candidates.append((film['at'], film['state']))
    if contact:
        candidates.append((contact['at'], 'present' if contact['water_present'] else 'absent'))
    if not candidates:
        return 'unknown'
    return max(candidates)[1]


def coating(db, e):
    if not (0 < e['emissivity_min'] <= e['emissivity_max'] <= 1):
        raise ValueError('invalid_emissivity_range')
    db.execute('INSERT INTO coating_sample VALUES(?,?,?,?,?,?,?)',
               (e['coating_id'], e['lane_id'], e['cycle_id'], e['emissivity_min'], e['emissivity_max'],
                e['retained_at'], 'retained'))
    log(db, e, 'ice', e['coating_id'], 'retained', 'coating_sample_registered')
    return 'retained'


def water(db, e):
    if e['state'] not in ('present', 'absent'):
        raise ValueError('invalid_water_state')
    db.execute('INSERT INTO water_film VALUES(?,?,?,?,?)',
               (e['water_id'], e['lane_id'], e['observed_at'], e['state'], e['method']))
    log(db, e, 'ice', e['water_id'], e['state'], 'water_film_observed')
    if e['state'] == 'present':
        # A new water film never inherits an earlier reopening: published
        # reopenings are suspended and the lane goes back to pending review.
        window = one(db, 'SELECT cycle_id FROM ice_window WHERE lane_id=? ORDER BY rowid DESC LIMIT 1', (e['lane_id'],))
        for row in many(db, "SELECT * FROM lane_reopen WHERE lane_id=? AND state='published' AND from_at<=?",
                        (e['lane_id'], e['observed_at'])):
            db.execute("UPDATE lane_reopen SET state='superseded',reason='new_water_film_requires_recheck' WHERE id=?", (row['id'],))
            db.execute('INSERT INTO ice_todo VALUES(?,?,?,?,?,?)',
                       (f"{e['water_id']}:reassess:{row['id']}", e['lane_id'],
                        window['cycle_id'] if window else '', 'ice_reassessment', 'pending', 'new_water_film'))
            log(db, e, 'ice', row['id'], 'superseded', 'new_water_film_requires_recheck')
    return e['state']


def closure(db, e):
    db.execute('INSERT INTO lane_closure VALUES(?,?,?,?,?)',
               (e['closure_id'], e['lane_id'], e['closed_at'], e['actor'], e['reason']))
    log(db, e, 'ice', e['closure_id'], 'closed', 'executed_closure_recorded')
    return 'closed'


def detour(db, e):
    db.execute('INSERT INTO lane_detour VALUES(?,?,?,?)',
               (e['detour_id'], e['lane_id'], e['at'], e.get('note', '')))
    log(db, e, 'ice', e['detour_id'], 'recorded', 'executed_detour_recorded')
    return 'recorded'


def treatment(db, e):
    db.execute('INSERT INTO treatment VALUES(?,?,?,?,?)',
               (e['treatment_id'], e['lane_id'], e['at'], e['material'], e.get('actor', '')))
    log(db, e, 'ice', e['treatment_id'], 'applied', 'surface_treatment_recorded')
    return 'applied'


def contact(db, e):
    # A contact reading is evidence from its own time onward only; it never
    # touches the historical ice_frame/ice_window assessment.
    safe = e['temperature_c'] > FREEZING_C and not e['water_present']
    state, reason = ('safe', 'above_freezing_and_dry') if safe else ('unsafe', 'freezing_or_wet')
    db.execute('INSERT INTO contact_measure VALUES(?,?,?,?,?,?,?)',
               (e['measure_id'], e['lane_id'], e['measured_at'], e['temperature_c'],
                int(bool(e['water_present'])), state, reason))
    log(db, e, 'ice', e['measure_id'], state, reason)
    return state


def _responsibility(db, lane_id, window_state, danger_from):
    if window_state == 'no_ice':
        return 'none'
    if window_state != 'confirmed_ice':
        return 'undetermined'
    shut = one(db, 'SELECT * FROM lane_closure WHERE lane_id=? ORDER BY closed_at DESC LIMIT 1', (lane_id,))
    if not shut:
        return 'maintenance_no_closure'
    return 'maintenance_gap' if shut['closed_at'] > danger_from else 'closed_before_danger'


def _summarize(db, lane_id, rows, evidence_gap):
    """Window summary from per-frame states; the melt boundary stays an
    interval between the last dangerous and the first clear observation."""
    danger = [r for r in rows if r['state'] in ('ice', 'possible_ice')]
    if not danger:
        danger_from = melt_from = melt_to = None
        state, reason = 'no_ice', 'no_danger_frame_in_captured_evidence'
    else:
        danger_from = danger[0]['observed_at']
        melt_from = danger[-1]['observed_at']
        later_clear = [r for r in rows if r['state'] == 'no_ice' and r['observed_at'] > melt_from]
        melt_to = later_clear[0]['observed_at'] if later_clear else None
        if any(r['state'] == 'ice' for r in danger):
            state, reason = 'confirmed_ice', 'sub_freezing_temperatures_with_water_film'
        else:
            state, reason = 'possible_ice', 'temperature_range_crosses_freezing'
    if evidence_gap:
        reason += '+excluded_frame_gap'
    return danger_from, melt_from, melt_to, state, _responsibility(db, lane_id, state, danger_from), reason


def assess(db, e):
    sample = one(db, 'SELECT * FROM coating_sample WHERE lane_id=? AND cycle_id=?', (e['lane_id'], e['cycle_id']))
    if not sample:
        raise ValueError('unknown_coating_cycle')
    span = (e['lane_id'], e['from_at'], e['to_at'])
    frames = many(db, "SELECT * FROM surface_frame WHERE lane_id=? AND observed_at>=? AND observed_at<=? AND state='captured' ORDER BY observed_at",
                  span)
    if not frames:
        raise ValueError('no_captured_frames')
    gap = one(db, "SELECT 1 FROM surface_frame WHERE lane_id=? AND observed_at>=? AND observed_at<=? AND state!='captured' LIMIT 1",
              span) is not None
    rows = []
    for f in frames:
        t_min, t_max = temperature_range(f['brightness_c'], f['sky_c'],
                                         sample['emissivity_min'], sample['emissivity_max'])
        water_state = water_state_at(db, e['lane_id'], f['observed_at'])
        state, reason = classify(t_min, t_max, water_state)
        row_id = f"{e['assess_id']}:{f['id']}"
        db.execute('INSERT INTO ice_frame VALUES(?,?,?,?,?,?,?,?,?,?,1)',
                   (row_id, e['lane_id'], e['cycle_id'], f['id'], f['observed_at'],
                    t_min, t_max, water_state, state, reason))
        db.execute('INSERT INTO ice_frame_history SELECT * FROM ice_frame WHERE id=?', (row_id,))
        rows.append({'observed_at': f['observed_at'], 'state': state})
    danger_from, melt_from, melt_to, state, responsibility, reason = _summarize(db, e['lane_id'], rows, gap)
    db.execute('INSERT INTO ice_window VALUES(?,?,?,?,?,?,?,?,?,1)',
               (e['assess_id'], e['lane_id'], e['cycle_id'], danger_from, melt_from, melt_to,
                state, responsibility, reason))
    db.execute('INSERT INTO ice_window_history SELECT * FROM ice_window WHERE id=?', (e['assess_id'],))
    if state == 'possible_ice':
        db.execute('INSERT INTO ice_todo VALUES(?,?,?,?,?,?)',
                   (f"{e['assess_id']}:emissivity", e['lane_id'], e['cycle_id'],
                    'emissivity_certificate', 'pending', 'emissivity_range_unresolved'))
    log(db, e, 'ice', e['assess_id'], state, reason)
    return state


def certificate(db, e):
    sample = one(db, 'SELECT * FROM coating_sample WHERE lane_id=? AND cycle_id=?', (e['lane_id'], e['cycle_id']))
    if not sample:
        raise ValueError('unknown_coating_cycle')
    eps = e['emissivity']
    if not (sample['emissivity_min'] <= eps <= sample['emissivity_max']):
        raise ValueError('certificate_outside_sample_range')
    window = one(db, 'SELECT * FROM ice_window WHERE lane_id=? AND cycle_id=? ORDER BY rowid DESC LIMIT 1',
                 (e['lane_id'], e['cycle_id']))
    if not window:
        raise ValueError('no_assessment_to_revise')
    frames = many(db, 'SELECT * FROM ice_frame WHERE lane_id=? AND cycle_id=? ORDER BY observed_at',
                  (e['lane_id'], e['cycle_id']))
    if e['retained_at'] > min(f['observed_at'] for f in frames):
        raise ValueError('certificate_not_predating_footage')
    # One certified emissivity is applied to every frame of the cycle; the
    # executed closure/detour records are facts and stay untouched.
    rows = []
    for f in frames:
        surf = one(db, 'SELECT * FROM surface_frame WHERE id=?', (f['frame_id'],))
        t = surface_temperature(surf['brightness_c'], surf['sky_c'], eps)
        water_state = water_state_at(db, e['lane_id'], f['observed_at'])
        state, reason = classify(t, t, water_state)
        db.execute('UPDATE ice_frame SET t_min=?,t_max=?,water=?,state=?,reason=?,revision=revision+1 WHERE id=?',
                   (t, t, water_state, state, reason, f['id']))
        db.execute('INSERT INTO ice_frame_history SELECT * FROM ice_frame WHERE id=?', (f['id'],))
        rows.append({'observed_at': f['observed_at'], 'state': state})
    danger_from, melt_from, melt_to, state, responsibility, reason = _summarize(
        db, e['lane_id'], rows, '+excluded_frame_gap' in window['reason'])
    db.execute('UPDATE ice_window SET danger_from=?,melt_from=?,melt_to=?,state=?,responsibility=?,reason=?,revision=revision+1 WHERE id=?',
               (danger_from, melt_from, melt_to, state, responsibility, reason, window['id']))
    db.execute('INSERT INTO ice_window_history SELECT * FROM ice_window WHERE id=?', (window['id'],))
    db.execute("UPDATE ice_todo SET state='fulfilled',reason='certificate_applied' WHERE lane_id=? AND cycle_id=? AND kind='emissivity_certificate' AND state='pending'",
               (e['lane_id'], e['cycle_id']))
    db.execute('INSERT INTO emissivity_certificate VALUES(?,?,?,?,?,?)',
               (e['certificate_id'], e['lane_id'], e['cycle_id'], eps, e['retained_at'], 'applied'))
    log(db, e, 'ice', e['certificate_id'], 'history_revised', reason)
    return 'history_revised'


def reopen(db, e):
    basis = e['basis']
    if basis not in ('contact_measurement', 'certificate_melt_boundary'):
        raise ValueError('unknown_reopen_basis')
    state, reason = 'published', 'reopen_published'
    if basis == 'contact_measurement':
        if not one(db, 'SELECT 1 FROM treatment WHERE lane_id=? AND at<=? LIMIT 1', (e['lane_id'], e['from_at'])):
            state, reason = 'held', 'anti_skid_treatment_required'
        else:
            latest = one(db, 'SELECT * FROM contact_measure WHERE lane_id=? AND measured_at<=? ORDER BY measured_at DESC LIMIT 1',
                         (e['lane_id'], e['from_at']))
            if not latest or latest['state'] != 'safe':
                state, reason = 'held', 'no_safe_contact_measurement'
            elif one(db, "SELECT 1 FROM water_film WHERE lane_id=? AND observed_at>? AND observed_at<=? AND state='present' LIMIT 1",
                     (e['lane_id'], latest['measured_at'], e['from_at'])):
                state, reason = 'held', 'water_film_present_at_reopen'
    else:
        window = one(db, 'SELECT * FROM ice_window WHERE lane_id=? ORDER BY rowid DESC LIMIT 1', (e['lane_id'],))
        if not window or window['state'] != 'confirmed_ice' or not window['melt_to']:
            state, reason = 'held', 'certificate_not_applied'
        elif e['from_at'] < window['melt_to']:
            state, reason = 'held', 'melt_boundary_not_reached'
    db.execute('INSERT INTO lane_reopen VALUES(?,?,?,?,?,?)',
               (e['reopen_id'], e['lane_id'], e['from_at'], basis, state, reason))
    log(db, e, 'ice', e['reopen_id'], state, reason)
    return state
