"""Minimal offline pytest entry point.

The execution environment has no network, no pip and no site-packages
pytest.  This shim maps the pytest invocation used in the project to the
standard-library ``unittest`` runner, supporting the flags the suite needs
(``-q`` and ``-p no:cacheprovider``).  When a real pytest is installed it
takes precedence on ``sys.path`` and this file is never imported.
"""
import sys
import unittest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Flags accepted for pytest command-line compatibility.
    if '-p' in argv:
        idx = argv.index('-p')
        plugin = argv[idx + 1] if idx + 1 < len(argv) else ''
        if plugin != 'no:cacheprovider':
            raise SystemExit(f'pytest shim: unsupported plugin "{plugin}"')
        del argv[idx:idx + 2]
    quiet = False
    paths = []
    for arg in argv:
        if arg in ('-q', '--quiet'):
            quiet = True
        elif arg in ('-v', '--verbose'):
            quiet = False
        elif arg.startswith('-'):
            raise SystemExit(f'pytest shim: unsupported flag "{arg}"')
        else:
            paths.append(arg)
    start_dir = paths[0] if paths else 'tests'
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir, pattern='test_*.py')
    verbosity = 1 if quiet else 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
