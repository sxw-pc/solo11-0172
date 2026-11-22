"""Walking route diesel exposure product.

Three roadside channels cannot uniquely split a mixture when a reference
profile lies in the span of the others (b == (d+u)/2 makes the observation
matrix rank-deficient). Every decomposition keeps the *joint* feasible range
of source shares and route person-time exposure instead of collapsing a
zero-residual family onto one coefficient point. A route is only recommended
when it dominates on diesel concentration-time across every feasible
decomposition; an uncovered segment or overlapping feasible ranges leave the
east/west comparison explicitly open.
"""
import itertools
import json
from .store import log, many, one

INF = float('inf')
EPS = 1e-9


def _vec(n, fill=0.0):
    return [fill] * n


def _solve_square(A, b):
    """Gaussian elimination with partial pivoting; None when singular/inconsistent."""
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < EPS:
            return None
        M[col], M[pivot] = M[pivot], M[col]
        pv = M[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col] / pv
            if factor:
                M[r] = [M[r][k] - factor * M[col][k] for k in range(n + 1)]
    x = [M[i][n] / M[i][i] for i in range(n)]
    return x


def _residual(A, x, y):
    rows, cols = len(A), len(x)
    out = []
    for i in range(rows):
        out.append(sum(A[i][j] * x[j] for j in range(cols)) - y[i])
    return out


def nullspace(A):
    """Basis vectors of {x: A x = 0}; empty list when the columns are independent."""
    rows, cols = len(A), len(A[0])
    M = [list(r) for r in A]
    pivots = []
    r = 0
    for c in range(cols):
        pivot = next((i for i in range(r, rows) if abs(M[i][c]) > EPS), None)
        if pivot is None:
            continue
        M[r], M[pivot] = M[pivot], M[r]
        pv = M[r][c]
        M[r] = [v / pv for v in M[r]]
        for i in range(rows):
            if i != r and abs(M[i][c]) > EPS:
                factor = M[i][c]
                M[i] = [M[i][k] - factor * M[r][k] for k in range(cols)]
        pivots.append(c)
        r += 1
        if r == rows:
            break
    basis = []
    free = [c for c in range(cols) if c not in pivots]
    for fc in free:
        v = _vec(cols)
        v[fc] = 1.0
        for i, pc in enumerate(pivots):
            v[pc] = -M[i][fc]
        basis.append(v)
    return basis


def linearly_dependent(vectors):
    """True when the given column vectors are linearly dependent."""
    A = [list(row) for row in zip(*vectors)]
    return any(any(abs(v) > EPS for v in z) for z in nullspace(A))


def mixture_bounds(A, y, target):
    """Feasible range of coefficient ``target`` over {x >= 0: A x = y}.

    Enumerates basic feasible solutions (vertex enumeration). For the small
    synthetic matrices here this is exact and needs no external LP solver.
    Returns ``(lower, upper, vertices, basis)``; an unbounded side is ``INF``.
    """
    rows, cols = len(A), len(A[0])
    vertices = []
    seen = set()

    def _add(x):
        if any(v < -EPS for v in x):
            return
        x = [max(0.0, v) for v in x]
        if any(abs(r) > 1e-7 for r in _residual(A, x, y)):
            return
        key = tuple(round(v, 8) for v in x)
        if key not in seen:
            seen.add(key)
            vertices.append(x)

    for size in range(1, min(rows, cols) + 1):
        for js in itertools.combinations(range(cols), size):
            B = [[A[i][j] for j in js] for i in range(rows)]
            sub_rows = list(range(rows))
            # choose `size` independent rows to square the basis
            for rs in itertools.combinations(sub_rows, size):
                Bs = [B[i] for i in rs]
                bs = [y[i] for i in rs]
                sol = _solve_square(Bs, bs)
                if sol is None:
                    continue
                if any(v < -EPS for v in sol):
                    continue
                x = _vec(cols)
                for k, j in enumerate(js):
                    x[j] = sol[k]
                _add(x)

    basis = nullspace(A)
    lower = min((v[target] for v in vertices), default=0.0)
    upper = max((v[target] for v in vertices), default=INF)
    for z in basis:
        if abs(z[target]) > EPS:
            # a nullspace ray that preserves non-negativity makes this side open
            ray_pos = all(z[j] >= -EPS for j in range(cols))
            ray_neg = all(z[j] <= EPS for j in range(cols))
            if z[target] > EPS and ray_pos:
                upper = INF
            if z[target] < -EPS and ray_neg:
                lower = 0.0
    lower = max(0.0, lower)
    return lower, upper, vertices, basis


