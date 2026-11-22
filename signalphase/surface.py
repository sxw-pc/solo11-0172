"""Pavement infrared brightness observations, without ice inference."""
from .store import one, log


def station(db, e):
    old = one(db, 'SELECT * FROM surface_station WHERE id=?', (e['station_id'],))
    if old and e['revision'] <= old['revision']:
        raise ValueError('stale_station_revision')
    if e['valid_from'] >= e['valid_to']:
        raise ValueError('invalid_station_period')
    db.execute('INSERT INTO surface_station VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lane_id=excluded.lane_id,valid_from=excluded.valid_from,valid_to=excluded.valid_to,revision=excluded.revision',
               (e['station_id'], e['lane_id'], e['valid_from'], e['valid_to'], e['revision']))
    log(db, e, 'surface', e['station_id'], 'registered', 'valid_station')
    return 'registered'


def frame(db, e):
    sensor = one(db, 'SELECT * FROM surface_station WHERE id=?', (e['station_id'],))
    if not sensor or sensor['lane_id'] != e['lane_id'] or not (sensor['valid_from'] <= e['observed_at'] < sensor['valid_to']):
        state = 'excluded'
    else:
        state = 'captured'
    db.execute('INSERT INTO surface_frame VALUES(?,?,?,?,?,?,?)',
               (e['frame_id'], e['station_id'], e['lane_id'], e['observed_at'], e['brightness_c'], e['sky_c'], state))
    log(db, e, 'surface', e['frame_id'], state, 'station_valid' if state == 'captured' else 'station_invalid')
    return state
