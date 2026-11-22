"""Raw fiber route geometry and single-ended reflection observations."""
from .store import one, log


def route(db, e):
    if min(e['propagation_km_per_us'], e['splice_km'], e['north_branch_km'], e['south_branch_km']) <= 0:
        raise ValueError('invalid_fiber_route')
    db.execute('INSERT INTO fiber_route VALUES(?,?,?,?,?,?)',
               (e['route_id'], e['controller_id'], e['propagation_km_per_us'], e['splice_km'],
                e['north_branch_km'], e['south_branch_km']))
    log(db, e, 'fiber', e['route_id'], 'registered', 'route_geometry')
    return 'registered'


def echo(db, e):
    if not one(db, 'SELECT 1 FROM fiber_route WHERE id=?', (e['route_id'],)):
        raise ValueError('unknown_fiber_route')
    if e['return_us'] <= 0:
        raise ValueError('invalid_echo_time')
    db.execute('INSERT INTO fiber_echo VALUES(?,?,?,?,?)',
               (e['echo_id'], e['route_id'], e['measured_at'], e['return_us'], e['amplitude']))
    log(db, e, 'fiber', e['echo_id'], 'captured', 'single_ended_echo')
    return 'captured'
