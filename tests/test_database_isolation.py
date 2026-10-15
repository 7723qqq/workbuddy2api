import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import DATABASE_PATH, assert_test_database


class IsolationTests(unittest.TestCase):
    def test_writes_refuse_mysql_and_unrelated_sqlite(self):
        for dialect, path in [('mysql', 'live'), ('sqlite', str(DATABASE_PATH.parent / 'other.db'))]:
            engine = SimpleNamespace(dialect=SimpleNamespace(name=dialect),url=SimpleNamespace(database=path))
            with self.assertRaisesRegex(RuntimeError, 'Refusing test database writes'):
                assert_test_database(engine)

    def test_preloaded_settings_stop_tests_before_database_import(self):
        script = '''
import sys, types
sys.modules['admin.config'] = types.ModuleType('admin.config')
try:
    import tests
except RuntimeError as exc:
    assert 'before database isolation' in str(exc)
else:
    raise AssertionError('Preloaded settings were accepted')
assert 'admin.db' not in sys.modules
print('Preloaded application settings safely refused')
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
