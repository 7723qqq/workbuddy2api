"""Public listing freshness and permissions, with isolated DB and upstream I/O."""
import unittest
from unittest.mock import MagicMock, patch

from tests import assert_test_database
from admin.db import Base, SessionLocal, engine
from admin.models import Account, ApiKey, ModelConfig, ModelGroup, ModelGroupItem
from admin.routers import proxy
from admin.security import hash_key
from admin.model_listing import enrich_entries


class LiveListingTests(unittest.TestCase):
    def setUp(self):
        assert_test_database(engine)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.account = Account(uid="offline-list", auth_json="{}", balance_remain=100)
        self.group = ModelGroup(name="limited")
        self.db.add_all([self.account, self.group])
        self.db.flush()
        self.db.add_all([
            ModelConfig(model_id="visible", enabled=1),
            ModelConfig(model_id="outside", enabled=1),
            ModelConfig(model_id="disabled", enabled=0),
            ModelGroupItem(group_id=self.group.id, model_id="visible"),
            ModelGroupItem(group_id=self.group.id, model_id="disabled"),
            ApiKey(key_hash=hash_key("list-key"), group_id=self.group.id, unlimited=1),
            ApiKey(key_hash=hash_key("public-list-key"), unlimited=1),
        ])
        self.db.commit()
        self.session = MagicMock()
        self.session.updated_json.return_value = "{}"
        self.context = MagicMock()
        self.context.__enter__.return_value = self.session

    def tearDown(self):
        self.db.close()

    def listing(self, token="list-key"):
        with patch.object(proxy.backend, "AccountSession", return_value=self.context), \
                patch.object(proxy.model_catalog, "get_models", side_effect=AssertionError("Listing used TTL cache")):
            return proxy.models(db=self.db, x_api_key=token, authorization=None)

    def test_repeated_lists_fetch_new_prices_and_capacity_without_changing_ids(self):
        self.session.fetch_models.side_effect = [
            [{"id":"visible", "credits":"x0.11"}, {"id":"outside"}, {"id":"disabled"}],
            [{"id":"visible", "credits":"x0.25"}],
        ]
        self.session.fetch_model_busy.side_effect = [
            {"code":0, "data":[{"model":"visible", "busy_level":"busy"}]},
            {"code":0, "data":[{"model":"visible", "busy_level":"idle"}]},
        ]
        first = self.listing()["data"]
        second = self.listing()["data"]
        self.assertEqual([m["id"] for m in first], ["visible"])
        self.assertEqual(first[0]["name"], "visible · ×0.11 · 轻微拥堵")
        self.assertEqual(second[0]["name"], "visible · ×0.25 · 空闲")
        self.assertEqual(first[0]["busy_level"], "busy")
        self.assertEqual(first[0]["credit_multiplier"], 0.11)
        self.assertFalse(first[0]["busy_stale"])
        self.assertTrue(first[0]["busy_observed_at"].endswith("Z"))
        self.assertEqual(self.session.fetch_models.call_count, 2)
        self.assertEqual(self.session.fetch_model_busy.call_args_list,
                         [unittest.mock.call(["visible"]), unittest.mock.call(["visible"])])

    def test_capacity_failure_returns_unknown_and_retains_model_catalog(self):
        self.session.fetch_models.return_value = [{"id":"visible", "credits":"x0"}]
        self.session.fetch_model_busy.side_effect = RuntimeError("offline upstream")
        entry = self.listing()["data"][0]
        self.assertEqual(entry["name"], "visible · ×0 · 未知")
        self.assertTrue(entry["busy_stale"])
        self.assertIsNone(entry["busy_observed_at"])

    def test_all_disabled_returns_empty_for_unrestricted_key(self):
        self.db.query(ModelConfig).update({"enabled":0})
        self.db.commit()
        self.session.fetch_models.return_value = [{"id":"visible", "credits":"x0"}]
        self.assertEqual(self.listing("public-list-key")["data"], [])
        self.session.fetch_model_busy.assert_not_called()

    def test_capacity_batches_and_unknown_price_or_unrecognized_level(self):
        entries = [{"id":f"model-{i}", "credit_multiplier":None} for i in range(51)]
        self.session.fetch_model_busy.side_effect = lambda batch: {
            "code":0, "data":[{"model":model, "busy_level":"new-status"} for model in batch]}
        result = enrich_entries(self.session, entries)
        self.assertEqual([len(c.args[0]) for c in self.session.fetch_model_busy.call_args_list], [50,1])
        self.assertTrue(all(e["name"].endswith("×未知 · 未知") for e in result))

    def test_partial_or_invalid_capacity_is_never_reported_as_fresh(self):
        entries = [{"id":f"model-{i}", "credit_multiplier":0} for i in range(51)]
        self.session.fetch_model_busy.side_effect = [
            {"code":0, "data":[{"model":e["id"], "busy_level":"busy"} for e in entries[:50]]},
            {"code":123, "data":[]},
        ]
        result = enrich_entries(self.session, entries)
        self.assertTrue(all(e["busy_level"] == "unknown" and e["busy_stale"] for e in result))


if __name__ == "__main__":
    unittest.main()
