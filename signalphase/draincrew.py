"""Crew-facing decisions: provisional visit, balance dispatch, executed work.

A level threshold only decides whether to send someone to look.  It is not a
blockage location and not a discharge estimate.  Attribution is delegated to
`drain_balance`, and every dispatch revision is appended: late flow evidence
creates a new revision and never rewrites work already executed.
"""
import json

from . import drain_balance
from .store import one, many, log


def visit(db, e):
    row = one(db, 'SELECT * FROM drain_level WHERE id=?', (e['level_id'],))
    if not row:
        raise ValueError('unknown_drain_level')
    if row['state'] != 'captured':
        state, reason = 'held', 'level_not_valid'
    elif row['depth_m'] >= 1.0:
        state, reason = 'inspect', 'high_water_level'
    else:
        state, reason = 'monitor', 'below_visit_threshold'
    db.execute('INSERT INTO drain_visit VALUES(?,?,?,?,?)',
               (e['visit_id'], row['id'], row['well_id'], state, reason))
    log(db, e, 'draincrew', e['visit_id'], state, reason)
    return state


def dispatch(db, e):
    """Compute a conservation explanation and append a dispatch revision."""
    well_id = e['well_id']
    revision = int(e['revision'])
    last = one(db, 'SELECT MAX(revision) AS r FROM drain_dispatch WHERE dispatch_id=?', (e['dispatch_id'],))
    if last and last['r'] is not None and revision <= last['r']:
        raise ValueError('dispatch_revision_not_monotonic')
    executed = bool(e.get('executed_clearing')) or one(
        db, "SELECT 1 FROM drain_work WHERE well_id=? AND work_kind='clearing' LIMIT 1", (well_id,)) is not None
    explanation = drain_balance.explain(db, well_id, e['level_from'], e['level_to'])
    decision = drain_balance.decision(explanation, executed_clearing=executed)
    drain_balance.store_explanation(db, e['dispatch_id'], revision, explanation, decision,
                                    e['recorded_at'])
    log(db, e, 'draincrew', e['dispatch_id'], decision['action'], decision['reason'])
    return decision['action']


def redispatch(db, e):
    """Late evidence re-runs the balance and appends a new dispatch revision.

    The previous revision, the visit and every executed work row stay exactly
    as recorded; only a new version is added for the crew to act on.
    """
    latest = one(db, 'SELECT * FROM drain_dispatch WHERE dispatch_id=? ORDER BY revision DESC LIMIT 1',
                 (e['dispatch_id'],))
    if not latest:
        raise ValueError('unknown_drain_dispatch')
    if latest['well_id'] != e['well_id']:
        raise ValueError('dispatch_well_mismatch')
    revision = latest['revision'] + 1
    executed = bool(e.get('executed_clearing')) or one(
        db, "SELECT 1 FROM drain_work WHERE well_id=? AND work_kind='clearing' LIMIT 1",
        (e['well_id'],)) is not None
    explanation = drain_balance.explain(db, e['well_id'], latest['level_from'], latest['level_to'])
    decision = drain_balance.decision(explanation, executed_clearing=executed)
    drain_balance.store_explanation(db, e['dispatch_id'], revision, explanation, decision,
                                    e['recorded_at'])
    log(db, e, 'draincrew', e['dispatch_id'], decision['action'], decision['reason'])
    return 'revised:%d:%s' % (revision, decision['action'])