def nnls_point(A, y):
    """Non-negative least squares point estimate via subset enumeration.

    Used only as a labelled fallback when no exact zero-residual decomposition
    exists; the feasible-range product never treats it as the sole answer.
    """
    rows, cols = len(A), len(A[0])
    best = None
    for size in range(1, min(rows, cols) + 1):
        for js in itertools.combinations(range(cols), size):
            for rs in itertools.combinations(range(rows), size):
                Bs = [[A[i][j] for j in js] for i in rs]
                sol = _solve_square(Bs, [y[i] for i in rs])
                if sol is None or any(v < -EPS for v in sol):
                    continue
                x = _vec(cols)
                for k, j in enumerate(js):
                    x[j] = sol[k]
                sse = sum(r * r for r in _residual(A, x, y))
                if best is None or sse < best[0] - EPS:
                    best = (sse, x)
    return list(best[1]) if best else _vec(cols)


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #

def calibration(db, e):
    bias = e['bias']
    if len(bias) not in (3, 4) or any(not isinstance(x, (int, float)) for x in bias):
        raise ValueError('invalid_calibration_bias')
    old = one(db, 'SELECT revision FROM walk_calibration WHERE id=?', (e['calibration_id'],))
    if old and e['revision'] <= old['revision']:
        raise ValueError('stale_calibration')
    db.execute('INSERT INTO walk_calibration VALUES(?,?,?,?,?,?)',
               (e['calibration_id'], e['batch_id'], json.dumps(bias),
                e['valid_from'], e['valid_to'], e['revision']))
    log(db, e, 'walk', e['calibration_id'], 'registered', 'instrument_calibration')
    return 'registered'


def route(db, e):
    old = one(db, 'SELECT revision FROM walk_route WHERE id=?', (e['route_id'],))
    if old and e['revision'] <= old['revision']:
        raise ValueError('stale_route')
    db.execute('INSERT INTO walk_route VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision=excluded.revision',
               (e['route_id'], e['name'], e['revision']))
    log(db, e, 'walk', e['route_id'], 'registered', 'walking_route')
    return 'registered'


def segment(db, e):
    if e['length_m'] <= 0 or e['walk_seconds'] <= 0:
        raise ValueError('invalid_segment')
    if not one(db, 'SELECT 1 FROM walk_route WHERE id=?', (e['route_id'],)):
        raise ValueError('unknown_route')
    db.execute('INSERT INTO walk_segment VALUES(?,?,?,?,?)',
               (e['segment_id'], e['route_id'], e['seq_no'], e['length_m'], e['walk_seconds']))
    log(db, e, 'walk', e['segment_id'], 'captured', 'route_segment')
    return 'captured'


def reading(db, e):
    channels = e['channels']
    if len(channels) != 3 or any(not isinstance(x, (int, float)) or x < 0 for x in channels):
        raise ValueError('invalid_walk_reading')
    if not one(db, 'SELECT 1 FROM walk_segment WHERE id=?', (e['segment_id'],)):
        raise ValueError('unknown_segment')
    if not one(db, 'SELECT 1 FROM walk_calibration WHERE batch_id=?', (e['calibration_batch'],)):
        raise ValueError('unknown_calibration_batch')
    db.execute('INSERT INTO walk_reading VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['reading_id'], e['segment_id'], e['observed_at'], json.dumps(channels),
                e['wind_direction'], e['calibration_batch'], e['fingerprint_version'],
                e.get('station_coverage'), 'captured', 'three_channel_observation'))
    log(db, e, 'walk', e['reading_id'], 'captured', 'three_channel_observation')
    return 'captured'


def channel4(db, e):
    if not isinstance(e['value'], (int, float)):
        raise ValueError('invalid_channel4')
    if not one(db, 'SELECT 1 FROM walk_segment WHERE id=?', (e['segment_id'],)):
        raise ValueError('unknown_segment')
    db.execute('INSERT INTO walk_channel4 VALUES(?,?,?,?,?,?,?,?,?)',
               (e['reading_id'], e['segment_id'], e['observed_at'], e['value'],
                e['calibration_batch'], e['fingerprint_version'], e['wind_direction'],
                'captured', 'mobile_fourth_channel'))
    log(db, e, 'walk', e['reading_id'], 'captured', 'mobile_fourth_channel')
    return 'captured'


def source_channel4(db, e):
    if not isinstance(e['value'], (int, float)):
        raise ValueError('invalid_source_channel4')
    if not one(db, 'SELECT 1 FROM air_signature WHERE id=?', (e['signature_id'],)):
        raise ValueError('unknown_signature')
    db.execute('INSERT INTO walk_source_channel4 VALUES(?,?,?) ON CONFLICT(signature_id,fingerprint_version) DO UPDATE SET value=excluded.value',
               (e['signature_id'], e['fingerprint_version'], e['value']))
    log(db, e, 'walk', e['signature_id'], 'registered', 'fourth_channel_fingerprint')
    return 'registered'


