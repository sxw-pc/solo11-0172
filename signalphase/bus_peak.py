"""In-stop peak load proof from door event order and on-site evidence.

A door summary (board/alight totals over a window) only fixes the net movement;
the peak load inside the stop depends on the order of individual movements.
This module records per-door event sequences, sparse snapshots and continuous
video reviews, then derives the provable peak interval for each stop. Both
doors share one cabin and one pre-stop load, so all movements of a stop act on
a single running load. Cross-door order is only provable from a shared gateway
sequence or from mutually synchronized clocks; otherwise the assessment keeps
the honest interval instead of manufacturing a globally impossible safe
trajectory from per-door optimistic orders.
"""
import json

from .busload import chain_start
from .store import log, many, one

DOORS = ('left', 'right')
COVERAGE_RANK = {'none': 0, 'endpoints_only': 1, 'partial': 2, 'continuous': 3}


def segment(db, e):
    batch = one(db, 'SELECT * FROM bus_door_batch WHERE id=?', (e['batch_id'],))
    if not batch:
        raise ValueError('unknown_door_batch')
    if e['door'] not in DOORS or e['door_open_at'] >= e['door_close_at']:
        raise ValueError('invalid_door_segment')
    if one(db, 'SELECT 1 FROM bus_door_segment WHERE batch_id=? AND door=?', (e['batch_id'], e['door'])):
        raise ValueError('duplicate_door_segment')
    events = e['events']
    if not isinstance(events, list) or not events:
        raise ValueError('invalid_door_segment')
    board = alight = 0
    seqs = []
    for ev in events:
        if (not isinstance(ev.get('seq'), int) or isinstance(ev.get('seq'), bool)
                or ev.get('direction') not in ('board', 'alight')
                or not isinstance(ev.get('at'), str)
                or not (e['door_open_at'] <= ev['at'] <= e['door_close_at'])
                or ('gateway_seq' in ev and not isinstance(ev['gateway_seq'], int))):
            raise ValueError('invalid_door_event')
        if 'reboard_of' in ev and not ev.get('identity_ref'):
            raise ValueError('reboard_identity_required')
        seqs.append(ev['seq'])
        if ev['direction'] == 'board':
            board += 1
        else:
            alight += 1
    if len(set(seqs)) != len(seqs) or seqs != sorted(seqs):
        raise ValueError('invalid_door_event_order')
    db.execute('INSERT INTO bus_door_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['segment_id'], e['batch_id'], batch['trip_id'], batch['stop_id'], e['door'],
                e['gateway_id'], 1 if e.get('clock_synced') else 0, e['door_open_at'], e['door_close_at'],
                board, alight, json.dumps(events, sort_keys=True)))
    log(db, e, 'bus_peak', e['segment_id'], 'captured', 'door_event_sequence')
    return 'captured'


def complaint(db, e):
    if not one(db, 'SELECT 1 FROM bus_trip WHERE id=?', (e['trip_id'],)):
        raise ValueError('unknown_bus_trip')
    if e['window_from'] >= e['window_to']:
        raise ValueError('invalid_complaint_window')
    db.execute('INSERT INTO bus_complaint VALUES(?,?,?,?,?,?)',
               (e['complaint_id'], e['trip_id'], e['stop_id'], e['window_from'], e['window_to'], e['description']))
    log(db, e, 'bus_peak', e['complaint_id'], 'registered', 'passenger_complaint_window')
    return 'registered'


