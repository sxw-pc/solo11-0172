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
CREATE TABLE IF NOT EXISTS remote_signal_window(id TEXT PRIMARY KEY,intersection_id TEXT NOT NULL,green_from TEXT NOT NULL,green_to TEXT NOT NULL,verified INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_request(id TEXT PRIMARY KEY,controller_id TEXT NOT NULL,requested_at TEXT NOT NULL,remote_id TEXT NOT NULL,travel_min_sec INTEGER NOT NULL,travel_max_sec INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_check(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,active_plan TEXT,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_link(id TEXT PRIMARY KEY,from_controller TEXT NOT NULL,to_intersection TEXT NOT NULL,capacity INTEGER NOT NULL,declared_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS link_event(id TEXT PRIMARY KEY,link_id TEXT NOT NULL,vehicle TEXT NOT NULL,direction TEXT NOT NULL,at TEXT NOT NULL,receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_meter(id TEXT PRIMARY KEY,controller_id TEXT NOT NULL,approach TEXT NOT NULL,meter_from TEXT NOT NULL,meter_to TEXT NOT NULL,receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_eval(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,link_id TEXT NOT NULL,release_at TEXT NOT NULL,a_vehicle_from TEXT NOT NULL,a_vehicle_to TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,feasible_from TEXT,feasible_to TEXT,evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_commitment(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,eval_id TEXT NOT NULL,route TEXT NOT NULL,state TEXT NOT NULL,window_from TEXT,window_to TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,closed_at TEXT);
CREATE TABLE IF NOT EXISTS corridor_notice(id TEXT PRIMARY KEY,commitment_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,detail TEXT NOT NULL);
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
