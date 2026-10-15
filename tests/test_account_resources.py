"""Regression checks for credential lifecycle and empty-database onboarding."""
import atexit
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import DATABASE_URL, assert_test_database
os.environ['ADMIN_DATABASE_URL'] = DATABASE_URL
from admin import backend, post_login
from admin.db import Base, engine, SessionLocal
from admin.models import Account, ModelConfig, SystemSetting
from admin.routers import models
from converter import CredentialError, CredentialManager

assert_test_database(engine)
Base.metadata.create_all(engine)
atexit.register(engine.dispose)


class CredentialSessionTests(unittest.TestCase):
    def test_empty_or_malformed_auth_never_sends_empty_bearer(self):
        with patch('converter.httpx.Client', side_effect=AssertionError('Invalid credentials reached network')):
            for value in ({}, {'auth': {}}, {'auth': {'accessToken': ' ', 'refreshToken': 'r'}},
                          {'auth': {'accessToken': 'token\r\nsecret', 'refreshToken': 'r'}}):
                with self.subTest(value=value), self.assertRaises(CredentialError):
                    backend.AccountSession(json.dumps(value))

    def test_partial_refresh_preserves_existing_auth(self):
        credential = {'auth': {'accessToken': 'old', 'refreshToken': 'refresh'}, 'account': {'uid': 'offline'}}
        class Response:
            def json(self): return {'code': 0, 'data': {'domain': 'example.test'}}
        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **k): return Response()
        manager = CredentialManager.from_json(json.dumps(credential))
        with patch('converter.httpx.Client', Client), patch('converter._get_turing_device_token', return_value=None):
            with self.assertRaisesRegex(CredentialError, '已保留原凭证'):
                manager._refresh()
        self.assertEqual(json.loads(manager.export_json()), credential)

    def test_file_backed_desktop_credentials_still_reload_external_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.info'
            path.write_text('{"account":{"uid":"first"}}', encoding='utf-8')
            manager = CredentialManager(path)
            self.assertEqual(json.loads(manager.export_json())['account']['uid'], 'first')
            path.write_text('{"account":{"uid":"second"}}', encoding='utf-8')
            os.utime(path, (1, 1))
            self.assertEqual(json.loads(manager.export_json())['account']['uid'], 'second')
    def test_refresh_and_export_need_no_temporary_file(self):
        credential = {'auth': {'accessToken': 'old', 'refreshToken': 'refresh'}, 'account': {'uid': 'offline'}}
        class Response:
            def json(self): return {'code': 0, 'data': {'accessToken': 'new', 'refreshToken': 'next', 'expiresIn': 3600}}
        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **k): return Response()
        with patch('builtins.open', side_effect=AssertionError('Credential session tried filesystem access')), \
             patch('converter.httpx.Client', Client), patch.object(CredentialManager, '_build_headers_from', return_value={}):
            with backend.AccountSession(json.dumps(credential)) as session:
                session.cm._refresh()
                result = json.loads(session.updated_json())
                self.assertEqual(result['auth']['accessToken'], 'new')
                self.assertEqual(result['account']['uid'], 'offline')


class ResourceTests(unittest.TestCase):
    def setUp(self):
        assert_test_database(engine)
        post_login._resources.join()
        with SessionLocal() as db:
            for model in (Account, ModelConfig, SystemSetting): db.query(model).delete()
            account = Account(name='offline', uid='offline', auth_json='{}', status='active', balance_remain=0)
            db.add(account); db.commit(); self.account_id = account.id

    def test_model_discovery_accepts_account_before_balance_refresh(self):
        class Session:
            def __init__(self, raw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def fetch_models(self): return [{'id': 'hy3', 'credits': 'x0.00'}]
            def updated_json(self): return '{}'
        with patch.object(backend, 'AccountSession', Session), SessionLocal() as db:
            result = models._do_sync_models(db)
            self.assertEqual(result['added'], 1)
            db.query(ModelConfig).update({ModelConfig.enabled: 0}); db.commit()
            result = models._do_sync_models(db)
            self.assertEqual(result['enabled'], 0)

    def test_resources_run_even_when_reward_queue_is_not_started(self):
        class Session:
            def __init__(self, raw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def fetch_balance(self): return {'total': 4000, 'remain': 3500}
            def updated_json(self): return '{}'
        with patch.object(backend, 'AccountSession', Session), patch.object(models, '_do_sync_models') as sync:
            post_login.enqueue_resources(self.account_id)
            post_login._resources.join()
            sync.assert_called_once()
            with SessionLocal() as db:
                self.assertEqual(db.get(Account, self.account_id).balance_remain, 3500)
                state = json.loads(db.get(SystemSetting, 'account_resources:' + str(self.account_id)).value)
                self.assertEqual(state['status'], 'completed')

    def test_restart_recovers_missing_models_and_failed_initialization(self):
        with SessionLocal() as db:
            db.add(SystemSetting(key='account_resources:' + str(self.account_id), value='{"status":"failed"}'))
            db.commit()
        with patch.object(post_login, 'enqueue_resources') as enqueue:
            post_login.resume()
            enqueue.assert_called_once_with(self.account_id)

    def test_empty_credential_is_reported_without_model_sync_or_network(self):
        with patch('converter.httpx.Client', side_effect=AssertionError('Empty credentials reached network')), \
             patch.object(models, '_do_sync_models') as sync:
            post_login.enqueue_resources(self.account_id)
            post_login._resources.join()
            sync.assert_not_called()
        with SessionLocal() as db:
            state = json.loads(db.get(SystemSetting, 'account_resources:' + str(self.account_id)).value)
            self.assertEqual(state['status'], 'invalid_credential')
            self.assertIn('重新导入', state['errors'][0])


if __name__ == '__main__': unittest.main()
