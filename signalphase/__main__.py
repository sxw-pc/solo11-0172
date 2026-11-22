"""Command line interface for the signal and surface decision ledgers."""
import argparse
import json
from pathlib import Path

from .engine import audit, ingest, report

IMPORTS = ('flow-import', 'plan-import', 'field-import', 'surface-import', 'ice-import')


def main():
    parser = argparse.ArgumentParser(prog='signalphase')
    parser.add_argument('command', choices=[*IMPORTS, 'report', 'audit', 'demo'])
    parser.add_argument('--db', default='signalphase.sqlite')
    parser.add_argument('--file')
    args = parser.parse_args()
    if args.command in IMPORTS:
        if not args.file:
            parser.error('--file is required for imports')
        result = ingest(args.db, args.command, args.file)
    elif args.command == 'report':
        result = report(args.db)
    elif args.command == 'audit':
        result = audit(args.db)
    else:
        root = Path(__file__).resolve().parent.parent / 'fixtures'
        steps = [('flow-import', root / 'demo' / '01-flow.json'), ('plan-import', root / 'demo' / '02-plan.json'),
                 ('field-import', root / 'demo' / '03-field.json'), ('field-import', root / 'demo' / '04-fault.json'),
                 ('flow-import', root / 'demo' / '05-late-flow.json'), ('ice-import', root / 'ice-time.json')]
        result = [ingest(args.db, command, path) for command, path in steps]
        result = {'imports': result, 'audit': audit(args.db), 'report': report(args.db)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
