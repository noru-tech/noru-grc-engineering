#!/usr/bin/env python3
"""Offline literal datastore-binding discovery regressions."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins/privacy-datamap/scripts'))


class StaticBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = pathlib.Path(temporary.name)

    def test_literal_store_bindings_are_static_and_conservative(self):
        import static_bindings
        source = '''from dataclasses import dataclass
@dataclass(frozen=True)
class Store:
    destination: str
    schema_path: str
LEFT = Store("left", "left.sql")
RIGHT = Store(schema_path="right.sql", destination="right")
DYNAMIC = Store(destination(), "left.sql")
raise RuntimeError("target code must never execute")
'''
        (self.repo/'storage.py').write_text(source)
        for name in ('left.sql', 'right.sql'):
            (self.repo/name).write_text('CREATE TABLE documents (id TEXT);')
        paths = ['storage.py', 'left.sql', 'right.sql']
        bindings = static_bindings.discover(self.repo, paths)
        self.assertEqual({key: [row['key'] for row in rows] for key, rows in bindings.items()},
                         {'left.sql': ['left'], 'right.sql': ['right']})
        self.assertEqual(bindings['left.sql'][0]['refs'], ['storage.py:3', 'storage.py:6'])
        for mutated in (source.replace('frozen=True', 'frozen=False'),
                        source.replace('    destination: str', '    def __post_init__(self): pass\n    destination: str'),
                        source + '\nStore = other_factory\n',
                        source + '\ndef Store(*args): pass\n',
                        source + '\nif flag:\n    Store = other_factory\n',
                        source.replace('    schema_path: str', '    schema_path: str\n    extra: str')):
            (self.repo/'storage.py').write_text(mutated)
            self.assertEqual(static_bindings.discover(self.repo, paths), {})
        (self.repo/'storage.py').write_text(source)
        self.assertNotIn('right.sql', static_bindings.discover(self.repo, paths[:-1]))


if __name__ == '__main__':
    unittest.main()
