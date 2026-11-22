"""Transactional evidence import, reporting and replay audit."""
import hashlib
import json
import tempfile
from pathlib import Path

from . import field, flow, plans
from .store import many, one, session

HANDLERS = {
    'flow-import': {'detector': flow.detector, 'sample': flow.sample, 'revoke': flow.revoke},
    'plan-import': {'plan': plans.submit},
    'field-import': {'upload': field.upload, 'fault': field.fault},
}
TABLES = ('detector', 'vehicle_sample', 'demand', 'phase_plan', 'phase_plan_history', 'approval',
          'controller', 'activation', 'rollback', 'disrupted_window', 'decision')


def _ingest(db, command, raw):
    if command not in HANDLERS:
        raise ValueError('unknown_command')
    sha = hashlib.sha256(raw).hexdigest()
    if one(db, 'SELECT id FROM source_file WHERE sha=?', (sha,)):
        return {'source_sha': sha, 'duplicate': True, 'decisions': []}
    payload = json.loads(raw)
    events = payload['events']
    if not isinstance(events, list) or not events:
        raise ValueError('events_required')
    db.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)', (command, sha, raw))
    source_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    result = []
    for e in events:
        kind = e['kind']
        if kind not in HANDLERS[command]:
            raise ValueError('kind_not_allowed_for_command')
        event_sha = hashlib.sha256(json.dumps(e, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if one(db, 'SELECT id FROM event WHERE id=?', (e['id'],)):
            raise ValueError('event_id_reused')
        db.execute('INSERT INTO event VALUES(?,?,?,?,?,?)',
                   (e['id'], kind, event_sha, json.dumps(e, ensure_ascii=False, sort_keys=True).encode(),
                    e.get('recorded_at', e.get('observed_at', e.get('at', '2026-01-01T00:00:00Z'))), source_id))
        outcome = HANDLERS[command][kind](db, e)
        result.append({'event_id': e['id'], 'outcome': outcome})
    return {'source_sha': sha, 'duplicate': False, 'decisions': result}


def ingest(db_path, command, file_path):
    raw = Path(file_path).read_bytes()
    with session(db_path) as db:
        return _ingest(db, command, raw)


def report(db_path):
    with session(db_path) as db:
        return {table: [dict(row) for row in many(db, f'SELECT * FROM {table} ORDER BY rowid')]
                for table in TABLES}


def audit(db_path):
    with session(db_path) as db:
        sources = [dict(row) for row in many(db, 'SELECT * FROM source_file ORDER BY id')]
        events = [dict(row) for row in many(db, 'SELECT * FROM event ORDER BY rowid')]
        source_integrity = all(hashlib.sha256(row['raw']).hexdigest() == row['sha'] for row in sources)
        event_integrity = all(hashlib.sha256(json.dumps(json.loads(row['raw']), sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest() == row['sha'] for row in events)
        original = {table: [tuple(row) for row in many(db, f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in TABLES}
    with tempfile.TemporaryDirectory() as temp:
        replay_db = Path(temp) / 'replay.sqlite'
        try:
            with session(replay_db) as db:
                replay = [_ingest(db, row['command'], row['raw']) for row in sources]
                rebuilt = {table: [tuple(row) for row in many(db, f'SELECT * FROM {table} ORDER BY rowid')]
                           for table in TABLES}
            replay_ok = original == rebuilt and all(not x['duplicate'] for x in replay)
        except Exception:
            replay_ok = False
    return {'source_integrity': source_integrity, 'event_integrity': event_integrity,
            'replay_ok': replay_ok, 'sources': len(sources), 'events': len(events)}
