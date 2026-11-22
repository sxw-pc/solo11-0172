"""Raw evidence and independent demand, plan, field execution ledgers."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DDL="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,recorded_at TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS detector(id TEXT PRIMARY KEY,approach TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vehicle_sample(id TEXT PRIMARY KEY,detector_id TEXT NOT NULL,vehicle_hash TEXT NOT NULL,observed_at TEXT NOT NULL,window_id TEXT NOT NULL,approach TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS demand(window_id TEXT NOT NULL,approach TEXT NOT NULL,valid_count INTEGER NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,PRIMARY KEY(window_id,approach));
CREATE TABLE IF NOT EXISTS phase_plan(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,window_id TEXT NOT NULL,approach TEXT NOT NULL,ped_distance_m INTEGER NOT NULL,ped_green_sec INTEGER NOT NULL,vehicle_green_sec INTEGER NOT NULL,phases_json TEXT NOT NULL,approved_by TEXT NOT NULL,approved_from TEXT NOT NULL,approved_to TEXT NOT NULL,sha TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phase_plan_history(id TEXT NOT NULL,revision INTEGER NOT NULL,window_id TEXT NOT NULL,approach TEXT NOT NULL,ped_distance_m INTEGER NOT NULL,ped_green_sec INTEGER NOT NULL,vehicle_green_sec INTEGER NOT NULL,phases_json TEXT NOT NULL,approved_by TEXT NOT NULL,approved_from TEXT NOT NULL,approved_to TEXT NOT NULL,sha TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS approval(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,revision INTEGER NOT NULL,reviewer TEXT NOT NULL,at TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controller(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,plan_revision INTEGER NOT NULL,uploaded_at TEXT NOT NULL,readback_sha TEXT NOT NULL,readback_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activation(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,plan_revision INTEGER NOT NULL,controller_id TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rollback(id INTEGER PRIMARY KEY,failed_controller TEXT NOT NULL,restored_plan TEXT NOT NULL,restored_revision INTEGER NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,affected_from TEXT NOT NULL,affected_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS disrupted_window(window_id TEXT PRIMARY KEY,from_at TEXT NOT NULL,to_at TEXT NOT NULL,controller_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS head_scan(id TEXT PRIMARY KEY,controller_id TEXT NOT NULL,head_id TEXT NOT NULL,observed_at TEXT NOT NULL,sample_rate_hz INTEGER NOT NULL,exposure_us INTEGER NOT NULL,shutter_window_us INTEGER NOT NULL,values_json TEXT NOT NULL,plan_id TEXT,plan_revision INTEGER,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_group(id TEXT PRIMARY KEY,phase_id TEXT NOT NULL,controller_id TEXT NOT NULL,budget REAL NOT NULL,required_luminance REAL NOT NULL,power_to_luminance REAL NOT NULL,sync_skew_us INTEGER NOT NULL,period_us INTEGER NOT NULL,clock_ref TEXT NOT NULL,head_ids_json TEXT NOT NULL,observed_from TEXT NOT NULL,observed_to TEXT NOT NULL,plan_id TEXT,plan_revision INTEGER,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_verdict(id INTEGER PRIMARY KEY,group_id TEXT NOT NULL,revision INTEGER NOT NULL,head_id TEXT NOT NULL,scope TEXT NOT NULL,window_from TEXT,window_to TEXT,status TEXT NOT NULL,min_luminance REAL NOT NULL,max_luminance REAL NOT NULL,required_luminance REAL NOT NULL,min_dim_heads INTEGER NOT NULL,group_healthy INTEGER NOT NULL,work_order TEXT,control_action TEXT,evidence_json TEXT NOT NULL,superseded INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS current_reading(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,branch_id TEXT NOT NULL,head_id TEXT NOT NULL,window_from TEXT NOT NULL,window_to TEXT NOT NULL,mean_current REAL NOT NULL,calibrated_clock TEXT NOT NULL,clock_offset_us INTEGER NOT NULL,complete INTEGER NOT NULL,covers_historical_window INTEGER NOT NULL,present INTEGER NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clamp_reading(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,branch_id TEXT NOT NULL,head_id TEXT NOT NULL,installed_at TEXT NOT NULL,window_from TEXT NOT NULL,window_to TEXT NOT NULL,mean_current REAL NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_action(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,group_id TEXT NOT NULL,action TEXT NOT NULL,scope TEXT NOT NULL,head_id TEXT,window_from TEXT,window_to TEXT,at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,outcome TEXT NOT NULL,reason TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(DDL)
    cols={r[1] for r in db.execute('PRAGMA table_info(head_scan)')}
    if 'shutter_window_us' not in cols:
        db.execute('ALTER TABLE head_scan ADD COLUMN shutter_window_us INTEGER NOT NULL DEFAULT 0')
        db.execute('ALTER TABLE head_scan RENAME TO head_scan_legacy')
        db.execute('CREATE TABLE head_scan(id TEXT PRIMARY KEY,controller_id TEXT NOT NULL,head_id TEXT NOT NULL,observed_at TEXT NOT NULL,sample_rate_hz INTEGER NOT NULL,exposure_us INTEGER NOT NULL,shutter_window_us INTEGER NOT NULL,values_json TEXT NOT NULL,plan_id TEXT,plan_revision INTEGER,state TEXT NOT NULL)')
        db.execute('INSERT INTO head_scan SELECT id,controller_id,head_id,observed_at,sample_rate_hz,exposure_us,0,values_json,plan_id,plan_revision,state FROM head_scan_legacy')
        db.execute('DROP TABLE head_scan_legacy')
    return db
@contextmanager
def session(path):
    db=connect(path)
    try:
        with db:yield db
    finally:db.close()
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,e,system,key,outcome,reason):db.execute('INSERT INTO decision(event_id,subsystem,object_id,outcome,reason) VALUES(?,?,?,?,?)',(e['id'],system,key,outcome,reason))
