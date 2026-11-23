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
CREATE TABLE IF NOT EXISTS drain_catchment(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,name TEXT NOT NULL,effective_area_m2 REAL NOT NULL,loss_coefficient REAL NOT NULL,radar_covered INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_rainfall(id TEXT PRIMARY KEY,catchment_id TEXT NOT NULL,well_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,intensity_mh REAL NOT NULL,inflow_m3h REAL NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_flow(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,point TEXT NOT NULL,direction TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,rate_m3h REAL NOT NULL,provenance TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_pump(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,outlet_id TEXT NOT NULL,energized_at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_pump_receipt(id TEXT PRIMARY KEY,pump_id TEXT NOT NULL,well_id TEXT NOT NULL,outlet_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,volume_m3 REAL NOT NULL,rate_m3h REAL NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_scenario(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,weather TEXT NOT NULL,outlet_state TEXT NOT NULL,terms_json TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_balance(id TEXT PRIMARY KEY,well_id TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL,level_count INTEGER NOT NULL,storage_change_m3 REAL NOT NULL,net_rate_m3h REAL NOT NULL,known_inflow_m3h REAL NOT NULL,known_outflow_m3h REAL NOT NULL,inflow_gap_m3h REAL NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_balance_solution(balance_id TEXT NOT NULL,scenario_id TEXT NOT NULL,weather TEXT NOT NULL,outlet_state TEXT NOT NULL,inflow_m3h REAL NOT NULL,outflow_m3h REAL NOT NULL,feasible INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,terms_json TEXT NOT NULL,PRIMARY KEY(balance_id,scenario_id));
CREATE TABLE IF NOT EXISTS drain_balance_gap(balance_id TEXT NOT NULL,kind TEXT NOT NULL,source TEXT NOT NULL,missing_m3h REAL NOT NULL,note TEXT NOT NULL,PRIMARY KEY(balance_id,kind,source));
CREATE TABLE IF NOT EXISTS drain_dispatch(id TEXT PRIMARY KEY,balance_id TEXT NOT NULL,well_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,gap TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drain_work_receipt(id TEXT PRIMARY KEY,dispatch_id TEXT NOT NULL,well_id TEXT NOT NULL,action TEXT NOT NULL,executed_at TEXT NOT NULL,work_hours REAL NOT NULL);
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
