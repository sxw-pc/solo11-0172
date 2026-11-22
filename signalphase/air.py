"""Raw roadside pollutant channel profiles and observations."""
import json
from .store import one, log


def signature(db, e):
    channels = e['channels']
    if len(channels) != 3 or any(not isinstance(x, (int, float)) or x < 0 for x in channels):
        raise ValueError('invalid_air_signature')
    old = one(db, 'SELECT revision FROM air_signature WHERE id=?', (e['signature_id'],))
    if old and e['revision'] <= old['revision']:
        raise ValueError('stale_air_signature')
    db.execute('INSERT INTO air_signature VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_kind=excluded.source_kind,channels_json=excluded.channels_json,revision=excluded.revision',
               (e['signature_id'], e['source_kind'], json.dumps(channels), e['revision']))
    log(db, e, 'air', e['signature_id'], 'registered', 'reference_profile')
    return 'registered'


def sample(db, e):
    channels = e['channels']
    if len(channels) != 3 or any(not isinstance(x, (int, float)) or x < 0 for x in channels):
        raise ValueError('invalid_air_sample')
    db.execute('INSERT INTO air_sample VALUES(?,?,?,?,?)',
               (e['sample_id'], e['site_id'], e['observed_at'], json.dumps(channels), e['wind_direction']))
    log(db, e, 'air', e['sample_id'], 'captured', 'three_channel_observation')
    return 'captured'
