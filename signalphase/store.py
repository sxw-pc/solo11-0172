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
CREATE TABLE IF NOT EXISTS drain_well(id TEXT PRIMARY KEY,approach TEXT NOT NULL,storage_area_m2 REAL NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_level(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,observed_at TEXT NOT NULL,depth_m REAL NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_visit(id TEXT PRIMARY KEY,level_id TEXT NOT NULL,well_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_outlet(outlet_id TEXT PRIMARY KEY,well_id TEXT NOT NULL,name TEXT NOT NULL,capacity_m3h REAL NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_catchment(catchment_id TEXT PRIMARY KEY,well_id TEXT NOT NULL,name TEXT NOT NULL,surface TEXT NOT NULL,area_m2 REAL NOT NULL,runoff_coefficient REAL NOT NULL,radar_covered INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS drain_rainfall(rain_id TEXT PRIMARY KEY,catchment_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,intensity_mm_h REAL NOT NULL,effective_area_m2 REAL NOT NULL,loss_coefficient REAL NOT NULL,rate_m3h REAL NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_flow(flow_id TEXT PRIMARY KEY,well_id TEXT NOT NULL,direction TEXT NOT NULL,source TEXT NOT NULL,outlet_id TEXT,catchment_id TEXT,from_at TEXT NOT NULL,to_at TEXT NOT NULL,rate_m3h REAL NOT NULL,measures_total INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_pump(pump_event_id TEXT PRIMARY KEY,well_id TEXT NOT NULL,pump_id TEXT NOT NULL,at TEXT NOT NULL,powered INTEGER NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_work(work_id TEXT PRIMARY KEY,well_id TEXT NOT NULL,work_kind TEXT NOT NULL,at TEXT NOT NULL,crew TEXT NOT NULL,hours REAL NOT NULL,volume_m3 REAL NOT NULL,dispatch_id TEXT,dispatch_revision INTEGER,note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_dispatch(dispatch_id TEXT NOT NULL,revision INTEGER NOT NULL,well_id TEXT NOT NULL,level_from TEXT NOT NULL,level_to TEXT NOT NULL,action TEXT NOT NULL,equipment_json TEXT NOT NULL,scope_json TEXT NOT NULL,withdraw_json TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL,basis_json TEXT NOT NULL,PRIMARY KEY(dispatch_id,revision));
CREATE TABLE IF NOT EXISTS drain_balance(dispatch_id TEXT NOT NULL,revision INTEGER NOT NULL,candidate INTEGER NOT NULL,well_id TEXT NOT NULL,level_from TEXT NOT NULL,level_to TEXT NOT NULL,inflow_m3h REAL NOT NULL,outflow_m3h REAL NOT NULL,inflow_m3 REAL NOT NULL,outflow_m3 REAL NOT NULL,storage_m3 REAL NOT NULL,residual_m3 REAL NOT NULL,label TEXT NOT NULL,measured_outflow INTEGER NOT NULL,basis_json TEXT NOT NULL,PRIMARY KEY(dispatch_id,revision,candidate));
CREATE TABLE IF NOT EXISTS drain_gap(dispatch_id TEXT NOT NULL,revision INTEGER NOT NULL,candidate INTEGER NOT NULL,gap_no INTEGER NOT NULL,well_id TEXT NOT NULL,kind TEXT NOT NULL,m3h REAL NOT NULL,m3 REAL NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(dispatch_id,revision,candidate,gap_no));
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,outcome TEXT NOT NULL,reason TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(DDL)
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