def evidence(db, e):
    if not one(db, 'SELECT 1 FROM bus_trip WHERE id=?', (e['trip_id'],)):
        raise ValueError('unknown_bus_trip')
    batch = one(db, 'SELECT * FROM bus_door_batch WHERE trip_id=? AND stop_id=? ORDER BY window_from',
                (e['trip_id'], e['stop_id']))
    if not batch:
        raise ValueError('unknown_stop_batch')
    if e['method'] == 'snapshot':
        snaps = e['snapshots']
        if (not isinstance(snaps, list) or not snaps
                or any(not isinstance(s.get('at'), str) or not isinstance(s.get('count'), int) or s['count'] < 0
                       for s in snaps)):
            raise ValueError('invalid_snapshot_evidence')
        interior = any(batch['window_from'] <= s['at'] <= batch['window_to'] for s in snaps)
        coverage = 'partial' if interior else 'endpoints_only'
        detail = {'snapshots': snaps}
    elif e['method'] == 'door_video':
        reviewers = e['reviewers']
        if not isinstance(reviewers, list) or len(set(reviewers)) != 2:
            raise ValueError('video_reviewers_required')
        marks = e['order_marks']
        if not isinstance(marks, list) or not marks or any(m not in ('board', 'alight') for m in marks):
            raise ValueError('invalid_video_marks')
        counts_ok = (marks.count('board') == batch['board_count']
                     and marks.count('alight') == batch['alight_count'])
        continuous = counts_ok and bool(e.get('timecode_closed')) and not e.get('occluded')
        coverage = 'continuous' if continuous else 'partial'
        detail = {'reviewers': sorted(reviewers), 'order_marks': marks,
                  'timecode_closed': bool(e.get('timecode_closed')), 'occluded': bool(e.get('occluded'))}
    else:
        raise ValueError('invalid_evidence_method')
    db.execute('INSERT INTO bus_stop_evidence VALUES(?,?,?,?,?,?)',
               (e['evidence_id'], e['trip_id'], e['stop_id'], e['method'], coverage,
                json.dumps(detail, sort_keys=True)))
    log(db, e, 'bus_peak', e['evidence_id'], coverage, e['method'] + '_evidence')
    return coverage


def _movements(batch, segments):
    moves = []
    for s in segments:
        for ev in json.loads(s['events_json']):
            moves.append({'segment': s['id'], 'direction': ev['direction'], 'seq': ev['seq'], 'at': ev['at'],
                          'door': s['door'], 'gateway_id': s['gateway_id'], 'gateway_seq': ev.get('gateway_seq'),
                          'synced': bool(s['clock_synced'])})
    for _ in range(batch['board_count'] - sum(s['board_count'] for s in segments)):
        moves.append({'segment': None, 'direction': 'board', 'seq': 0, 'at': None, 'door': None,
                      'gateway_id': None, 'gateway_seq': None, 'synced': False})
    for _ in range(batch['alight_count'] - sum(s['alight_count'] for s in segments)):
        moves.append({'segment': None, 'direction': 'alight', 'seq': 0, 'at': None, 'door': None,
                      'gateway_id': None, 'gateway_seq': None, 'synced': False})
    return moves


def _ordered_before(a, b):
    """True when movement a provably precedes b across different doors."""
    same_gateway = a['gateway_id'] is not None and a['gateway_id'] == b['gateway_id']
    if same_gateway and a['gateway_seq'] is not None and b['gateway_seq'] is not None:
        return a['gateway_seq'] < b['gateway_seq']
    if a['synced'] and b['synced']:
        return a['at'] < b['at']
    return False


def _reaches(preds, src, dst):
    stack, seen = [src], set()
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(preds[node])
    return False


def _preds(moves):
    n = len(moves)
    preds = [set() for _ in range(n)]
    by_segment = {}
    for i, m in enumerate(moves):
        if m['segment'] is not None:
            by_segment.setdefault(m['segment'], []).append(i)
    for idxs in by_segment.values():
        idxs.sort(key=lambda i: moves[i]['seq'])
        for earlier, later in zip(idxs, idxs[1:]):
            preds[later].add(earlier)
    for j in range(n):
        for i in range(n):
            if i == j or moves[i]['segment'] is None or moves[j]['segment'] is None:
                continue
            if moves[i]['segment'] == moves[j]['segment']:
                continue
            if _ordered_before(moves[i], moves[j]) and not _reaches(preds, j, i):
                preds[j].add(i)
    return preds


def _peak(start, moves, preds, boards_first):
    """Peak load of the greedy extreme trajectory over the movement poset."""
    n = len(moves)
    placed = [False] * n
    load = peak = start
    for _ in range(n):
        done = {i for i in range(n) if placed[i]}
        avail = [i for i in range(n) if not placed[i] and preds[i] <= done]
        prefer = 'board' if boards_first else 'alight'

        def key(i):
            m = moves[i]
            return (0 if m['direction'] == prefer else 1, m['at'] or '9999',
                    m['gateway_seq'] if m['gateway_seq'] is not None else 1 << 30, m['door'] or '', m['seq'])

        choice = None
        for i in sorted(avail, key=key):
            if not boards_first and moves[i]['direction'] == 'alight' and load <= 0:
                continue
            choice = i
            break
        if choice is None:
            choice = sorted(avail, key=key)[0]
        placed[choice] = True
        load += 1 if moves[choice]['direction'] == 'board' else -1
        peak = max(peak, load)
    return peak


