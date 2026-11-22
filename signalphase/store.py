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
CREATE TABLE IF NOT EXISTS fiber_route(id TEXT PRIMARY KEY,controller_id TEXT NOT NULL,propagation_km_per_us REAL NOT NULL,splice_km REAL NOT NULL,north_branch_km REAL NOT NULL,south_branch_km REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fiber_echo(id TEXT PRIMARY KEY,route_id TEXT NOT NULL,measured_at TEXT NOT NULL,return_us REAL NOT NULL,amplitude REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fiber_triage(id TEXT PRIMARY KEY,echo_id TEXT NOT NULL,distance_km REAL NOT NULL,state TEXT NOT NULL);
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
