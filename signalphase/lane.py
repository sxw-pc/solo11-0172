"""Provisional lane watch from a single IR brightness value."""
from .store import one, log


def check(db, e):
    frame = one(db, 'SELECT * FROM surface_frame WHERE id=?', (e['frame_id'],))
    if not frame or frame['lane_id'] != e['lane_id']:
        raise ValueError('unknown_surface_frame')
    if frame['state'] != 'captured':
        state, reason = 'held', 'uncalibrated_surface_frame'
    elif frame['brightness_c'] < 0:
        state, reason = 'watch', 'negative_brightness_temperature'
    else:
        state, reason = 'open', 'nonnegative_brightness_temperature'
    db.execute('INSERT INTO lane_watch VALUES(?,?,?,?,?,?)',
               (e['check_id'], e['frame_id'], e['lane_id'], e['at'], state, reason))
    log(db, e, 'lane', e['check_id'], state, reason)
    return state
