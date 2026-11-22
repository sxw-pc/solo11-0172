"""Command line interface for the three signal decision ledgers."""
import argparse
import json
import sqlite3
from pathlib import Path

from . import drain_balance
from .engine import audit, ingest, report


def explain(db_path, well_id, level_from, level_to):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        result = drain_balance.explain(db, well_id, level_from, level_to)
        result['decision'] = drain_balance.decision(result)
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(prog='signalphase')
    parser.add_argument('command', choices=['flow-import', 'plan-import', 'field-import', 'drain-import',
                                            'report', 'audit', 'explain', 'demo'])
    parser.add_argument('--db', default='signalphase.sqlite')
    parser.add_argument('--file')
    parser.add_argument('--well')
    parser.add_argument('--from-level')
    parser.add_argument('--to-level')
    args = parser.parse_args()
    if args.command in {'flow-import', 'plan-import', 'field-import', 'drain-import'}:
        if not args.file:
            parser.error('--file is required for imports')
        result = ingest(args.db, args.command, args.file)
    elif args.command == 'report':
        result = report(args.db)
    elif args.command == 'audit':
        result = audit(args.db)
    elif args.command == 'explain':
        if not (args.well and args.from_level and args.to_level):
            parser.error('--well, --from-level and --to-level are required for explain')
        result = explain(args.db, args.well, args.from_level, args.to_level)
    else:
        root = Path(__file__).resolve().parent.parent / 'fixtures' / 'demo'
        steps = [('flow-import', '01-flow.json'), ('plan-import', '02-plan.json'),
                 ('field-import', '03-field.json'), ('field-import', '04-fault.json'),
                 ('flow-import', '05-late-flow.json')]
        result = [ingest(args.db, command, root / name) for command, name in steps]
        result = {'imports': result, 'audit': audit(args.db), 'report': report(args.db)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
