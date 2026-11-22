"""Controller readback, activation and safe baseline rollback."""
import json
from .plans import plan_digest
from .corridor_capacity import invalidate_controller
from .store import one,many,log

def upload(db,e):
    plan=one(db,'SELECT * FROM phase_plan WHERE id=?',(e['plan_id'],))
    if not plan:raise ValueError('unknown_plan')
    if plan['status']!='approved':raise ValueError('plan_not_approved')
    if not (plan['approved_from']<=e['uploaded_at']<plan['approved_to']):raise ValueError('approval_window_expired')
    try:
        readback_sha=plan_digest({**e.get('readback',{}),'plan_id':e['plan_id'],'revision':plan['revision'],
                                 'window_id':plan['window_id'],'approach':plan['approach']})
    except KeyError:
        readback_sha='missing_readback'
    match=readback_sha==plan['sha'] and e['readback_at']>=e['uploaded_at']
    state='active' if match else 'rejected'
    reason='readback_matches_approved' if match else 'controller_version_or_timing_mismatch'
    db.execute('INSERT INTO controller VALUES(?,?,?,?,?,?,?,?)',
      (e['controller_id'],e['plan_id'],plan['revision'],e['uploaded_at'],readback_sha,e['readback_at'],state,reason))
    if match:
        db.execute("UPDATE activation SET ended_at=?,status='replaced',reason='new_plan_active' WHERE status='active'",(e['readback_at'],))
        db.execute('INSERT INTO activation(plan_id,plan_revision,controller_id,started_at,status,reason) VALUES(?,?,?,?,?,?)',
          (e['plan_id'],plan['revision'],e['controller_id'],e['readback_at'],'active','confirmed_readback'))
    else:
        safe=one(db,"SELECT plan_id,plan_revision FROM activation WHERE status='active' ORDER BY id DESC LIMIT 1")
        if safe:db.execute('INSERT INTO rollback(failed_controller,restored_plan,restored_revision,at,reason,actor,affected_from,affected_to) VALUES(?,?,?,?,?,?,?,?)',
                           (e['controller_id'],safe['plan_id'],safe['plan_revision'],e['readback_at'],'failed_readback_kept_safe_baseline',e.get('actor','unknown'),e['uploaded_at'],e['readback_at']))
    log(db,e,'field',e['controller_id'],state,reason)
    return state

def fault(db,e):
    if e.get('controller_id'):
        active=one(db,"SELECT * FROM activation WHERE controller_id=? AND status='active' ORDER BY id DESC LIMIT 1",(e['controller_id'],))
    else:
        active=one(db,"SELECT * FROM activation WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not active:raise ValueError('no_active_signal_plan')
    previous=one(db,"SELECT * FROM activation WHERE status='replaced' AND id<? ORDER BY id DESC LIMIT 1",(active['id'],))
    db.execute("UPDATE activation SET ended_at=?,status='failed',reason='controller_fault' WHERE id=?",(e['at'],active['id']))
    if e.get('window_id'):
        db.execute('INSERT OR REPLACE INTO disrupted_window VALUES(?,?,?,?)',
          (e['window_id'],active['started_at'],e['at'],active['controller_id']))
        affected=many(db,"SELECT * FROM vehicle_sample WHERE window_id=? AND observed_at>=? AND observed_at<=? AND state='counted'",(e['window_id'],active['started_at'],e['at']))
        for row in affected:
            db.execute("UPDATE vehicle_sample SET state='excluded',reason='field_fault_disrupted_flow_window' WHERE id=?",(row['id'],))
        if affected:
            from .flow import recalc
            for approach in {row['approach'] for row in affected}:recalc(db,e['window_id'],approach,e)
    if previous:
        db.execute('INSERT INTO activation(plan_id,plan_revision,controller_id,started_at,status,reason) VALUES(?,?,?,?,?,?)',
          (previous['plan_id'],previous['plan_revision'],previous['controller_id'],e['at'],'active','safe_rollback'))
        db.execute('INSERT INTO rollback(failed_controller,restored_plan,restored_revision,at,reason,actor,affected_from,affected_to) VALUES(?,?,?,?,?,?,?,?)',
          (active['controller_id'],previous['plan_id'],previous['plan_revision'],e['at'],'field_fault_reverted_to_previous_safe_plan',e.get('actor','unknown'),active['started_at'],e['at']))
        outcome='rolled_back'
    else:outcome='needs_manual_safe_mode'
    invalidate_controller(db,active['controller_id'],e['at'],'field_fault_rollback',e)
    log(db,e,'field',active['controller_id'],outcome,'controller_fault')
    return outcome
