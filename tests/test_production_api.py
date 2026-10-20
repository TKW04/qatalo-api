"""Tests de production.py (bitácora de producción + reporte fabricados vs vendidos).

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
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from botocore.exceptions import ClientError  # noqa: E402

import production  # noqa: E402


def _cond_fail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")


class FakeTable:
    """Tabla en memoria. key = nombre de PK o tupla (PK, SK)."""

    def __init__(self, key, items=()):
        self.key = key if isinstance(key, tuple) else (key,)
        self.items = {}
        for i in items:
            self.items[self._k(i)] = dict(i)
        self.updates = []

    def _k(self, d):
        return tuple(d[k] for k in self.key)

    def get_item(self, Key):
        it = self.items.get(self._k(Key))
        return {"Item": dict(it)} if it is not None else {}

    def put_item(self, Item):
        self.items[self._k(Item)] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(self._k(Key), None)

    def scan(self, **kw):
        # Los filtros Attr(...) no se evalúan; el código filtra/valida igual.
        return {"Items": [dict(i) for i in self.items.values()]}

    def query(self, **kw):
        # KeyConditionExpression = Key("business_id").eq(x)
        cond = kw["KeyConditionExpression"]
        value = cond._values[1]
        return {"Items": [dict(i) for i in self.items.values() if i.get(self.key[0]) == value]}


class FakeProducts(FakeTable):
    """Simula los dos update_item que usa production.py (ADD / SET quantity=0) con condiciones."""

    def update_item(self, **kw):
        self.updates.append(kw)
        it = self.items.get(self._k(kw["Key"]))
        vals = kw["ExpressionAttributeValues"]
        cond = kw.get("ConditionExpression", "")
        if it is None or ("business_id = :b" in cond and it.get("business_id") != vals[":b"]):
            raise _cond_fail()
        if "quantity >= :q" in cond and int(it.get("quantity", 0)) < vals[":q"]:
            raise _cond_fail()
        expr = kw["UpdateExpression"]
        if expr.startswith("ADD quantity :q"):
            it["quantity"] = int(it.get("quantity", 0)) + vals[":q"]
        elif expr.startswith("ADD quantity :neg"):
            it["quantity"] = int(it.get("quantity", 0)) + vals[":neg"]
        if "quantity = :z" in expr:
            it["quantity"] = vals[":z"]
        if ":av" in vals:
            it["is_available"] = vals[":av"]
        return {"Attributes": {"quantity": Decimal(it["quantity"])}}


BIZ = {"business_id": "b1", "user_id": "u1"}
PRODUCTS = [
    {"product_id": "p1", "business_id": "b1", "user_id": "u1", "product_name": "Pan", "quantity": Decimal(5)},
    {"product_id": "p2", "business_id": "b1", "user_id": "u1", "product_name": "Bizcocho", "quantity": Decimal(0)},
    {"product_id": "px", "business_id": "b2", "user_id": "u2", "product_name": "Ajeno", "quantity": Decimal(9)},
]


class ProductionApiTest(unittest.TestCase):
    def setUp(self):
        self.business = FakeTable("business_id", [BIZ])
        self.products = FakeProducts("product_id", PRODUCTS)
        self.logs = FakeTable(("business_id", "log_id"))
        self.customers = FakeTable("customer_id")
        self.patches = [
            mock.patch.object(production, "business_table", self.business),
            mock.patch.object(production, "products_table", self.products),
            mock.patch.object(production, "production_table", self.logs),
            mock.patch.object(production, "customers_table", self.customers),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _route(self, path, method, body=None, params=None, user_id="u1"):
        event = {"body": json.dumps(body) if body is not None else None, "queryStringParameters": params}
        r = production.production_routes(f"/Prod{path}", method, event, "dueno@x.com", user_id, "Prod")
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "*")
        return r["statusCode"], json.loads(r["body"])

    def _create(self, pid="p1", qty=3, date="2026-10-01", note=None):
        body = {"product_id": pid, "quantity": qty, "date": date}
        if note is not None:
            body["note"] = note
        return self._route("/production", "POST", body)

    # ---------- POST ----------
    def test_create_adds_stock_and_returns_log(self):
        status, log = self._create(qty=4, note="  lote A ")
        self.assertEqual(status, 200)
        for k in ("log_id", "business_id", "product_id", "product_name", "quantity", "date", "note", "create_date"):
            self.assertIn(k, log)
        self.assertEqual(log["business_id"], "b1")
        self.assertEqual(log["product_name"], "Pan")
        self.assertEqual(log["quantity"], 4)
        self.assertEqual(log["note"], "lote A")
        self.assertEqual(self.products.items[("p1",)]["quantity"], 9)
        self.assertEqual(len(self.logs.items), 1)
        upd = self.products.updates[-1]
        self.assertIn("ADD quantity", upd["UpdateExpression"])

    def test_create_marks_available(self):
        self._create(pid="p2", qty=2)
        self.assertEqual(self.products.items[("p2",)]["quantity"], 2)
        self.assertEqual(self.products.items[("p2",)]["is_available"], "available")

    def test_create_validation(self):
        for qty in (0, -1, 1.5, "abc", None, True, "2.5"):
            with self.subTest(qty=qty):
                status, _ = self._create(qty=qty)
                self.assertEqual(status, 400)
        for date in ("2026/10/01", "2026-13-01", "", None, "01-10-2026"):
            with self.subTest(date=date):
                status, _ = self._create(date=date)
                self.assertEqual(status, 400)
        status, _ = self._route("/production", "POST", {"quantity": 1, "date": "2026-10-01"})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.logs.items), 0)
        self.assertEqual(self.products.items[("p1",)]["quantity"], 5)

    def test_create_accepts_integer_string(self):
        status, log = self._create(qty="7")
        self.assertEqual(status, 200)
        self.assertEqual(log["quantity"], 7)

    def test_create_other_business_product_404(self):
        status, _ = self._create(pid="px")
        self.assertEqual(status, 404)
        status, _ = self._create(pid="nope")
        self.assertEqual(status, 404)
        self.assertEqual(self.products.items[("px",)]["quantity"], 9)
        self.assertEqual(len(self.logs.items), 0)

    def test_create_reverts_log_when_conditional_update_fails(self):
        # El producto cambia de negocio entre la lectura y el update.
        orig_get = self.products.get_item
        self.products.get_item = lambda Key: {"Item": dict(PRODUCTS[0])}
        self.products.items[("p1",)]["business_id"] = "b2"
        status, _ = self._create()
        self.products.get_item = orig_get
        self.assertEqual(status, 404)
        self.assertEqual(len(self.logs.items), 0)

    def test_no_business_403(self):
        status, _ = self._route("/production", "GET", user_id="otro")
        self.assertEqual(status, 403)

    # ---------- GET list ----------
    def test_list_filters_and_order(self):
        self._create(pid="p1", qty=1, date="2026-10-01")
        self._create(pid="p2", qty=2, date="2026-10-05")
        self._create(pid="p1", qty=3, date="2026-10-03")
        self._create(pid="p1", qty=4, date="2026-09-20")
        self.logs.put_item({"business_id": "b2", "log_id": "z", "product_id": "px", "quantity": 1,
                            "date": "2026-10-02"})
        status, data = self._route("/production", "GET")
        self.assertEqual(status, 200)
        self.assertEqual([l["date"] for l in data], ["2026-10-05", "2026-10-03", "2026-10-01", "2026-09-20"])
        _, data = self._route("/production", "GET", params={"from": "2026-10-01", "to": "2026-10-03"})
        self.assertEqual([l["quantity"] for l in data], [3, 1])
        _, data = self._route("/production", "GET", params={"product_id": "p2"})
        self.assertEqual([l["product_id"] for l in data], ["p2"])
        status, _ = self._route("/production", "GET", params={"from": "2026-10-05", "to": "2026-10-01"})
        self.assertEqual(status, 400)
        status, _ = self._route("/production", "GET", params={"from": "ayer"})
        self.assertEqual(status, 400)

    # ---------- DELETE ----------
    def test_delete_subtracts_stock(self):
        _, log = self._create(qty=3)
        self.assertEqual(self.products.items[("p1",)]["quantity"], 8)
        status, _ = self._route(f"/production/{log['log_id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(self.products.items[("p1",)]["quantity"], 5)
        self.assertEqual(len(self.logs.items), 0)

    def test_delete_does_not_go_below_zero(self):
        _, log = self._create(pid="p2", qty=5)
        self.products.items[("p2",)]["quantity"] = 2  # se vendió parte
        status, _ = self._route(f"/production/{log['log_id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(self.products.items[("p2",)]["quantity"], 0)
        self.assertEqual(self.products.items[("p2",)]["is_available"], "unavailable")

    def test_delete_exact_to_zero_marks_unavailable(self):
        _, log = self._create(pid="p2", qty=2)
        self._route(f"/production/{log['log_id']}", "DELETE")
        self.assertEqual(self.products.items[("p2",)]["quantity"], 0)
        self.assertEqual(self.products.items[("p2",)]["is_available"], "unavailable")

    def test_delete_other_business_log_404(self):
        self.logs.put_item({"business_id": "b2", "log_id": "z", "product_id": "px", "quantity": 1,
                            "date": "2026-10-02"})
        status, _ = self._route("/production/z", "DELETE")
        self.assertEqual(status, 404)
        self.assertIn(("b2", "z"), self.logs.items)
        self.assertEqual(self.products.items[("px",)]["quantity"], 9)

    def test_delete_when_product_gone(self):
        _, log = self._create(qty=3)
        del self.products.items[("p1",)]
        status, _ = self._route(f"/production/{log['log_id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.logs.items), 0)

    # ---------- Report ----------
    def test_report(self):
        self._create(pid="p1", qty=10, date="2026-10-01")
        self._create(pid="p1", qty=5, date="2026-10-10")
        self._create(pid="p1", qty=99, date="2026-11-01")  # fuera de rango
        self.customers.put_item({"customer_id": "c1", "business_id": "b1", "transactions": [
            {"product_id": "p1", "product_name": "Pan", "quantity": Decimal(3), "create_date": "2026-10-02 10:00:00",
             "status": "Entregada"},
            {"product_id": "p1", "product_name": "Pan", "quantity": Decimal(2), "create_date": "2026-10-31 23:59:00",
             "status": "Cancelada"},
            {"product_id": "p3", "product_name": "Galleta", "quantity": Decimal(4), "create_date": "2026-10-15 08:00:00",
             "status": "Pendiente de pago"},
            {"product_id": "p1", "product_name": "Pan", "quantity": Decimal(50), "create_date": "2026-09-30 23:00:00",
             "status": "Aprobada"},
        ]})
        status, data = self._route("/production/report", "GET", params={"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual(status, 200)
        by_id = {i["product_id"]: i for i in data["items"]}
        self.assertEqual(set(by_id), {"p1", "p3"})
        self.assertEqual(by_id["p1"], {"product_id": "p1", "product_name": "Pan", "produced": 15, "sold": 3,
                                       "difference": 12, "active": True})  # la cancelada (2) no cuenta
        self.assertEqual(by_id["p3"], {"product_id": "p3", "product_name": "Galleta", "produced": 0, "sold": 4,
                                       "difference": -4, "active": False})  # producto inexistente = inactivo
        self.assertEqual(data["totals"], {"produced": 15, "sold": 7, "difference": 8})

    def test_report_year_includes_whole_dec_31(self):
        self._create(pid="p1", qty=7, date="2026-12-31")
        self._create(pid="p1", qty=1, date="2027-01-01")  # fuera
        self.logs.put_item({"business_id": "b1", "log_id": "lx", "product_id": "p1", "product_name": "Pan",
                            "quantity": Decimal(2), "date": "2026-12-31T22:00:00"})
        self.customers.put_item({"customer_id": "c1", "business_id": "b1", "transactions": [
            {"product_id": "p1", "quantity": Decimal(3), "create_date": "2026-12-31 23:59:59", "status": "Entregada"},
            {"product_id": "p1", "quantity": Decimal(4), "create_date": "2026-12-31T23:59:59.999999-04:00",
             "status": "Entregada"},
            {"product_id": "p1", "quantity": Decimal(5), "create_date": "2026-01-01 00:00:00", "status": "Entregada"},
            {"product_id": "p1", "quantity": Decimal(60), "create_date": "2027-01-01 00:00:00", "status": "Entregada"},
            {"product_id": "p1", "quantity": Decimal(70), "create_date": "2025-12-31 23:59:59", "status": "Entregada"},
        ]})
        status, data = self._route("/production/report", "GET", params={"from": "2026-01-01", "to": "2026-12-31"})
        self.assertEqual(status, 200)
        self.assertEqual(data["totals"], {"produced": 9, "sold": 12, "difference": -3})

    def test_report_active_flag(self):
        self._create(pid="p1", qty=2, date="2026-10-01")  # queda available
        self._create(pid="p2", qty=1, date="2026-10-01")
        self.products.items[("p2",)]["is_available"] = "unavailable"
        _, data = self._route("/production/report", "GET", params={"from": "2026-10-01", "to": "2026-10-31"})
        by_id = {i["product_id"]: i for i in data["items"]}
        self.assertTrue(by_id["p1"]["active"])
        self.assertFalse(by_id["p2"]["active"])

    def test_report_active_uses_stored_is_available_values(self):
        # Valores reales que guardan products.py / customers.py: "available" | "unavailable" | ausente.
        self.products.put_item({"product_id": "pa", "business_id": "b1", "product_name": "A", "quantity": Decimal(3),
                                "is_available": "available"})
        self.products.put_item({"product_id": "pu", "business_id": "b1", "product_name": "U", "quantity": Decimal(0),
                                "is_available": "unavailable"})
        self.products.put_item({"product_id": "pn", "business_id": "b1", "product_name": "N", "quantity": Decimal(1)})
        # Solo ventas (sin fabricación) para no alterar is_available vía create_production.
        self.customers.put_item({"customer_id": "c1", "business_id": "b1", "transactions": [
            {"product_id": pid, "quantity": Decimal(1), "create_date": "2026-03-01 10:00:00", "status": "Entregada"}
            for pid in ("pa", "pu", "pn", "gone")
        ]})
        _, data = self._route("/production/report", "GET", params={"from": "2026-01-01", "to": "2026-12-31"})
        by_id = {i["product_id"]: i for i in data["items"]}
        for it in data["items"]:
            self.assertIsInstance(it["active"], bool)
        self.assertEqual({pid: by_id[pid]["active"] for pid in by_id},
                         {"pa": True, "pu": False, "pn": False, "gone": False})
        self.assertEqual(by_id["pa"]["product_name"], "A")

    def test_report_empty(self):
        status, data = self._route("/production/report", "GET", params={"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"items": [], "totals": {"produced": 0, "sold": 0, "difference": 0}})

    def test_unknown_route_404(self):
        status, _ = self._route("/production", "PUT")
        self.assertEqual(status, 404)


class LambdaRoutingTest(unittest.TestCase):
    def test_production_routed_and_requires_subscription(self):
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
        self.assertTrue(lf._needs_subscription("/Prod/production", "POST"))
        self.assertTrue(lf._needs_subscription("/Prod/production/abc", "DELETE"))
        self.assertFalse(lf._needs_subscription("/Prod/production/report", "GET"))

        called = {}

        def fake_routes(**kw):
            called.update(kw)
            return {"statusCode": 200}

        ctx = mock.Mock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:f:Prod")
        with mock.patch.object(lf, "production_routes", fake_routes):
            lf.lambda_handler({"rawPath": "/Prod/production/report", "headers": {},
                               "requestContext": {"http": {"method": "GET"}}}, ctx)
        self.assertEqual(called.get("path"), "/Prod/production/report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
