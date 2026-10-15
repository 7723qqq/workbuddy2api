"""成长计划回归：合成凭证、隔离 SQLite，不调用真实上游。"""
import atexit
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import DATABASE_URL, assert_test_database
os.environ["ADMIN_DATABASE_URL"] = DATABASE_URL
os.environ["ADMIN_GATEWAY_LIMIT_BACKEND"] = "memory"
os.environ["ADMIN_REDIS_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin import growth_plan as gp, scheduler
from admin.db import Base, SessionLocal, engine
from admin.models import Account, ModelConfig, Schedule, SystemSetting, GrowthPlanRecord
from admin.routers import schedules
from admin.backend import AccountSession

assert_test_database(engine)
Base.metadata.create_all(engine)
atexit.register(engine.dispose)
NOW = datetime(2026, 10, 14, 10, tzinfo=timezone(timedelta(hours=8)))


def ok(data=None):
    return {"ok": True, "status": 200, "code": 0, "data": data or {}}


class FakeSession:
    worlds = {}

    def __init__(self, auth_json):
        self.auth = json.loads(auth_json)
        self.world = self.worlds[self.auth["user"]]
        self.world.setdefault("calls", [])

    def record(self, name, *args):
        self.world["calls"].append((name, args))

    def updated_json(self):
        return json.dumps({**self.auth, "refreshed": True})

    def close(self):
        self.world["closed"] = True

    def growth_heatmap(self):
        self.record("heatmap")
        return ok({"today": {"date": self.world.get("day", "2026-10-14"),
                             "is_active": self.world.get("active", False)}})

    def growth_fire_event(self, events, model):
        self.record("chat", events, model)
        if self.world.get("chat_fail"):
            return {"ok": False, "status": 0}
        if not self.world.get("delayed_activity"):
            self.world["active"] = True
        return ok()

    def get_checkin_status(self):
        return {"active": not self.world.get("checkin_ended"),
                "today_checked_in": self.world.get("checked", True)}

    def claim_daily_checkin(self):
        self.record("checkin")
        self.world["checked"] = True
        return {"ok": True, "credit": 100}

    def buddy_info(self):
        return {"instance_id": 1}

    def travel_status(self):
        return self.world.get("travel", {"state": "traveling"})

    def travel_claim(self):
        self.record("gift")
        self.world["travel"] = {"state": "idle", "daily_limit_reached": False}
        return ok({"reward_credit": 5})

    def travel_depart(self, location):
        self.record("depart", location)
        self.world["travel"] = {"state": "traveling"}
        return ok()

    def growth_streak(self):
        return ok({"redemption_status": self.world.get("redemption", {
            "tiers": [{"tier": "7d"}], "tier_7d_status": "locked"})})

    def growth_redeem(self, tier, token):
        self.record("redeem", tier, token)
        self.world["redemption"][f"tier_{tier}_status"] = "claimed"
        return ok({"credit_granted": 50, "cards_overflow": 1})

    def _growth(self, method, path):
        if path.endswith("lottery/summary"):
            return ok({"module": {"enabled": self.world.get("lottery_enabled", True)}})
        if path.endswith("travel/config"):
            return ok({"locations": [{"id": 9}]})
        raise AssertionError(path)

    def growth_lottery_chances(self):
        return ok({"balance": self.world.get("chances", 0)})

    def growth_lottery_draw(self, token):
        self.record("draw", token)
        if self.world.get("draw_fail"):
            return {"ok": False, "status": 0}
        self.world["chances"] = max(0, self.world.get("chances", 0) - 1)
        return ok()

    def growth_buddy_quota(self):
        return ok({"affordable": self.world.get("affordable", 0), "max_open_count": 5})

    def growth_buddy_open(self, count):
        self.record("box", count)
        if self.world.get("box_fail"):
            return {"ok": False, "status": 0}
        return ok({"results": [{}] * count})


class GrowthPlanTests(unittest.TestCase):
    def setUp(self):
        assert_test_database(engine)
        with SessionLocal() as db:
            for model in (GrowthPlanRecord, Account, SystemSetting, Schedule, ModelConfig):
                db.query(model).delete()
            db.commit()
        FakeSession.worlds = {}
        self.session_patch = patch.object(gp, "AccountSession", FakeSession)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.sleep_patch = patch.object(gp.time, "sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def add(self, user, **world):
        FakeSession.worlds[user] = world
        with SessionLocal() as db:
            a = Account(name=user, uid=user, status="active", auth_json=json.dumps({"user": user}))
            db.add(a)
            db.commit()
            return a.id

    def run_plan(self, **kwargs):
        with SessionLocal() as db:
            return gp.run_growth_plan(db, now=kwargs.pop("now", NOW), **kwargs)

    def calls(self, user, name):
        return [args for call, args in FakeSession.worlds[user]["calls"] if call == name]

    def test_each_unused_account_gets_direct_chat_and_persists_tokens(self):
        self.add("a")
        self.add("b")
        result = self.run_plan()
        self.assertEqual(result["processed"], 2)
        for user in ("a", "b"):
            self.assertEqual(self.calls(user, "chat"), [(["chat_request_send"], "hy3")])
        with SessionLocal() as db:
            self.assertTrue(all(json.loads(a.auth_json)["refreshed"] for a in db.query(Account)))
            self.assertTrue(all(a.status == "active" for a in db.query(Account)))
            self.assertEqual(db.query(GrowthPlanRecord).count(),2)
            from admin.growth_center import load_snapshot
            for a in db.query(Account):
                self.assertTrue(load_snapshot(db,a)['sections']['heatmap']['data']['today']['is_active'])

    def test_existing_activity_skips_chat_and_repeat_is_idempotent(self):
        self.add("a", active=True, affordable=12, chances=12, checked=False)
        self.run_plan()
        self.run_plan()
        self.assertFalse(self.calls("a", "chat"))
        self.assertEqual(self.calls("a", "box"), [(5,)])
        self.assertEqual(len(self.calls("a", "draw")), 5)
        self.assertEqual(len(self.calls("a", "checkin")), 1)

    def test_chat_success_with_delayed_activity_is_not_repeated(self):
        self.add("a", delayed_activity=True)
        first = self.run_plan()
        second = self.run_plan()
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(len(self.calls("a", "chat")), 1)
        self.assertIn("活跃尚未入账", second["detail"][0]["reason"])

    def test_failed_chat_is_backed_off_without_disabling_account(self):
        self.add("a", chat_fail=True)
        self.add("b")
        self.run_plan()
        self.run_plan()
        self.assertEqual(len(self.calls("a", "chat")), 1)
        self.assertEqual(len(self.calls("b", "chat")), 1)
        with SessionLocal() as db:
            self.assertTrue(all(a.status == "active" for a in db.query(Account)))

    def test_next_natural_day_enables_another_activity_chat(self):
        self.add("a")
        self.run_plan()
        FakeSession.worlds["a"].update(active=False, day="2026-10-15")
        with patch.object(gp.time, "time", return_value=time.time() + 86400):
            self.run_plan(now=NOW + timedelta(days=1))
        self.assertEqual(len(self.calls("a", "chat")), 2)

    def test_travel_claim_needs_no_record_id_and_departs_to_real_location(self):
        self.add("a", active=True, travel={"state": "arrived"})
        result = self.run_plan()
        self.assertEqual(self.calls("a", "gift"), [()])
        self.assertEqual(self.calls("a", "depart"), [(9,)])
        self.assertEqual(result["credit"], 5)

    def test_latest_travel_config_codes_are_compatible(self):
        self.add("a", active=True, travel={"state": "idle"})
        original = FakeSession._growth
        def latest(sess, method, path):
            if path.endswith("travel/config"):
                return ok({"locations": [{"code": "coffee"}]})
            return original(sess, method, path)
        with patch.object(FakeSession, "_growth", latest):
            self.run_plan()
        self.assertEqual(self.calls("a", "depart"), [(1,)])

    def test_redeem_only_available_tiers_no_guessing_days(self):
        self.add("a", active=True, redemption={"tiers": [{"tier": "9d"}, {"tier": "14d"}, {"tier": "28d"}],
            "tier_9d_status": "available", "tier_14d_status": "claimed", "tier_28d_status": "locked"})
        result = self.run_plan()
        self.run_plan()
        self.assertEqual([c[0] for c in self.calls("a", "redeem")], ["9d"])
        self.assertEqual(result["credit"], 50)
        self.assertIn("溢出 1 张", result["detail"][0]["reason"])

    def test_ambiguous_draw_reuses_persistent_token_even_next_day(self):
        self.add("a", active=True, chances=1, draw_fail=True)
        self.run_plan()
        self.run_plan(now=NOW + timedelta(days=1))
        draws = self.calls("a", "draw")
        self.assertEqual(len(draws), 2)
        self.assertEqual(draws[0], draws[1])
        FakeSession.worlds["a"].update(draw_fail=False, chances=0)
        self.run_plan()
        self.assertEqual(self.calls("a", "draw")[-1], draws[0])

    def test_ambiguous_box_does_not_consume_again_today(self):
        self.add("a", active=True, affordable=3, box_fail=True)
        self.assertFalse(self.run_plan()["ok"])
        self.assertFalse(self.run_plan()["ok"])
        self.assertEqual(self.calls("a", "box"), [(3,)])

    def test_no_energy_or_disabled_lottery_causes_no_mutation(self):
        self.add("a", active=True, affordable=0, chances=10, lottery_enabled=False, checkin_ended=True, checked=False)
        self.run_plan()
        for name in ("box", "draw", "checkin"):
            self.assertFalse(self.calls("a", name))

    def test_database_lease_excludes_second_worker_and_allows_release(self):
        with SessionLocal() as first, SessionLocal() as second:
            l1, l2 = gp._Lease(first), gp._Lease(second)
            self.assertTrue(l1.acquire())
            self.assertFalse(l2.acquire())
            l1.renew()
            l1.release()
            self.assertTrue(l2.acquire())
            l2.release()
            self.assertTrue(l1.acquire())  # reuse the same ORM session without stale lock state
            l1.release()

    def test_expired_lease_recovers_after_process_exit(self):
        with SessionLocal() as db:
            gp._write(db, gp._LEASE_KEY, {"owner": "dead-process", "expires": time.time() - 1})
            lease = gp._Lease(db)
            self.assertTrue(lease.acquire())
            lease.release()

    def test_time_budget_rotates_accounts_instead_of_starving_tail(self):
        ids = [self.add(n) for n in ("a", "b", "c")]
        seen = []
        def fast(sess, *args):
            seen.append(sess.auth["user"])
            return {"ok": True, "credit": 0, "reason": "done"}
        with patch.object(gp, "_account_plan", side_effect=fast):
            for expected in ids:
                with patch.object(gp.time, "monotonic", side_effect=[0, 0, 2, 2]):
                    result = self.run_plan(budget_seconds=1)
                self.assertEqual(result["detail"][0]["account_id"], expected)
                self.assertEqual(result["pending"], len(ids) - ids.index(expected) - 1)
        self.assertEqual(seen, ["a", "b", "c"])

    def test_account_timeout_does_not_abort_other_accounts(self):
        self.add("a", active=True)
        self.add("b", active=True)
        original = FakeSession.buddy_info
        def fail_first(sess):
            if sess.auth["user"] == "a":
                raise TimeoutError("sensitive upstream text")
            return original(sess)
        with patch.object(FakeSession, "buddy_info", fail_first):
            result = self.run_plan()
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertTrue(FakeSession.worlds["a"]["closed"])
        self.assertTrue(FakeSession.worlds["b"]["closed"])

    def test_deleted_pending_accounts_do_not_stall_future_runs(self):
        self.add("a", active=True)
        with SessionLocal() as db:
            gp._write(db, gp._CURSOR_KEY, {"id": 123, "remaining": [999]})
        self.run_plan()
        result = self.run_plan()
        self.assertEqual(result["processed"], 1)

    def test_defaults_added_once_without_reenabling_disabled_task(self):
        with SessionLocal() as db:
            scheduler.ensure_growth_plan_schedule(db)
            scheduler.ensure_growth_plan_schedule(db)
            row = db.query(Schedule).one()
            self.assertEqual(row.interval_minutes, 60)
            row.enabled = 0
            db.commit()
            scheduler.ensure_growth_plan_schedule(db)
            self.assertEqual(db.query(Schedule).count(), 1)
            self.assertEqual(row.enabled, 0)
        self.assertIn("run_growth_plan", schedules.TASK_CHOICES)

    def test_manual_execution_returns_immediately_and_prevents_duplicate_job(self):
        self.add("a", active=True)
        entered, finish, finished = threading.Event(), threading.Event(), threading.Event()
        original = gp.run_growth_plan
        def paused(db, **kwargs):
            entered.set()
            finish.wait(5)
            try:
                return original(db, now=NOW, **kwargs)
            finally:
                finished.set()
        with patch.object(gp, "run_growth_plan", side_effect=paused) as runner:
            with SessionLocal() as db:
                scheduler.ensure_growth_plan_schedule(db)
                row = db.query(Schedule).one()
                started = time.monotonic()
                response = schedules.run_now(row.id, True, db)
                self.assertTrue(response["result"]["running"])
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.wait(2))
                schedules.run_now(row.id, True, db)
                self.assertEqual(runner.call_count, 1)
                finish.set()
                self.assertTrue(finished.wait(5))
            # 等待 worker 落下最后结果再清理测试数据库。
            for _ in range(200):
                with SessionLocal() as db:
                    result = json.loads(db.query(Schedule).one().last_result)
                if not result.get("running"):
                    break
                threading.Event().wait(.01)
            self.assertFalse(result.get("running", False))

    def test_travel_adapter_sends_empty_object(self):
        session = object.__new__(AccountSession)
        with patch.object(session, "_growth", return_value=ok()) as request:
            AccountSession.travel_claim(session, 123)
        request.assert_called_once_with("POST", "/activity/growth/buddy/travel/claim", {})


if __name__ == "__main__":
    unittest.main()
