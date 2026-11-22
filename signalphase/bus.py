"""Bus trips and aggregated door counts at a signalized approach."""
from .store import one, log


def trip(db, e):
    if e['capacity'] < 1 or e['initial_load'] < 0:
        raise ValueError('invalid_bus_capacity')
    db.execute('INSERT INTO bus_trip VALUES(?,?,?,?,?)',
               (e['trip_id'], e['route_id'], e['approach'], e['capacity'], e['initial_load']))
    log(db, e, 'bus', e['trip_id'], 'registered', 'trip_capacity')
    return 'registered'


def batch(db, e):
    if not one(db, 'SELECT 1 FROM bus_trip WHERE id=?', (e['trip_id'],)):
        raise ValueError('unknown_bus_trip')
    if e['window_from'] >= e['window_to'] or e['board_count'] < 0 or e['alight_count'] < 0:
        raise ValueError('invalid_door_batch')
    seq = e.get('stop_seq')
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 1):
        raise ValueError('invalid_door_batch')
    db.execute('INSERT INTO bus_door_batch VALUES(?,?,?,?,?,?,?,?)',
               (e['batch_id'], e['trip_id'], e['stop_id'], e['window_from'], e['window_to'],
                e['board_count'], e['alight_count'], seq))
    log(db, e, 'bus', e['batch_id'], 'captured', 'aggregate_door_counts')
    return 'captured'
