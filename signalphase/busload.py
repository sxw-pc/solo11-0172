"""Provisional load computed from aggregate net movement."""
from .store import one, log


def check(db, e):
    batch = one(db, 'SELECT * FROM bus_door_batch WHERE id=?', (e['batch_id'],))
    if not batch:
        raise ValueError('unknown_door_batch')
    trip = one(db, 'SELECT * FROM bus_trip WHERE id=?', (batch['trip_id'],))
    load = trip['initial_load'] + batch['board_count'] - batch['alight_count']
    state = 'nominal_within' if 0 <= load <= trip['capacity'] else 'nominal_outside'
    db.execute('INSERT INTO bus_load VALUES(?,?,?,?,?)',
               (e['check_id'], e['batch_id'], trip['id'], load, state))
    log(db, e, 'busload', e['check_id'], state, 'aggregate_net_load')
    return state
