"""Offline regressions for observed recovery clocks and background admission."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests  # Select an isolated database before importing application settings.
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from admin import maintenance, model_busy, scheduler
from admin.config import settings
from admin.db import Base
from admin.health import risk_health
from admin.models import Account, AccountModelCooldown, Schedule, UsageLog
from admin.pool import InFlight, PoolRuntime, POOL, WafIpGate
from admin.routers import proxy
from admin.routers.accounts import _active_model_limits


class RiskSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.accounts = [Account(uid=f'offline-{i}', auth_json='{}', balance_remain=100)
                         for i in range(3)]
        self.db.add_all(self.accounts)
        self.db.commit()
        self.patches = [patch.object(settings, 'KEEPALIVE_ACCOUNT_GAP', 0),
                        patch.object(POOL, 'inflight', InFlight()),
                        patch.object(POOL, 'waf', WafIpGate())]
        for p in self.patches:
            p.start()
        model_busy._cache.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()

    def test_explicit_account_reset_is_not_truncated_or_shortened(self):
        a = self.accounts[0]
        reset = datetime.utcnow() + timedelta(hours=5)
        proxy._apply_account_policy(self.db, a, 'soft_rate', 429, '{"code":6002}', reset_at=reset)
        self.assertEqual(a.cool_until, reset)
        proxy._apply_account_policy(self.db, a, 'soft_rate', 429, '', reset_at=reset - timedelta(hours=1))
        self.assertEqual(a.cool_until, reset)
        later = reset + timedelta(hours=1)
        proxy._apply_account_policy(self.db, a, 'soft_rate', 429, '', reset_at=later)
        self.assertEqual(a.cool_until, later)
        self.assertIsNone(proxy._select_account(self.db, account_id=a.id, mark_picked=False))

    def test_busy_model_obeys_explicit_clock_and_preserves_source(self):
        a = self.accounts[0]
        reset = datetime.utcnow() + timedelta(hours=5)
        proxy._apply_account_policy(self.db, a, 'model_rate', 429, '{"code":14003}', 'busy', reset)
        row = self.db.query(AccountModelCooldown).one()
        self.assertEqual(row.until, reset)
        self.assertIsNone(a.cool_until)
        self.assertEqual(_active_model_limits(self.db, datetime.utcnow())[a.id][0]['reset_source'], 'upstream')
        proxy._apply_account_policy(self.db, a, 'model_rate', 429, '{"code":14003}', 'busy')
        self.assertEqual(row.until, reset)
        self.assertEqual(_active_model_limits(self.db, datetime.utcnow())[a.id][0]['reset_source'], 'upstream')

    def test_invalid_or_absurd_clock_is_bounded(self):
        a = self.accounts[0]
        now = datetime.utcnow()
        proxy._apply_account_policy(self.db, a, 'soft_rate', 429, '', reset_at=now - timedelta(seconds=1))
        self.assertGreater(a.cool_until, now)
        proxy._apply_account_policy(self.db, a, 'soft_rate', 429, '', reset_at=now + timedelta(days=90))
        self.assertLessEqual(a.cool_until, datetime.utcnow() + timedelta(days=7))

    def test_background_reservation_is_exclusive_even_with_unlimited_requests(self):
        a = self.accounts[0]
        with maintenance.account_slot(a, self.db) as reason:
            self.assertIsNone(reason)
            self.assertFalse(POOL.inflight.acquire(a.uid, 0))
            self.assertTrue(POOL.inflight.full(a.uid, 0))
            runtime = PoolRuntime()
            runtime.inflight = POOL.inflight
            self.assertEqual(runtime.available([a]), [])
        self.assertTrue(POOL.inflight.acquire(a.uid, 0))
        POOL.inflight.release(a.uid)

    def test_only_one_racer_can_reserve_an_idle_identity(self):
        inflight = InFlight()
        barrier = Barrier(16)
        def reserve(_):
            barrier.wait()
            return inflight.acquire_maintenance('same')
        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(reserve, range(16)))
        self.assertEqual(sum(results), 1)
        self.assertFalse(inflight.acquire('same', 3))
        inflight.release_maintenance('same')
        self.assertTrue(inflight.acquire('same', 3))

    def test_inflight_and_cooling_skip_without_touching_upstream(self):
        a, b, _ = self.accounts
        POOL.inflight.acquire(a.uid, 3)
        b.cool_until = datetime.utcnow() + timedelta(hours=1)
        self.db.commit()
        with maintenance.account_slot(a, self.db) as reason:
            self.assertEqual(reason, 'inflight')
        with maintenance.account_slot(b, self.db) as reason:
            self.assertEqual(reason, 'account_cooling')
        self.assertEqual(POOL.inflight.count(a.uid), 1)
        POOL.inflight.release(a.uid)

    def test_exception_releases_both_lease_and_batch_lock(self):
        with self.assertRaises(RuntimeError):
            with maintenance.account_slot(self.accounts[0], self.db):
                raise RuntimeError('synthetic failure')
        self.assertEqual(POOL.inflight.maintenance_count(), 0)
        with maintenance.account_slot(self.accounts[1], self.db) as reason:
            self.assertIsNone(reason)

    def test_background_spacing_is_shared_and_waf_rechecked_after_wait(self):
        a = self.accounts[0]
        with patch.object(settings, 'KEEPALIVE_ACCOUNT_GAP', .8), \
             patch.object(maintenance, '_last_finished', 10), \
             patch.object(maintenance.time, 'monotonic', return_value=10.3), \
             patch.object(maintenance.time, 'sleep') as sleep:
            sleep.side_effect = lambda _: (POOL.waf.note('A'), POOL.waf.note('B'))
            with maintenance.account_slot(a, self.db) as reason:
                self.assertEqual(reason, 'waf')
            self.assertAlmostEqual(sleep.call_args.args[0], .5)
        self.assertEqual(POOL.inflight.maintenance_count(), 0)

    def test_waf_from_background_stops_the_rest_of_keepalive_batch(self):
        with patch('admin.backend.AccountSession') as cls:
            sess = cls.return_value
            sess.fetch_models.side_effect = RuntimeError('HTTP 403 blocked by WAF')
            sess.updated_json.return_value = '{}'
            result = scheduler.run_keepalive_tokens(self.db)
        self.assertEqual(cls.call_count, 2)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['disabled'], 0)
        self.assertTrue(POOL.waf.active())
        self.assertEqual(POOL.inflight.maintenance_count(), 0)

    def test_account_restriction_does_not_disable_credentials(self):
        self.db.query(Account).filter(Account.id != self.accounts[0].id).delete()
        self.db.commit()
        with patch('admin.backend.AccountSession') as cls:
            cls.return_value.fetch_models.side_effect = RuntimeError('HTTP 403 {"code":11142}')
            result = scheduler.run_keepalive_tokens(self.db)
        self.assertEqual(result['disabled'], 0)
        self.assertEqual(self.accounts[0].status, 'active')
        self.assertEqual(self.accounts[0].cool_kind, 'account_fault')
        self.assertFalse(POOL.waf.active())

    def test_waf_defers_schedule_without_marking_it_as_run(self):
        prior = datetime.utcnow() - timedelta(days=1)
        row = Schedule(task='keepalive_tokens', last_run_at=prior)
        self.db.add(row)
        self.db.commit()
        POOL.waf.note('A')
        POOL.waf.note('B')
        now = datetime.utcnow()
        with patch.object(scheduler, 'run_task') as run:
            scheduler._run_one(row, self.db, now)
        run.assert_not_called()
        self.assertEqual(row.last_run_at, prior)
        self.assertGreater(row.next_run_at, now)
        self.assertTrue(json.loads(row.last_result)['deferred'])

    def test_manual_run_also_defers_without_advancing_last_run(self):
        from admin.routers.schedules import run_now
        prior = datetime.utcnow() - timedelta(days=1)
        row = Schedule(task='keepalive_tokens', last_run_at=prior)
        self.db.add(row)
        self.db.commit()
        POOL.waf.note('A')
        POOL.waf.note('B')
        result = run_now(row.id, db=self.db)
        self.assertTrue(result['result']['deferred'])
        self.assertEqual(row.last_run_at, prior)

    def test_reservation_reloads_credentials_after_another_request_updated_them(self):
        a = self.accounts[0]
        self.db.expire_on_commit = False
        self.db.query(Account).filter(Account.id == a.id).update(
            {Account.auth_json: '{"updated":true}'}, synchronize_session=False)
        self.db.commit()
        self.assertEqual(a.auth_json, '{}')
        with maintenance.account_slot(a, self.db) as reason:
            self.assertIsNone(reason)
            self.assertEqual(a.auth_json, '{"updated":true}')

    def test_refresh_batches_skip_busy_accounts(self):
        POOL.inflight.acquire(self.accounts[0].uid, 3)
        with patch('admin.routers.accounts._refresh_balance', return_value=True) as refresh:
            result = scheduler.run_task('refresh_balances', self.db)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['refreshed'], 2)
        POOL.inflight.release(self.accounts[0].uid)

    def test_busy_panel_never_probes_during_exit_protection(self):
        POOL.waf.note('A')
        POOL.waf.note('B')
        with patch('admin.backend.AccountSession') as cls:
            result = model_busy.get_busy_levels(self.db, ['busy'])
        cls.assert_not_called()
        self.assertTrue(result['stale'])
        self.assertEqual(result['skip_reason'], 'waf')

    def test_risk_snapshot_uses_recent_gateway_logs_without_credentials(self):
        now = datetime.utcnow()
        self.db.add_all([
            UsageLog(account_id=self.accounts[0].id, model='m', error_kind='waf', created_at=now),
            UsageLog(account_id=self.accounts[1].id, model='m', error_kind='waf', created_at=now),
            UsageLog(model='m', error_kind='success', created_at=now),
            UsageLog(model='m', error_kind='', created_at=now),
            UsageLog(model='m', error_kind='waf', created_at=now - timedelta(hours=1))])
        self.db.commit()
        result = risk_health(self.db, now)
        self.assertEqual(result['request_count'], 4)
        self.assertEqual(result['observations']['success']['requests'], 2)
        self.assertEqual(result['observations']['waf'], {'requests': 2, 'accounts': 2})
        self.assertNotIn('auth_json', json.dumps(result))
        self.assertNotIn(self.accounts[0].uid, json.dumps(result))

    def test_gate_reports_last_fractional_second_as_active(self):
        gate = WafIpGate()
        with patch('admin.pool.time.monotonic', return_value=100):
            gate.note('A')
            gate.note('B')
        with patch('admin.pool.time.monotonic', return_value=159.8):
            self.assertTrue(gate.active())
            self.assertEqual(gate.remaining(), 1)


if __name__ == '__main__':
    unittest.main()
