"""Select a temporary database before any test imports application settings."""
import atexit
import os
from pathlib import Path
import sys
import tempfile

_work = tempfile.TemporaryDirectory(prefix='wb-isolated-tests-')
DATABASE_PATH = (Path(_work.name) / 'test.db').resolve()
DATABASE_URL = 'sqlite:///' + DATABASE_PATH.as_posix()

# unittest loads every named module before running setUp. Importing converter
# from an earlier suite used to freeze .env's real MySQL engine first.
if 'admin.config' in sys.modules:
    raise RuntimeError('Refusing tests: application settings were loaded before database isolation. Run tests in a fresh process.')
os.environ.update(ADMIN_DATABASE_URL=DATABASE_URL, ADMIN_REDIS_URL='',
                  ADMIN_GATEWAY_LIMIT_BACKEND='memory')
def _cleanup():
    module = sys.modules.get('admin.db')
    engine = getattr(module, 'engine', None)
    if engine is not None:
        assert_test_database(engine)
        engine.dispose()
    _work.cleanup()


atexit.register(_cleanup)


def assert_test_database(engine):
    if engine.dialect.name != 'sqlite' or Path(engine.url.database).resolve() != DATABASE_PATH:
        raise RuntimeError('Refusing test database writes: engine is not the isolated temporary SQLite database')
