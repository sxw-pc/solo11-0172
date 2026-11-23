"""Provisional site visit from a single water-level threshold.

``inspect`` is an attendance threshold for a field crew.  It is neither a
blockage localization nor a pumped-volume estimate: a level at or above one
metre only proves the crew should attend, never which outlet (if any) is
blocked or how much water must be moved.  Readings outside the well
calibration window cannot even reach that threshold and are *held*.
"""
from .store import one, log


def visit(db, e):
    row = one(db, 'SELECT * FROM drain_level WHERE id=?', (e['level_id'],))
    if not row:
        raise ValueError('unknown_drain_level')
    if row['state'] != 'captured':
        state, reason = 'held', 'level_not_valid'
    elif row['depth_m'] >= 1.0:
        state, reason = 'inspect', 'high_water_level'
    else:
        state, reason = 'monitor', 'below_visit_threshold'
    db.execute('INSERT INTO drain_visit VALUES(?,?,?,?,?)',
               (e['visit_id'], row['id'], row['well_id'], state, reason))
    log(db, e, 'draincrew', e['visit_id'], state, reason)
    return state
