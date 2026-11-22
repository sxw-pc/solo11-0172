"""Provisional one-dimensional fiber distance triage."""
from .store import one, log


def check(db, e):
    echo = one(db, 'SELECT * FROM fiber_echo WHERE id=?', (e['echo_id'],))
    if not echo:
        raise ValueError('unknown_fiber_echo')
    route = one(db, 'SELECT * FROM fiber_route WHERE id=?', (echo['route_id'],))
    distance = echo['return_us'] * route['propagation_km_per_us'] / 2
    state = 'splice_inspection' if abs(distance-route['splice_km']) <= 0.05 else 'line_inspection'
    db.execute('INSERT INTO fiber_triage VALUES(?,?,?,?)',
               (e['check_id'], e['echo_id'], distance, state))
    log(db, e, 'fibertriage', e['check_id'], state, 'single_end_distance')
    return state
