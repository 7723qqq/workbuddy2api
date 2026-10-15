"""Removed models must be rejected before upstream I/O in every protocol."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests import assert_test_database
import httpx
from fastapi import FastAPI
from admin.db import Base, SessionLocal, engine, get_db
from admin.models import ApiKey, ModelConfig, ModelGroup, ModelGroupItem
from admin.routers import groups, models, proxy
from admin.security import hash_key, require_admin


class ModelAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        assert_test_database(engine)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.group = ModelGroup(name="restricted")
        self.db.add(self.group)
        self.db.flush()
        self.db.add_all([ModelConfig(model_id="kept", enabled=1),
                         ModelConfig(model_id="removed", enabled=1),
                         ModelGroupItem(group_id=self.group.id, model_id="kept"),
                         ModelGroupItem(group_id=self.group.id, model_id="removed"),
                         ApiKey(key_hash=hash_key("group-key"), group_id=self.group.id, unlimited=1),
                         ApiKey(key_hash=hash_key("public-key"), unlimited=1)])
        self.db.commit()
        app = FastAPI()
        for router in (proxy.router, groups.router, models.router):
            app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: True
        def database():
            with SessionLocal() as db:
                yield db
        app.dependency_overrides[get_db] = database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.upstream = AsyncMock(side_effect=AssertionError("Denied model reached upstream"))
        self.patch = patch.object(proxy, "_account_session_async", self.upstream)
        self.patch.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.patch.stop()
        self.db.close()

    async def assert_denied(self, model, token="group-key", expected_status=403):
        for endpoint in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
            for stream in (False, True):
                with self.subTest(endpoint=endpoint, stream=stream, model=model):
                    payload = {"model":model, "stream":stream, "max_tokens":8,
                               "messages":[{"role":"user", "content":"hi"}]}
                    if endpoint == "/v1/responses":
                        payload["input"] = "hi"
                    r = await self.client.post(endpoint, json=payload, headers={"X-API-Key":token})
                    self.assertEqual(r.status_code, expected_status, r.text)
                    self.assertIn(r.json()["error"]["type"], ("model_not_in_group", "model_not_found"))
        self.upstream.assert_not_called()

    async def test_group_removal_immediately_rejects_explicit_model_in_all_protocols(self):
        # The model stays globally enabled and usable by an unrestricted Key.
        r = await self.client.put(f"/api/groups/{self.group.id}", json={"name":"restricted", "models":["kept"]})
        self.assertEqual(r.status_code, 200)
        await self.assert_denied("removed")
        self.db.expire_all()
        self.assertTrue(models._is_model_allowed(self.db, "removed"))
        key = self.db.query(ApiKey).filter_by(key_hash=hash_key("group-key")).one()
        allowed = proxy._key_allowed_models(self.db, key)
        self.assertEqual(allowed, {"kept"})
        self.assertEqual(proxy._candidate_models(self.db, set(), allowed), ["kept"])
        self.assertEqual(proxy._pick_best_model(self.db, "auto", allowed), "kept")

    async def test_claude_alias_cannot_fall_back_after_target_removed(self):
        self.db.query(ModelGroupItem).filter_by(model_id="removed").delete()
        self.db.commit()
        with patch.object(proxy, "_ANTHROPIC_MODEL_TIERS", (("sonnet", "removed"),)):
            r = await self.client.post("/v1/messages", headers={"X-API-Key":"group-key"},
                json={"model":"claude-sonnet-4-5", "max_tokens":8,
                      "messages":[{"role":"user","content":"hi"}]})
        self.assertEqual(r.status_code, 403, r.text)
        self.upstream.assert_not_called()

    async def test_disabled_concrete_model_cannot_become_auto(self):
        self.db.query(ModelConfig).filter_by(model_id="removed").update({"enabled":0})
        self.db.commit()
        await self.assert_denied("removed", token="public-key", expected_status=400)

    async def test_all_disabled_is_not_an_unconfigured_whitelist(self):
        self.db.query(ModelConfig).update({"enabled":0})
        self.db.commit()
        self.assertEqual(groups.effective_group_models(self.db, self.group.id), set())
        self.assertEqual(groups.group_model_map(self.db), {self.group.id:set()})
        await self.assert_denied("kept")
        await self.assert_denied("kept", token="public-key", expected_status=400)
        self.assertIsNone(proxy._pick_best_model(self.db, "auto"))

    def test_claude_alias_and_auto_still_resolve_within_group(self):
        key = self.db.query(ApiKey).filter_by(key_hash=hash_key("group-key")).one()
        with patch.object(proxy, "_ANTHROPIC_MODEL_TIERS", (("sonnet", "kept"),)):
            mapped = proxy._map_anthropic_model(self.db, "claude-sonnet-4-5", key)
        self.assertEqual(mapped, "kept")
        self.assertEqual(proxy._pick_best_model(self.db, mapped, {"kept"}), "kept")
        self.assertEqual(proxy._map_anthropic_model(self.db, "auto", key), "auto")


if __name__ == "__main__":
    unittest.main()
