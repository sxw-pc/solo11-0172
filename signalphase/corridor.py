"""A second junction's observed green window and provisional corridor request."""
from .store import one, log


def remote(db, e):
    if e['green_from'] >= e['green_to']:
        raise ValueError('invalid_remote_window')
    db.execute('INSERT INTO remote_signal_window VALUES(?,?,?,?,?)',
               (e['remote_id'], e['intersection_id'], e['green_from'], e['green_to'], int(e['verified'])))
    log(db, e, 'corridor', e['remote_id'], 'captured', 'remote_green_window')
    return 'captured'


def request(db, e):
    if not one(db, 'SELECT 1 FROM remote_signal_window WHERE id=?', (e['remote_id'],)):
        raise ValueError('unknown_remote_window')
    if not 0 <= e['travel_min_sec'] <= e['travel_max_sec']:
        raise ValueError('invalid_travel_interval')
    db.execute('INSERT INTO corridor_request VALUES(?,?,?,?,?,?)',
               (e['request_id'], e['controller_id'], e['requested_at'], e['remote_id'],
                e['travel_min_sec'], e['travel_max_sec']))
    log(db, e, 'corridor', e['request_id'], 'captured', 'route_request')
    return 'captured'


def check(db, e):
    route = one(db, 'SELECT * FROM corridor_request WHERE id=?', (e['request_id'],))
    if not route:
        raise ValueError('unknown_corridor_request')
    active = one(db, "SELECT * FROM activation WHERE controller_id=? AND started_at<=? AND (ended_at IS NULL OR ended_at>?) ORDER BY id DESC LIMIT 1",
                 (route['controller_id'], route['requested_at'], route['requested_at']))
    remote_window = one(db, 'SELECT * FROM remote_signal_window WHERE id=?', (route['remote_id'],))
    state = 'provisional' if active and remote_window['verified'] else 'held'
    db.execute('INSERT INTO corridor_check VALUES(?,?,?,?)',
               (e['check_id'], route['id'], active['plan_id'] if active else None, state))
    log(db, e, 'corridor', e['check_id'], state, 'verified_ends' if state == 'provisional' else 'missing_field_evidence')
    return state
