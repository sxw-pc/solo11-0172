"""Curbside drainage well metadata and level readings.

The well registration stores the calibrated shaft storage area and the
calibration validity window.  A level reading is only *captured* when its
observation time falls inside that window; an expired or not-yet-valid
calibration turns the reading into *excluded* evidence.  A high captured
water level is a site-visit threshold only — it never locates a blockage
and never estimates a drainage volume by itself.
"""
import math

from .store import one, log


def well(db, e):
    area = e['storage_area_m2']
    if not isinstance(area, (int, float)) or not math.isfinite(area) or area <= 0:
        raise ValueError('invalid_drain_well')
    if e['valid_from'] >= e['valid_to']:
        raise ValueError('invalid_drain_well')
    db.execute('INSERT INTO drain_well VALUES(?,?,?,?,?)',
               (e['well_id'], e['approach'], area, e['valid_from'], e['valid_to']))
    log(db, e, 'drain', e['well_id'], 'registered', 'well_profile')
    return 'registered'


def level(db, e):
    row = one(db, 'SELECT * FROM drain_well WHERE id=?', (e['well_id'],))
    if not row:
        raise ValueError('unknown_drain_well')
    depth = e['depth_m']
    if not isinstance(depth, (int, float)) or not math.isfinite(depth):
        raise ValueError('invalid_depth')
    inside = row['valid_from'] <= e['observed_at'] < row['valid_to']
    if inside and depth >= 0:
        state, reason = 'captured', 'well_valid'
    elif not inside:
        state, reason = 'excluded', 'well_calibration_expired'
    else:
        state, reason = 'excluded', 'negative_depth'
    db.execute('INSERT INTO drain_level VALUES(?,?,?,?,?)',
               (e['level_id'], e['well_id'], e['observed_at'], depth, state))
    log(db, e, 'drain', e['level_id'], state, reason)
    return state
