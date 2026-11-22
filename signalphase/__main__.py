"""Command line interface for the three signal decision ledgers."""
import argparse
import json
from pathlib import Path

from .engine import audit, ingest, report


def main():
    parser = argparse.ArgumentParser(prog='signalphase')
    parser.add_argument('command', choices=['flow-import', 'plan-import', 'field-import', 'corridor-import', 'report', 'audit', 'demo'])
    parser.add_argument('--db', default='signalphase.sqlite')
    parser.add_argument('--file')
    args = parser.parse_args()
    if args.command in {'flow-import', 'plan-import', 'field-import', 'corridor-import'}:
        if not args.file:
            parser.error('--file is required for imports')
        result = ingest(args.db, args.command, args.file)
    elif args.command == 'report':
        result = report(args.db)
    elif args.command == 'audit':
        result = audit(args.db)
    else:
        root = Path(__file__).resolve().parent.parent / 'fixtures'
        steps = [('flow-import', 'demo/01-flow.json'), ('plan-import', 'demo/02-plan.json'),
                 ('field-import', 'demo/03-field.json'), ('field-import', 'demo/04-fault.json'),
                 ('flow-import', 'demo/05-late-flow.json'),
                 ('corridor-import', 'corridor-ok.json'), ('corridor-import', 'corridor-storage.json')]
        result = [ingest(args.db, command, root / name) for command, name in steps]
        result = {'imports': result, 'audit': audit(args.db), 'report': report(args.db)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
