"""Provisional load computed from aggregate net movement along the trip chain."""
from .store import one, many, log


def chain_start(db, trip, batch):
    """True pre-stop load: trip initial load plus net movement of every prior stop.

    Returns (start_load, chain) where chain is 'trip_initial' for the first or an
    unsequenced stop, 'chained' when every earlier stop of the trip is summarized,
    and 'chain_gap' when an intermediate stop is missing so the start load cannot
    be treated as closed.
    """
    seq = batch['stop_seq']
    if seq is None or seq <= 1:
        return trip['initial_load'], 'trip_initial'
    priors = many(db, 'SELECT * FROM bus_door_batch WHERE trip_id=? AND stop_seq IS NOT NULL AND stop_seq<? '
                      'ORDER BY stop_seq', (trip['id'], seq))
    start = trip['initial_load'] + sum(b['board_count'] - b['alight_count'] for b in priors)
    known = {b['stop_seq'] for b in priors}
    chain = 'chained' if all(s in known for s in range(1, seq)) else 'chain_gap'
    return start, chain


def check(db, e):
    batch = one(db, 'SELECT * FROM bus_door_batch WHERE id=?', (e['batch_id'],))
    if not batch:
        raise ValueError('unknown_door_batch')
    trip = one(db, 'SELECT * FROM bus_trip WHERE id=?', (batch['trip_id'],))
    start, chain = chain_start(db, trip, batch)
    load = start + batch['board_count'] - batch['alight_count']
    state = 'nominal_within' if 0 <= load <= trip['capacity'] else 'nominal_outside'
    db.execute('INSERT INTO bus_load VALUES(?,?,?,?,?,?,?)',
               (e['check_id'], e['batch_id'], trip['id'], start, load, chain, state))
    log(db, e, 'busload', e['check_id'], state, 'aggregate_net_load')
    return state