def filter_analysis(db, e):
    if not one(db, 'SELECT 1 FROM walk_segment WHERE id=?', (e['segment_id'],)):
        raise ValueError('unknown_segment')
    db.execute('INSERT INTO walk_filter VALUES(?,?,?,?,?,?)',
               (e['filter_id'], e['segment_id'], e['observed_at'], 1 if e['diesel_confirmed'] else 0,
                e['fingerprint_version'], json.dumps(e.get('detail', {}))))
    log(db, e, 'walk', e['filter_id'],
        'diesel_confirmed' if e['diesel_confirmed'] else 'diesel_not_confirmed', 'filter_chemistry')
    return 'diesel_confirmed' if e['diesel_confirmed'] else 'diesel_not_confirmed'


# --------------------------------------------------------------------------- #
# Assessment: joint feasible exposure ranges and the route decision
# --------------------------------------------------------------------------- #

def _signature_matrix(db, fingerprint_version):
    rows = many(db, 'SELECT * FROM air_signature ORDER BY id')
    ids = [r['id'] for r in rows]
    kinds = [r['source_kind'] for r in rows]
    A3 = [json.loads(r['channels_json']) for r in rows]
    extra = {r['signature_id']: r['value']
             for r in many(db, 'SELECT * FROM walk_source_channel4 WHERE fingerprint_version=?',
                           (fingerprint_version,))}
    use4 = all(sid in extra for sid in ids)
    matrix3 = [list(col) for col in zip(*A3)]
    if use4:
        A4 = [A3[i] + [extra[ids[i]]] for i in range(len(ids))]
        matrix4 = [list(col) for col in zip(*A4)]
    else:
        matrix4 = matrix3
    return ids, kinds, matrix3, matrix4, use4


def _segment_observation(db, segment_id, wind, calibration_batch, fingerprint_version):
    """Collect the usable evidence for one segment under one wind/calibration regime."""
    readings = many(db, 'SELECT * FROM walk_reading WHERE segment_id=? ORDER BY observed_at', (segment_id,))
    excluded = []
    kept = []
    for r in readings:
        reason = None
        if r['wind_direction'] != wind:
            reason = 'wind_regime_mismatch'
        elif r['calibration_batch'] != calibration_batch:
            reason = 'calibration_batch_mismatch'
        elif r['fingerprint_version'] != fingerprint_version:
            reason = 'fingerprint_version_mismatch'
        if reason:
            if r['state'] == 'captured':
                db.execute('UPDATE walk_reading SET state=?,state_reason=? WHERE id=?',
                           ('excluded', reason, r['id']))
            excluded.append((r['id'], reason))
        else:
            kept.append(r)
    c4_rows = many(db, 'SELECT * FROM walk_channel4 WHERE segment_id=? ORDER BY observed_at', (segment_id,))
    c4 = []
    for r in c4_rows:
        reason = None
        if r['wind_direction'] != wind:
            reason = 'wind_regime_mismatch'
        elif r['calibration_batch'] != calibration_batch:
            reason = 'calibration_batch_mismatch'
        elif r['fingerprint_version'] != fingerprint_version:
            reason = 'fingerprint_version_mismatch'
        if reason:
            if r['state'] == 'captured':
                db.execute('UPDATE walk_channel4 SET state=?,state_reason=? WHERE id=?',
                           ('excluded', reason, r['id']))
            excluded.append((r['id'], reason))
        else:
            c4.append(r)
    if not kept:
        return None, c4, excluded
    cal = one(db, 'SELECT * FROM walk_calibration WHERE batch_id=?', (calibration_batch,))
    bias = json.loads(cal['bias_json'])
    channels = [sum(json.loads(r['channels_json'])[k] for r in kept) / len(kept)
                for k in range(3)]
    y = [channels[k] - bias[k] for k in range(3)]
    if any(v < -EPS for v in y):
        raise ValueError('calibration_bias_over_correction')
    y = [max(0.0, v) for v in y]
    if c4:
        y4 = sum(r['value'] for r in c4) / len(c4) - bias[3] if len(bias) > 3 else sum(r['value'] for r in c4) / len(c4)
        y = y + [max(0.0, y4)]
    return y, c4, excluded


