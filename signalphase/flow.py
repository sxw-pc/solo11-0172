"""Detector eligibility, duplicate vehicle evidence and demand revision."""
from .store import one,many,log

def detector(db,e):
    if e['valid_from']>=e['valid_to'] or e['revision']<1:raise ValueError('invalid_detector_period')
    old=one(db,'SELECT * FROM detector WHERE id=?',(e['detector_id'],))
    if old and e['revision']<=old['revision']:
        if e['revision']==old['revision'] and (old['approach'],old['valid_from'],old['valid_to'])!=(e['approach'],e['valid_from'],e['valid_to']):
            db.execute("UPDATE detector SET state='conflict' WHERE id=?",(e['detector_id'],))
            log(db,e,'flow',e['detector_id'],'conflict','parallel_detector_revision');return 'conflict'
        return 'stale'
    db.execute("INSERT INTO detector VALUES(?,?,?,?,?,'valid') ON CONFLICT(id) DO UPDATE SET approach=excluded.approach,valid_from=excluded.valid_from,valid_to=excluded.valid_to,revision=excluded.revision,state='valid'",
               (e['detector_id'],e['approach'],e['valid_from'],e['valid_to'],e['revision']))
    log(db,e,'flow',e['detector_id'],'valid','calibration_registered')
    return 'valid'

def sample(db,e):
    d=one(db,'SELECT * FROM detector WHERE id=?',(e['detector_id'],))
    if not d:raise ValueError('unknown_detector')
    if not e['vehicle_hash'] or not e['window_id']:raise ValueError('sample_identity_required')
    if d['state']!='valid' or not (d['valid_from']<=e['observed_at']<d['valid_to']):
        state,reason='excluded','detector_not_calibrated_at_event'
    elif e.get('speed_kph',0)>150:
        state,reason='excluded','implausible_speed_outlier'
    elif one(db,'SELECT 1 FROM disrupted_window WHERE window_id=? AND from_at<=? AND to_at>=?',(e['window_id'],e['observed_at'],e['observed_at'])):
        state,reason='excluded','field_fault_disrupted_flow_window'
    elif one(db,"SELECT 1 FROM vehicle_sample WHERE vehicle_hash=? AND window_id=? AND state='counted'",(e['vehicle_hash'],e['window_id'])):
        state,reason='excluded','vehicle_duplicate_across_detectors'
    else:state,reason='counted','independent_vehicle'
    db.execute('INSERT INTO vehicle_sample VALUES(?,?,?,?,?,?,?,?)',
               (e['sample_id'],e['detector_id'],e['vehicle_hash'],e['observed_at'],e['window_id'],d['approach'],state,reason))
    recalc(db,e['window_id'],d['approach'],e)
    log(db,e,'flow',e['sample_id'],state,reason)
    return state

def recalc(db,window,approach,e):
    count=one(db,"SELECT COUNT(*) AS n FROM vehicle_sample WHERE window_id=? AND approach=? AND state='counted'",(window,approach))['n']
    old=one(db,'SELECT * FROM demand WHERE window_id=? AND approach=?',(window,approach))
    rev=1 if not old else old['revision']+1
    state='evidenced' if count>=2 else 'low_sample'
    db.execute('INSERT INTO demand VALUES(?,?,?,?,?) ON CONFLICT(window_id,approach) DO UPDATE SET valid_count=excluded.valid_count,revision=excluded.revision,state=excluded.state',
               (window,approach,count,rev,state))
    from .plans import sync_demand
    sync_demand(db,window,approach,e)

def revoke(db,e):
    d=one(db,'SELECT * FROM detector WHERE id=?',(e['detector_id'],))
    if not d:raise ValueError('unknown_detector')
    db.execute("UPDATE detector SET state='revoked' WHERE id=?",(e['detector_id'],))
    affected=many(db,"SELECT * FROM vehicle_sample WHERE detector_id=? AND observed_at>=? AND state='counted'",(e['detector_id'],e['effective_at']))
    for row in affected:
        db.execute("UPDATE vehicle_sample SET state='excluded',reason='calibration_revoked' WHERE id=?",(row['id'],))
        recalc(db,row['window_id'],row['approach'],e)
    log(db,e,'flow',e['detector_id'],'revoked','calibration_revoked')
    return 'revoked'
