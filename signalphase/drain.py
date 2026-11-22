"""Curbside drainage well metadata and level readings."""
from .store import one, log


def well(db, e):
    if e['storage_area_m2'] <= 0 or e['valid_from'] >= e['valid_to']:
        raise ValueError('invalid_drain_well')
    db.execute('INSERT INTO drain_well VALUES(?,?,?,?,?)',
               (e['well_id'], e['approach'], e['storage_area_m2'], e['valid_from'], e['valid_to']))
    log(db, e, 'drain', e['well_id'], 'registered', 'well_profile')
    return 'registered'


def level(db, e):
    well = one(db, 'SELECT * FROM drain_well WHERE id=?', (e['well_id'],))
    if not well:
        raise ValueError('unknown_drain_well')
    state = 'captured' if well['valid_from'] <= e['observed_at'] < well['valid_to'] and e['depth_m'] >= 0 else 'excluded'
    db.execute('INSERT INTO drain_level VALUES(?,?,?,?,?)',
               (e['level_id'], e['well_id'], e['observed_at'], e['depth_m'], state))
    log(db, e, 'drain', e['level_id'], state, 'well_valid' if state == 'captured' else 'well_invalid')
    return state
