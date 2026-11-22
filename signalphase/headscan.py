"""Raw signal-head luminance frames, associated with field-verified activation."""
import json
from .store import one, log


def capture(db, e):
    rate = e['sample_rate_hz']
    values = e['values']
    exposure = e['exposure_us']
    if not isinstance(rate, int) or rate < 1 or not isinstance(exposure, int) or exposure < 1:
        raise ValueError('invalid_capture_rate')
    if not isinstance(values, list) or not values or any(not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 for v in values):
        raise ValueError('invalid_luminance_values')
    active = one(db, "SELECT * FROM activation WHERE controller_id=? AND started_at<=? AND (ended_at IS NULL OR ended_at>?) ORDER BY id DESC LIMIT 1",
                 (e['controller_id'], e['observed_at'], e['observed_at']))
    state = 'attributed' if active else 'unattributed'
    db.execute('INSERT INTO head_scan VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['scan_id'], e['controller_id'], e['head_id'], e['observed_at'], rate, exposure,
                json.dumps(values, separators=(',', ':')), active['plan_id'] if active else None,
                active['plan_revision'] if active else None, state))
    log(db, e, 'headscan', e['scan_id'], state, 'field_activation' if active else 'no_verified_activation')
    return state
