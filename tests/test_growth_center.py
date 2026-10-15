"""成长中心接口隔离、审计、快照失败回退及手动操作测试。"""
import atexit
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import DATABASE_URL, assert_test_database
os.environ["ADMIN_DATABASE_URL"] = DATABASE_URL
os.environ["ADMIN_REDIS_URL"] = ""
os.environ["ADMIN_GATEWAY_LIMIT_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from admin.db import Base, SessionLocal, engine
from admin.models import Account, GrowthPlanRecord, Schedule, SystemSetting
from admin import growth_center as core
from admin.routers import growth_center as routes
from admin.security import require_admin
from admin.growth_plan import _Lease, _read

assert_test_database(engine)
Base.metadata.create_all(engine)
atexit.register(engine.dispose)
app = FastAPI()
app.include_router(routes.router)


class FakeSession:
    calls = []
    fail = False
    draw_fail = False
    def __init__(self, auth): self.auth = auth
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def updated_json(self): return self.auth
    def _growth(self, method, path):
        self.calls.append(path)
        if self.fail: return {"ok": False, "status": 503}
        if 'lottery/summary' in path: return {"ok": True, "data": {"module": {"enabled": True}}}
        if '/records?' in path or '/draws?' in path or '/rewards?' in path:
            return {"ok": True, "data": {"records": [{"reward_credit": 5, "phone": "secret", "location": {"name": "古镇客栈", "url": "secret"}, "letter": "secret"}],
                "items": [{"prize_name": "杯子", "recipient_phone": "secret", "shipping_address": "secret", "status": "pending"}], "total": 1}}
        if path.endswith('/heatmap'):
            return {"ok": True, "data": {"cells": [{"date": "2026-10-14", "score": 2, "token": "secret"}], "today": {"is_active": True}, "auth": "secret"}}
        return {"ok": True, "data": {"balance": 9, "account_token": "secret"}}
    def get_checkin_status(self): return {"active": True, "today_checked_in": False}
    def growth_streak(self):
        return {"ok": True, "data": {"redemption_status": {"tier_7d_status": "available", "tier_14d_status": "locked"}}}
    def growth_redeem(self, tier, token):
        self.calls.append((tier, token))
        return {"ok": True, "data": {"credit_granted": 50, "cards_overflow": 1}}
    def growth_lottery_chances(self): return {"ok": True, "data": {"balance": 1}}
    def growth_lottery_draw(self, token):
        self.calls.append(token)
        return {"ok": not self.draw_fail, "data": {"prize_name": "10积分", "credit_granted": 10}}
    def growth_buddy_quota(self): return {"ok": True, "data": {"affordable": 3}}
    def growth_buddy_open(self, count):
        self.calls.append(count)
        return {"ok": True, "data": {"results": [{"template": {"name": "星际喵", "rarity": "SR"}}]}}


class GrowthCenterTests(unittest.TestCase):
    def setUp(self):
        assert_test_database(engine)
        app.dependency_overrides.clear()
        app.dependency_overrides[require_admin] = lambda: True
        self.client = TestClient(app)
        with SessionLocal() as db:
            for model in (GrowthPlanRecord, Account, SystemSetting, Schedule): db.query(model).delete()
            db.add_all([Account(id=1, name="13912345678", uid="one", auth_json='{"auth":"secret"}', status="active"),
                        Account(id=2, name="Other", uid="two", auth_json='{"auth":"secret"}', status="active")])
            db.commit()
        FakeSession.calls = []
        FakeSession.fail = FakeSession.draw_fail = False
        patcher = patch.object(routes, 'AccountSession', FakeSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_routes_require_admin(self):
        app.dependency_overrides.clear()
        for path in ('/accounts', '/accounts/1', '/accounts/1/records', '/accounts/1/official-records'):
            self.assertIn(self.client.get('/api/growth-center' + path).status_code, (401, 403))
        for path in ('refresh', 'action'):
            self.assertIn(self.client.post('/api/growth-center/accounts/1/' + path, json={'action':'open'}).status_code, (401,403))

    def test_snapshot_is_account_scoped_and_overview_does_not_contact_upstream(self):
        with SessionLocal() as db:
            a = db.get(Account, 1)
            snapshot = {}
            core.observe(snapshot, 'energy', {'balance': 43, 'auth_token':'secret'})
            core.store_snapshot(db, a, snapshot)
        first = self.client.get('/api/growth-center/accounts/1').json()
        second = self.client.get('/api/growth-center/accounts/2').json()
        self.assertEqual(first['snapshot']['sections']['energy']['data']['balance'], 43)
        self.assertFalse(second['snapshot'])
        self.assertEqual(FakeSession.calls, [])
        response = self.client.get('/api/growth-center/accounts').json()
        self.assertEqual(response['items'][0]['name'], '139****5678')
        self.assertNotIn('secret', json.dumps([first, second, response]))

    def test_record_pagination_never_mixes_accounts(self):
        with SessionLocal() as db:
            for aid in (1, 2, 1): core.record_result(db, aid, {'ok':True,'credit':5,'reason':'ok'})
        first = self.client.get('/api/growth-center/accounts/1/records?limit=1').json()
        second = self.client.get('/api/growth-center/accounts/1/records?limit=1&before='+str(first['next_before'])).json()
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(len(first['items'] + second['items']), 2)
        self.assertEqual(self.client.get('/api/growth-center/accounts/999/records').status_code, 404)

    def test_official_history_whitelists_shipping_credentials(self):
        for kind in ('travel', 'draws', 'rewards'):
            data = self.client.get('/api/growth-center/accounts/1/official-records?kind='+kind)
            self.assertEqual(data.status_code, 200)
            self.assertNotIn('secret', data.text)
        self.assertEqual(self.client.get('/api/growth-center/accounts/1/official-records?kind=arbitrary').status_code, 400)

    def test_partial_refresh_preserves_existing_successful_section(self):
        snapshot = {}
        core.observe(snapshot, 'energy', {'balance':43})
        FakeSession.fail = True
        core.update_snapshot(FakeSession('{}'), snapshot, lambda value: None)
        self.assertEqual(snapshot['sections']['energy']['data']['balance'],43)
        self.assertIn('energy', snapshot['errors'])
        self.assertNotIn('secret', json.dumps(snapshot))

    def test_newer_snapshot_section_is_not_overwritten_by_old_worker(self):
        with SessionLocal() as db:
            account = db.get(Account, 1)
            core.store_snapshot(db, account, {'sections':{'energy':{'data':{'balance':43},'updated_at':'2026-10-14T10:00:00Z'}}})
            core.store_snapshot(db, account, {'sections':{'energy':{'data':{'balance':5},'updated_at':'2026-10-14T09:00:00Z'},
                'heatmap':{'data':{'cells':[]},'updated_at':'2026-10-14T10:01:00Z'}}})
            current = core.load_snapshot(db, account)
        self.assertEqual(current['sections']['energy']['data']['balance'],43)
        self.assertIn('heatmap',current['sections'])

    def test_manual_action_excludes_scheduler_and_records_actual_reward(self):
        with SessionLocal() as db:
            lease = _Lease(db)
            self.assertTrue(lease.acquire())
            self.assertEqual(self.client.post('/api/growth-center/accounts/1/action',json={'action':'redeem','tier':'7d'}).status_code,409)
            lease.release()
        result = self.client.post('/api/growth-center/accounts/1/action',json={'action':'redeem','tier':'7d'}).json()
        self.assertEqual(result['credit'],50)
        self.assertIn('溢出',result['reason'])
        log=self.client.get('/api/growth-center/accounts/1/records').json()['items'][0]
        self.assertEqual(log['source'],'manual')
        self.assertEqual(log['credit'],50)
        self.assertEqual(self.client.post('/api/growth-center/accounts/1/action',json={'action':'redeem','tier':'14d'}).status_code,400)

    def test_manual_open_is_a_single_box_and_marks_automation_state(self):
        result=self.client.post('/api/growth-center/accounts/1/action',json={'action':'open'}).json()
        self.assertTrue(result['ok'])
        self.assertIn('星际喵',result['reason'])
        self.assertIn(1,FakeSession.calls)
        with SessionLocal() as db:
            state=_read(db, 'growth_plan:account:'+core.identity(db.get(Account,1)))
            self.assertEqual(state['box_attempt_day'],state['box_confirmed_day'])

    def test_failed_manual_draw_reuses_same_token(self):
        FakeSession.draw_fail=True
        for _ in range(2):
            self.client.post('/api/growth-center/accounts/1/action',json={'action':'draw'})
        tokens=[c for c in FakeSession.calls if isinstance(c,str) and c.startswith('draw-')]
        self.assertEqual(len(tokens),2)
        self.assertEqual(tokens[0],tokens[1])

    def test_last_schedule_record_is_imported_once_for_existing_account(self):
        with SessionLocal() as db:
            db.add(Schedule(task='run_growth_plan',last_result=json.dumps({'detail':[
                {'account_id':1,'ok':True,'credit':5,'steps':[{'step':'礼物','ok':True,'message':'已领','reward':5}]},
                {'account_id':999,'ok':True,'credit':0}]})))
            db.commit()
            core.import_last_run(db)
            core.import_last_run(db)
            self.assertEqual(db.query(GrowthPlanRecord).count(),1)
            self.assertEqual(db.query(GrowthPlanRecord).one().credit,5)

    def test_refresh_returns_immediately_deduplicates_and_persists_readonly_data(self):
        entered, finish = threading.Event(), threading.Event()
        original=FakeSession._growth
        def slow(sess, method, path):
            entered.set()
            finish.wait(5)
            return original(sess,method,path)
        with patch.object(FakeSession,'_growth',slow):
            start=time.monotonic()
            result=self.client.post('/api/growth-center/accounts/1/refresh')
            self.assertEqual(result.status_code,200)
            self.assertLess(time.monotonic()-start,1)
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.client.get('/api/growth-center/accounts/1').json()['refresh']['running'])
            duplicate=self.client.post('/api/growth-center/accounts/1/refresh')
            self.assertTrue(duplicate.json()['running'])
            finish.set()
            for _ in range(200):
                view=self.client.get('/api/growth-center/accounts/1').json()
                if not view['refresh']['running']: break
                time.sleep(.01)
            self.assertFalse(view['refresh']['running'])
            self.assertIn('heatmap',view['snapshot']['sections'])
            self.assertNotIn('secret',json.dumps(view['snapshot']))
            self.assertEqual(len(FakeSession.calls),len(core.SECTIONS))



if __name__ == '__main__': unittest.main()