def assess(db, e):
    batch = one(db, 'SELECT * FROM bus_door_batch WHERE id=?', (e['batch_id'],))
    if not batch:
        raise ValueError('unknown_door_batch')
    trip = one(db, 'SELECT * FROM bus_trip WHERE id=?', (batch['trip_id'],))
    start, chain = chain_start(db, trip, batch)
    capacity = trip['capacity']
    segments = many(db, 'SELECT * FROM bus_door_segment WHERE batch_id=? ORDER BY id', (batch['id'],))
    counted_boards = sum(s['board_count'] for s in segments)
    counted_alights = sum(s['alight_count'] for s in segments)
    mismatch = counted_boards > batch['board_count'] or counted_alights > batch['alight_count']
    moves = [] if mismatch else _movements(batch, segments)
    preds = _preds(moves)
    lo = _peak(start, moves, preds, boards_first=False)
    hi = _peak(start, moves, preds, boards_first=True)
    end_load = start + batch['board_count'] - batch['alight_count']
    coverage = 'none'
    reasons = []
    if mismatch:
        reasons.append('segment_batch_mismatch')
    for row in many(db, 'SELECT * FROM bus_stop_evidence WHERE trip_id=? AND stop_id=? ORDER BY id',
                    (trip['id'], batch['stop_id'])):
        if COVERAGE_RANK[row['coverage']] > COVERAGE_RANK[coverage]:
            coverage = row['coverage']
        detail = json.loads(row['detail_json'])
        if row['method'] == 'snapshot':
            for snap in detail['snapshots']:
                if batch['window_from'] <= snap['at'] <= batch['window_to']:
                    lo = max(lo, snap['count'])
                    hi = max(hi, snap['count'])
                elif snap['at'] < batch['window_from']:
                    if snap['count'] != start and 'snapshot_start_mismatch' not in reasons:
                        reasons.append('snapshot_start_mismatch')
                elif snap['count'] != end_load and 'snapshot_end_mismatch' not in reasons:
                    reasons.append('snapshot_end_mismatch')
        elif row['method'] == 'door_video' and row['coverage'] == 'continuous':
            load = peak = start
            for mark in detail['order_marks']:
                load += 1 if mark == 'board' else -1
                peak = max(peak, load)
            if lo <= peak <= hi:
                lo = hi = peak
            elif 'evidence_conflict' not in reasons:
                reasons.append('evidence_conflict')
    if chain == 'chain_gap':
        reasons.append('unverified_start_load')
    if reasons:
        verdict = 'pending_verification'
        reason = ','.join(reasons)
    elif lo > capacity:
        verdict, reason = 'overload_confirmed', 'provable_peak_exceeds_capacity'
    elif hi <= capacity:
        verdict, reason = 'overload_excluded', 'provable_peak_within_capacity'
    else:
        verdict, reason = 'pending_verification', 'peak_interval_straddles_capacity'
    db.execute('INSERT INTO bus_stop_peak VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['assessment_id'], batch['id'], trip['id'], batch['stop_id'], start, chain, lo, hi,
                capacity, verdict, coverage, reason))
    log(db, e, 'bus_peak', e['assessment_id'], verdict, reason)
    return verdict


def publish(db, e):
    complaint_row = one(db, 'SELECT * FROM bus_complaint WHERE id=?', (e['complaint_id'],))
    if not complaint_row:
        raise ValueError('unknown_complaint')
    peak = one(db, 'SELECT * FROM bus_stop_peak WHERE id=?', (e['assessment_id'],))
    if not peak:
        raise ValueError('unknown_peak_assessment')
    if (peak['trip_id'], peak['stop_id']) != (complaint_row['trip_id'], complaint_row['stop_id']):
        raise ValueError('assessment_complaint_mismatch')
    revision = one(db, 'SELECT COUNT(*) AS n FROM bus_complaint_ruling WHERE complaint_id=?',
                   (e['complaint_id'],))['n'] + 1
    responsibility = 'stop_boarding_management' if peak['verdict'] == 'overload_confirmed' else ''
    db.execute('INSERT INTO bus_complaint_ruling VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['ruling_id'], e['complaint_id'], revision, e['assessment_id'], peak['verdict'],
                peak['peak_lo'], peak['peak_hi'], peak['coverage'], responsibility, peak['reason']))
    log(db, e, 'bus_peak', e['ruling_id'], 'published', 'complaint_ruling_published')
    return 'published'
