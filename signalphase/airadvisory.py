"""Provisional nearest single-source roadside advisory."""
import json
import math
from .store import one, many, log


def check(db, e):
    sample = one(db, 'SELECT * FROM air_sample WHERE id=?', (e['sample_id'],))
    if not sample:
        raise ValueError('unknown_air_sample')
    references = many(db, 'SELECT * FROM air_signature')
    if not references:
        raise ValueError('no_air_signature')
    values = json.loads(sample['channels_json'])
    best = min(references, key=lambda row: sum((x-y)**2 for x,y in zip(values, json.loads(row['channels_json']))))
    distance = math.sqrt(sum((x-y)**2 for x,y in zip(values, json.loads(best['channels_json']))))
    db.execute('INSERT INTO air_advisory VALUES(?,?,?,?,?)',
               (e['check_id'], e['sample_id'], best['source_kind'], distance, 'provisional'))
    log(db, e, 'airadvisory', e['check_id'], 'provisional', best['source_kind'])
    return 'provisional'
