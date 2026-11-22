"""Curbside drainage well metadata, water levels and physical evidence.

Only calibrated well geometry may turn a water depth into a stored volume.
Every other record (outlet capacity, catchment area, radar intensity, flow
meter, pump power, crew work) is kept as its own original row so that a later
version of a dispatch can be added without rewriting what was already done.
"""
from .store import one, log


def _positive(value):
    return isinstance(value, (int, float)) and value > 0


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


def outlet(db, e):
    if not _positive(e['capacity_m3h']):
        raise ValueError('invalid_drain_outlet')
    if one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)) is None:
        raise ValueError('unknown_drain_well')
    db.execute('INSERT INTO drain_outlet VALUES(?,?,?,?,?)',
               (e['outlet_id'], e['well_id'], e['name'], e['capacity_m3h'], e.get('state', 'open')))
    log(db, e, 'drain', e['outlet_id'], 'registered', 'outlet_capacity')
    return 'registered'


def catchment(db, e):
    if not _positive(e['area_m2']) or not 0 < e['runoff_coefficient'] <= 1:
        raise ValueError('invalid_drain_catchment')
    if one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)) is None:
        raise ValueError('unknown_drain_well')
    radar = 1 if e.get('radar_covered') else 0
    db.execute('INSERT INTO drain_catchment VALUES(?,?,?,?,?,?,?)',
               (e['catchment_id'], e['well_id'], e['name'], e.get('surface', 'surface'),
                e['area_m2'], e['runoff_coefficient'], radar))
    log(db, e, 'drain', e['catchment_id'], 'registered', 'catchment_boundary')
    return 'registered'


def rainfall(db, e):
    """Radar rain intensity is never a well inflow; convert with area and loss."""
    row = one(db, 'SELECT * FROM drain_catchment WHERE catchment_id=?', (e['catchment_id'],))
    if not row:
        raise ValueError('unknown_drain_catchment')
    if e['intensity_mm_h'] < 0 or not 0 < e['effective_area_m2'] <= row['area_m2'] or not 0 < e['loss_coefficient'] <= 1:
        raise ValueError('invalid_rainfall_evidence')
    rate = e['intensity_mm_h'] / 1000.0 * e['effective_area_m2'] * e['loss_coefficient']
    db.execute('INSERT INTO drain_rainfall VALUES(?,?,?,?,?,?,?,?,?)',
               (e['rain_id'], e['catchment_id'], e['from_at'], e['to_at'], e['intensity_mm_h'],
                e['effective_area_m2'], e['loss_coefficient'], round(rate, 6), e['recorded_at']))
    log(db, e, 'drain', e['rain_id'], 'recorded', 'rainfall_converted_by_area_and_loss')
    return 'recorded'


def flow(db, e):
    """A measured flow rate over an interval; `measures_total` marks a total flow."""
    if one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)) is None:
        raise ValueError('unknown_drain_well')
    if e['direction'] not in ('inflow', 'outflow') or e['rate_m3h'] < 0 or e['from_at'] >= e['to_at']:
        raise ValueError('invalid_flow_evidence')
    if e['direction'] == 'outflow' and e.get('outlet_id'):
        if one(db, 'SELECT outlet_id FROM drain_outlet WHERE outlet_id=? AND well_id=?',
               (e['outlet_id'], e['well_id'])) is None:
            raise ValueError('unknown_drain_outlet')
    if e.get('catchment_id') and one(db, 'SELECT catchment_id FROM drain_catchment WHERE catchment_id=?',
                                     (e['catchment_id'],)) is None:
        raise ValueError('unknown_drain_catchment')
    db.execute('INSERT INTO drain_flow VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['flow_id'], e['well_id'], e['direction'], e.get('source', 'gauge'),
                e.get('outlet_id'), e.get('catchment_id'), e['from_at'], e['to_at'],
                e['rate_m3h'], 1 if e.get('measures_total') else 0,
                'valid', '', e['recorded_at']))
    log(db, e, 'drain', e['flow_id'], 'recorded', 'flow_evidence')
    return 'recorded'


def pump(db, e):
    """Pump power only proves an operating condition, never a discharge rate."""
    if one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)) is None:
        raise ValueError('unknown_drain_well')
    powered = 1 if e.get('powered') else 0
    db.execute('INSERT INTO drain_pump VALUES(?,?,?,?,?,?)',
               (e['pump_event_id'], e['well_id'], e['pump_id'], e['at'], powered, e['recorded_at']))
    log(db, e, 'draincrew', e['pump_event_id'], 'powered' if powered else 'unpowered', 'pump_condition_only')
    return 'powered' if powered else 'unpowered'


def work(db, e):
    """Executed crew time and discharged volume. Insert-only: never rewritten."""
    if one(db, 'SELECT id FROM drain_well WHERE id=?', (e['well_id'],)) is None:
        raise ValueError('unknown_drain_well')
    if e['work_kind'] not in ('arrival', 'clearing', 'temporary_pump', 'discharge'):
        raise ValueError('invalid_work_kind')
    if e['hours'] < 0 or e.get('volume_m3', 0) < 0:
        raise ValueError('invalid_work_record')
    dispatch_id = e.get('dispatch_id')
    revision = e.get('dispatch_revision')
    if dispatch_id is not None:
        if one(db, 'SELECT 1 FROM drain_dispatch WHERE dispatch_id=? AND revision=?',
               (dispatch_id, revision)) is None:
            raise ValueError('unknown_drain_dispatch')
    db.execute('INSERT INTO drain_work VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['work_id'], e['well_id'], e['work_kind'], e['at'], e.get('crew', 'unknown'),
                e['hours'], e.get('volume_m3', 0), dispatch_id, revision, e.get('note', '')))
    log(db, e, 'draincrew', e['work_id'], 'recorded', 'executed_work_original')
    return 'recorded'
