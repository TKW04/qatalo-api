"""Tests de recipes.py (CRUD de recetas; no tocan inventario).

Ejecutar:  python3 -m pytest -q tests
No toca AWS: las tablas de DynamoDB se reemplazan por fakes en memoria.
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "QATOLO"))
sys.path.insert(0, HERE)
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from botocore.exceptions import ClientError  # noqa: E402

import recipes  # noqa: E402
from test_production_api import FakeTable  # noqa: E402

BIZ = [{"business_id": "b1", "user_id": "u1"}, {"business_id": "b2", "user_id": "u2"}]


def _recipe(**over):
    body = {
        "name": "Pan de agua",
        "yield_lb": 12.5,
        "notes": "  horno 180  ",
        "ingredients": [
            {"name": "Harina", "quantity": 5, "unit": "lb", "cost_per_recipe": 250.75, "store": "Sirena"},
            {"name": "Sal", "quantity": 0.5, "unit": "oz", "cost_per_recipe": 0},
        ],
        "packaging": [
            {"name": "Funda", "quantity_per_unit": 1, "unit_cost": 3.5, "store": "Plaza"},
        ],
    }
    body.update(over)
    return body


class RecipesApiTest(unittest.TestCase):
    def setUp(self):
        self.business = FakeTable("business_id", BIZ)
        self.table = FakeTable(("business_id", "recipe_id"))
        self.patches = [
            mock.patch.object(recipes, "business_table", self.business),
            mock.patch.object(recipes, "recipes_table", self.table),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _route(self, path, method, body=None, user_id="u1", raw=None):
        event = {"body": raw if raw is not None else (json.dumps(body) if body is not None else None)}
        r = recipes.recipes_routes(f"/Prod{path}", method, event, "dueno@x.com", user_id, "Prod")
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "*")
        return r["statusCode"], json.loads(r["body"])

    # ---------- POST ----------
    def test_create_stores_decimals_and_returns_numbers(self):
        status, rec = self._route("/recipes", "POST", _recipe())
        self.assertEqual(status, 200)
        for k in ("recipe_id", "name", "yield_lb", "notes", "ingredients", "packaging", "create_date",
                  "update_date"):
            self.assertIn(k, rec)
        self.assertNotIn("business_id", rec)
        self.assertEqual(rec["name"], "Pan de agua")
        self.assertEqual(rec["notes"], "horno 180")
        self.assertEqual(rec["yield_lb"], 12.5)
        self.assertEqual(rec["ingredients"][0], {"name": "Harina", "quantity": 5, "unit": "lb",
                                                 "cost_per_recipe": 250.75, "store": "Sirena"})
        self.assertEqual(rec["ingredients"][1]["store"], "")
        self.assertEqual(rec["ingredients"][1]["cost_per_recipe"], 0)
        self.assertEqual(rec["packaging"][0], {"name": "Funda", "quantity_per_unit": 1, "unit_cost": 3.5,
                                               "store": "Plaza"})
        stored = self.table.items[("b1", rec["recipe_id"])]
        self.assertIsInstance(stored["yield_lb"], Decimal)
        self.assertEqual(stored["yield_lb"], Decimal("12.5"))
        self.assertIsInstance(stored["ingredients"][0]["cost_per_recipe"], Decimal)
        self.assertIsInstance(stored["packaging"][0]["unit_cost"], Decimal)
        self.assertFalse(any(isinstance(v, float) for v in stored.values()))

    def test_create_minimal(self):
        status, rec = self._route("/recipes", "POST", {"name": "X", "yield_lb": 1})
        self.assertEqual(status, 200)
        self.assertEqual(rec["ingredients"], [])
        self.assertEqual(rec["packaging"], [])
        self.assertEqual(rec["notes"], "")

    def test_create_validation(self):
        bad = [
            {"name": ""}, {"name": "   "}, {"name": None}, {"name": 5}, {"name": "x" * 101},
            {"yield_lb": 0}, {"yield_lb": -1}, {"yield_lb": True}, {"yield_lb": "3"}, {"yield_lb": None},
            {"yield_lb": float("nan")}, {"yield_lb": float("inf")},
            {"notes": "n" * 501}, {"notes": 3},
            {"ingredients": "x"}, {"ingredients": ["x"]},
            {"ingredients": [{"name": "", "quantity": 1, "unit": "lb", "cost_per_recipe": 1}]},
            {"ingredients": [{"name": "a", "quantity": 0, "unit": "lb", "cost_per_recipe": 1}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "", "cost_per_recipe": 1}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "u" * 21, "cost_per_recipe": 1}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "lb", "cost_per_recipe": -0.01}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "lb", "cost_per_recipe": False}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "lb", "cost_per_recipe": 1, "store": 7}]},
            {"ingredients": [{"name": "a", "quantity": 1, "unit": "lb", "cost_per_recipe": 1}] * 101},
            {"packaging": [{"name": "f", "quantity_per_unit": 0, "unit_cost": 1}]},
            {"packaging": [{"name": "f", "quantity_per_unit": 1, "unit_cost": -1}]},
            {"packaging": [{"quantity_per_unit": 1, "unit_cost": 1}]},
            {"packaging": [{"name": "f", "quantity_per_unit": 1, "unit_cost": 1}] * 51},
        ]
        for over in bad:
            with self.subTest(over=str(over)[:80]):
                status, _ = self._route("/recipes", "POST", _recipe(**over))
                self.assertEqual(status, 400)
        # NaN/Infinity literales en el JSON crudo
        for lit in ("NaN", "Infinity", "-Infinity"):
            status, _ = self._route("/recipes", "POST", raw='{"name": "a", "yield_lb": %s}' % lit)
            self.assertEqual(status, 400)
        status, _ = self._route("/recipes", "POST", raw="no json")
        self.assertEqual(status, 400)
        status, _ = self._route("/recipes", "POST", raw="[1]")
        self.assertEqual(status, 400)
        self.assertEqual(len(self.table.items), 0)

    def test_limits_accepted(self):
        body = _recipe(ingredients=[{"name": "a", "quantity": 1, "unit": "u" * 20, "cost_per_recipe": 0}] * 100,
                       packaging=[{"name": "f", "quantity_per_unit": 2, "unit_cost": 0}] * 50,
                       name="x" * 100, notes="n" * 500)
        status, rec = self._route("/recipes", "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(len(rec["ingredients"]), 100)
        self.assertEqual(len(rec["packaging"]), 50)

    def test_no_business_403(self):
        status, _ = self._route("/recipes", "GET", user_id="otro")
        self.assertEqual(status, 403)
        status, _ = self._route("/recipes", "POST", _recipe(), user_id=None)
        self.assertEqual(status, 403)

    # ---------- GET ----------
    def test_list_sorted_by_name_and_scoped(self):
        for name in ("zanahoria", "Bizcocho", "arepa"):
            self._route("/recipes", "POST", _recipe(name=name))
        self._route("/recipes", "POST", _recipe(name="Ajena"), user_id="u2")
        status, data = self._route("/recipes", "GET")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in data], ["arepa", "Bizcocho", "zanahoria"])
        self.assertIsInstance(data[0]["yield_lb"], float)
        _, data = self._route("/recipes", "GET", user_id="u2")
        self.assertEqual([r["name"] for r in data], ["Ajena"])

    # ---------- PUT ----------
    def test_update_replaces_fields_keeps_create_date(self):
        _, rec = self._route("/recipes", "POST", _recipe())
        rid = rec["recipe_id"]
        self.table.items[("b1", rid)]["create_date"] = "2026-01-01 00:00:00"
        status, upd = self._route(f"/recipes/{rid}", "PUT",
                                  {"name": "Pan sobao", "yield_lb": 3, "ingredients": [
                                      {"name": "Leche", "quantity": 1, "unit": "lata", "cost_per_recipe": 95}]})
        self.assertEqual(status, 200)
        self.assertEqual(upd["recipe_id"], rid)
        self.assertEqual(upd["name"], "Pan sobao")
        self.assertEqual(upd["yield_lb"], 3)
        self.assertEqual(upd["notes"], "")
        self.assertEqual(upd["packaging"], [])
        self.assertEqual(len(upd["ingredients"]), 1)
        self.assertEqual(upd["create_date"], "2026-01-01 00:00:00")
        self.assertEqual(len(self.table.items), 1)
        _, data = self._route("/recipes", "GET")
        self.assertEqual(data[0]["name"], "Pan sobao")

    def test_update_validation_and_404(self):
        _, rec = self._route("/recipes", "POST", _recipe())
        status, _ = self._route(f"/recipes/{rec['recipe_id']}", "PUT", _recipe(yield_lb=0))
        self.assertEqual(status, 400)
        status, _ = self._route("/recipes/nope", "PUT", _recipe())
        self.assertEqual(status, 404)
        # Receta de otro negocio
        status, _ = self._route(f"/recipes/{rec['recipe_id']}", "PUT", _recipe(name="hack"), user_id="u2")
        self.assertEqual(status, 404)
        self.assertEqual(self.table.items[("b1", rec["recipe_id"])]["name"], "Pan de agua")
        self.assertNotIn(("b2", rec["recipe_id"]), self.table.items)

    # ---------- DELETE ----------
    def test_delete(self):
        _, rec = self._route("/recipes", "POST", _recipe())
        status, _ = self._route(f"/recipes/{rec['recipe_id']}", "DELETE", user_id="u2")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.table.items), 1)
        status, body = self._route(f"/recipes/{rec['recipe_id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body["recipe_id"], rec["recipe_id"])
        self.assertEqual(len(self.table.items), 0)
        status, _ = self._route(f"/recipes/{rec['recipe_id']}", "DELETE")
        self.assertEqual(status, 404)

    def test_unknown_route_404(self):
        status, _ = self._route("/recipes", "PATCH")
        self.assertEqual(status, 404)
        status, _ = self._route("/recipes/a/b", "PUT", _recipe())
        self.assertEqual(status, 404)

    def test_missing_table_503(self):
        err = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Query")

        def boom(**kw):
            raise err

        self.table.query = boom
        status, _ = self._route("/recipes", "GET")
        self.assertEqual(status, 503)


class LambdaRoutingTest(unittest.TestCase):
    def test_recipes_routed_and_requires_subscription(self):
        import types
        noop = lambda *a, **k: None  # noqa: E731
        names = {"contact_team": "contact_team_routes", "payment_methods": "payment_methods_routes",
                 "products": "products_routes", "business": "business_routes", "users": "users_routes",
                 "paddle": "paddle_routes", "categories": "categories_routes", "customers": "customers_routes",
                 "offers": "offers_routes", "delivery_reminder": "run_delivery_reminders",
                 "suggestions": "suggestions_routes", "root": "root_routes", "account_deletion": "WORKER_SOURCE"}
        fakes = {}
        for mod, attr in names.items():
            m = types.ModuleType(mod)
            setattr(m, attr, noop)
            fakes[mod] = m
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop("lambda_function", None)
            import lambda_function as lf
            sys.modules.pop("lambda_function", None)
        self.assertTrue(lf._needs_subscription("/Prod/recipes", "POST"))
        self.assertTrue(lf._needs_subscription("/Prod/recipes/abc", "PUT"))
        self.assertTrue(lf._needs_subscription("/Prod/recipes/abc", "DELETE"))
        self.assertFalse(lf._needs_subscription("/Prod/recipes", "GET"))

        called = {}

        def fake_routes(**kw):
            called.update(kw)
            return {"statusCode": 200}

        ctx = mock.Mock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:f:Prod")
        with mock.patch.object(lf, "recipes_routes", fake_routes):
            lf.lambda_handler({"rawPath": "/Prod/recipes", "headers": {},
                               "requestContext": {"http": {"method": "GET"}}}, ctx)
        self.assertEqual(called.get("path"), "/Prod/recipes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
