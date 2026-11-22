"""Phase safety, pedestrian clearance and versioned approval."""
import hashlib,json,math
from .store import one,many,log

def plan_digest(e):
    public={k:e[k] for k in ('plan_id','revision','window_id','approach','ped_distance_m','ped_green_sec','vehicle_green_sec','phases')}
    return hashlib.sha256(json.dumps(public,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def safety(e):
    if e['ped_distance_m']<=0 or e['ped_green_sec']<=0 or e['vehicle_green_sec']<=0:
        return 'invalid_timing'
    if e['ped_green_sec']<math.ceil(e['ped_distance_m']/0.8):return 'pedestrian_clearance_short'
    groups=e['phases']['green_groups'];conflicts=e['phases']['conflicts']
    if not groups or any(len(group)!=len(set(group)) for group in groups):return 'invalid_phase_group'
    if any(a in group and b in group for group in groups for a,b in conflicts):return 'conflicting_green_phase'
    if any('pedestrian' in group and 'right_turn' in group for group in groups):return 'pedestrian_right_turn_conflict'
    if not any('pedestrian' in group for group in groups):return 'pedestrian_phase_missing'
    return None

def submit(db,e):
    old=one(db,'SELECT * FROM phase_plan WHERE id=?',(e['plan_id'],))
    digest=plan_digest(e)
    if old and e['revision']<=old['revision']:
        if e['revision']==old['revision'] and digest!=old['sha']:raise ValueError('parallel_plan_revision')
        return 'stale'
    reason=safety(e)
    if not reason and (not e.get('approved_by') or e.get('approval_role')!='traffic_engineer' or e['approved_from']>=e['approved_to']):reason='invalid_approval'
    demand=one(db,'SELECT * FROM demand WHERE window_id=? AND approach=?',(e['window_id'],e['approach']))
    if not reason and (not demand or demand['state']!='evidenced'):reason='insufficient_flow_evidence'
    status='approved' if not reason else 'held'
    db.execute('INSERT INTO phase_plan VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,window_id=excluded.window_id,approach=excluded.approach,ped_distance_m=excluded.ped_distance_m,ped_green_sec=excluded.ped_green_sec,vehicle_green_sec=excluded.vehicle_green_sec,phases_json=excluded.phases_json,approved_by=excluded.approved_by,approved_from=excluded.approved_from,approved_to=excluded.approved_to,sha=excluded.sha,status=excluded.status,reason=excluded.reason',
      (e['plan_id'],e['revision'],e['window_id'],e['approach'],e['ped_distance_m'],e['ped_green_sec'],e['vehicle_green_sec'],json.dumps(e['phases'],sort_keys=True),e.get('approved_by',''),e['approved_from'],e['approved_to'],digest,status,reason or 'safe_and_evidenced'))
    db.execute('INSERT INTO phase_plan_history SELECT * FROM phase_plan WHERE id=?',(e['plan_id'],))
    db.execute('INSERT INTO approval(plan_id,revision,reviewer,at,decision,reason) VALUES(?,?,?,?,?,?)',
      (e['plan_id'],e['revision'],e.get('approved_by',''),e.get('recorded_at',e['approved_from']),status,reason or 'safe_and_evidenced'))
    log(db,e,'plan',e['plan_id'],status,reason or 'safe_and_evidenced')
    return status

def recheck_pending(db,window,approach,e):
    demand=one(db,'SELECT * FROM demand WHERE window_id=? AND approach=?',(window,approach))
    if not demand or demand['state']!='evidenced':return
    for row in many(db,"SELECT * FROM phase_plan WHERE window_id=? AND approach=? AND status='held' AND reason IN ('insufficient_flow_evidence','flow_evidence_revoked')",(window,approach)):
        db.execute("UPDATE phase_plan SET status='approved',reason='late_flow_evidence' WHERE id=?",(row['id'],))
        db.execute("UPDATE phase_plan_history SET status='approved',reason='late_flow_evidence' WHERE id=? AND revision=?",(row['id'],row['revision']))
        db.execute('INSERT INTO approval(plan_id,revision,reviewer,at,decision,reason) VALUES(?,?,?,?,?,?)',
          (row['id'],row['revision'],row['approved_by'],e.get('recorded_at','2026-01-01T00:00:00Z'),'approved','late_flow_evidence'))
        log(db,e,'plan',row['id'],'approved','late_flow_evidence')

def sync_demand(db,window,approach,e):
    demand=one(db,'SELECT * FROM demand WHERE window_id=? AND approach=?',(window,approach))
    if demand and demand['state']=='evidenced':
        recheck_pending(db,window,approach,e)
        return
    for row in many(db,"SELECT * FROM phase_plan WHERE window_id=? AND approach=? AND status='approved'",(window,approach)):
        active=one(db,'SELECT 1 FROM activation WHERE plan_id=?',(row['id'],))
        if active:continue
        db.execute("UPDATE phase_plan SET status='held',reason='flow_evidence_revoked' WHERE id=?",(row['id'],))
        db.execute("UPDATE phase_plan_history SET status='held',reason='flow_evidence_revoked' WHERE id=? AND revision=?",(row['id'],row['revision']))
        db.execute('INSERT INTO approval(plan_id,revision,reviewer,at,decision,reason) VALUES(?,?,?,?,?,?)',
          (row['id'],row['revision'],row['approved_by'],e.get('recorded_at','2026-01-01T00:00:00Z'),'held','flow_evidence_revoked'))
        log(db,e,'plan',row['id'],'held','flow_evidence_revoked')