def assess(db, e):
    routes = e['routes']
    if not routes:
        raise ValueError('routes_required')
    wind = e['wind_direction']
    batch = e['calibration_batch']
    version = e['fingerprint_version']
    if not one(db, 'SELECT 1 FROM walk_calibration WHERE batch_id=?', (batch,)):
        raise ValueError('unknown_calibration_batch')
    _, kinds, matrix3, matrix4, use4 = _signature_matrix(db, version)
    if 'diesel_exhaust' not in kinds:
        raise ValueError('no_diesel_signature')
    target = kinds.index('diesel_exhaust')
    seg_rows = many(db, 'SELECT * FROM walk_segment WHERE route_id IN (%s) ORDER BY route_id,seq_no'
                    % ','.join('?' for _ in routes), routes)

    per_route = {rid: {'lower': 0.0, 'upper': 0.0, 'uncovered': []} for rid in routes}
    all_excluded = []
    for seg in seg_rows:
        y, c4, excluded = _segment_observation(db, seg['id'], wind, batch, version)
        all_excluded.extend(excluded)
        minutes = seg['walk_seconds'] / 60.0
        filt = one(db, 'SELECT * FROM walk_filter WHERE segment_id=? ORDER BY observed_at DESC', (seg['id'],))
        filter_finding = None
        if filt:
            filter_finding = 'diesel_confirmed' if filt['diesel_confirmed'] else 'diesel_not_confirmed'
        if y is None:
            # no 3-channel observation in this wind/calibration/version regime:
            # a school-gate filter elsewhere cannot invent concentrations here
            per_route[seg['route_id']]['uncovered'].append(seg['id'])
            per_route[seg['route_id']]['upper'] = INF
            db.execute('INSERT INTO walk_exposure VALUES(?,?,?,?,?,?,?,?,?)',
                       (e['assessment_id'] + ':' + seg['id'], e['assessment_id'], seg['route_id'],
                        seg['id'], 0, None, None, 'uncovered', filter_finding))
            continue
        if use4 and not c4:
            # mobile fourth channel is shielded on this segment: the three
            # roadside channels still constrain the feasible family
            lo, hi, verts, _ = mixture_bounds(matrix3, y, target)
            basis_state = 'three_channel_bounded'
            point_matrix = matrix3
        else:
            lo, hi, verts, _ = mixture_bounds(matrix4, y, target)
            basis_state = 'fourth_channel_resolved' if use4 else 'three_channel_bounded'
            point_matrix = matrix4
        if not verts:
            point = nnls_point(point_matrix, y)
            lo = hi = point[target]
            basis_state = 'nnls_point_estimate'
        db.execute('INSERT INTO walk_exposure VALUES(?,?,?,?,?,?,?,?,?)',
                   (e['assessment_id'] + ':' + seg['id'], e['assessment_id'], seg['route_id'],
                    seg['id'], 1, lo * minutes, None if hi == INF else hi * minutes,
                    basis_state, filter_finding))
        per_route[seg['route_id']]['lower'] += lo * minutes
        per_route[seg['route_id']]['upper'] = (INF if basis_state != 'nnls_point_estimate'
                                               and (per_route[seg['route_id']]['upper'] == INF or hi == INF)
                                               else per_route[seg['route_id']]['upper'] + hi * minutes)

    uncovered = {rid: v['uncovered'] for rid, v in per_route.items() if v['uncovered']}
    comparable = not uncovered
    winner = None
    reason_parts = ['excluded:' + json.dumps(all_excluded, sort_keys=True)] if all_excluded else []
    if not comparable:
        reason = 'uncovered_segments:' + json.dumps(uncovered, sort_keys=True)
    else:
        # dominance only when one route's worst feasible exposure beats the
        # other's best across the whole zero-residual family
        ids_order = routes
        bounds = {rid: (per_route[rid]['lower'], per_route[rid]['upper']) for rid in ids_order}
        if bounds[ids_order[0]][1] < bounds[ids_order[1]][0] - EPS:
            winner = ids_order[0]
            reason = 'dominates_all_feasible_decompositions'
        elif bounds[ids_order[1]][1] < bounds[ids_order[0]][0] - EPS:
            winner = ids_order[1]
            reason = 'dominates_all_feasible_decompositions'
        else:
            reason = 'feasible_ranges_overlap_no_dominant_route'
    status = 'comparable' if comparable else 'incomparable'
    full_reason = '|'.join([reason] + reason_parts)

    db.execute('INSERT INTO walk_assessment VALUES(?,?,?,?,?,?,?)',
               (e['assessment_id'], json.dumps(routes), wind, batch, version, status, full_reason))
    route_bounds = {rid: {'lower': per_route[rid]['lower'],
                          'upper': None if per_route[rid]['upper'] == INF else per_route[rid]['upper']}
                    for rid in routes}
    db.execute('INSERT INTO walk_recommendation VALUES(?,?,?,?,?,?,?)',
               (e['assessment_id'], e['assessment_id'], 1 if comparable else 0, winner, full_reason,
                json.dumps(route_bounds, sort_keys=True), json.dumps(uncovered, sort_keys=True)))
    log(db, e, 'walk', e['assessment_id'], status,
        ('recommend:' + winner) if winner else full_reason)
    return status + (':' + winner if winner else '')
