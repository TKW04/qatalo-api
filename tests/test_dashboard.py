"""Tests del dashboard de admin (GET /reports/dashboard en production.py) y de las fechas
reales approved_date / delivered_date que guardan approve/delivered_transaction (customers.py).

Ejecutar:  python3 -m pytest -q tests/test_dashboard.py
No toca AWS: las tablas de DynamoDB se reemplazan por fakes en memoria.
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import customers  # noqa: E402
import production  # noqa: E402
from test_order_stock import OrderStockTestBase, _line  # noqa: E402
from test_production_api import FakeTable  # noqa: E402


class PagedTable(FakeTable):
    """scan paginado de a 1 ítem para verificar que el endpoint sigue LastEvaluatedKey."""

    def scan(self, **kw):
        items = [dict(i) for i in self.items.values()]
        start = kw.get("ExclusiveStartKey", {}).get("i", 0)
        page = {"Items": items[start:start + 1]}
        if start + 1 < len(items):
            page["LastEvaluatedKey"] = {"i": start + 1}
        return page


def _tx(tid, pid, qty, status, create, price=100, currency="DOP", approved=None, label="", group="g"):
    t = {"transaction_id": tid, "order_group": group, "product_id": pid, "product_name": pid.upper(),
         "quantity": Decimal(qty), "price": Decimal(str(price)), "currency": currency,
         "status": status, "create_date": create, "variant_label": label}
    if approved:
        t["approved_date"] = approved
    return t


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.business = FakeTable("business_id", [{"business_id": "b1", "user_id": "u1"}])
        self.logs = FakeTable(("business_id", "log_id"), [
            {"business_id": "b1", "log_id": "l1", "product_id": "p1", "product_name": "Pan",
             "quantity": Decimal(5), "date": "2026-10-01"},
            {"business_id": "b1", "log_id": "l2", "product_id": "p1", "product_name": "Pan",
             "quantity": Decimal(3), "date": "2026-10-10"},
            {"business_id": "b1", "log_id": "l3", "product_id": "p2", "product_name": "Bizcocho",
             "quantity": Decimal(0), "date": "2026-10-05"},
            {"business_id": "b1", "log_id": "l4", "product_id": "p2", "product_name": "Bizcocho",
             "quantity": Decimal(2), "date": "2026-09-30"},
            {"business_id": "b2", "log_id": "l5", "product_id": "px", "product_name": "Ajeno",
             "quantity": Decimal(9), "date": "2026-10-05"},
        ])
        self.customers = PagedTable("customer_id", [
            {"customer_id": "c1", "business_id": "b1", "given_name": "Ana", "family_name": "Paz",
             "transactions": [
                 _tx("t1", "p1", 2, "Aprobada", "2026-09-20 10:00:00", approved="2026-10-02 09:00:00"),
                 _tx("t2", "p1", 1, "Entregada", "2026-10-03 08:00:00"),  # sin approved_date
                 _tx("t3", "p1", 4, "Pago Completado", "2026-10-04 08:00:00", price=10, currency="USD"),
                 _tx("t4", "p1", 7, "Cancelada", "2026-10-04 08:00:00"),
                 _tx("t5", "p2", 3, "Pendiente de pago", "2026-10-31 23:59:59", price="12.5",
                     label="Grande"),
             ]},
            {"customer_id": "c2", "business_id": "b1", "given_name": "Luis", "family_name": "Gil",
             "transactions": [
                 _tx("t6", "p2", 1, "Pendiente de aprobación", "2026-10-01 00:00:00", label="Grande"),
                 _tx("t7", "p2", 5, "Aprobada", "2026-10-15 00:00:00", approved="2026-11-01 00:00:01"),
             ]},
            {"customer_id": "c3", "business_id": "b2", "given_name": "X", "family_name": "Y",
             "transactions": [_tx("t9", "px", 9, "Aprobada", "2026-10-05 00:00:00")]},
        ])
        self.patches = [
            mock.patch.object(production, "business_table", self.business),
            mock.patch.object(production, "production_table", self.logs),
            mock.patch.object(production, "customers_table", self.customers),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self, params=None, user_id="u1", path="/reports/dashboard"):
        event = {"queryStringParameters": params}
        r = production.reports_routes(f"/Prod{path}", "GET", event, "d@x.com", user_id, "Prod")
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "*")
        return r["statusCode"], json.loads(r["body"])

    def test_classification_dates_and_currency(self):
        code, body = self._get({"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual(code, 200)
        self.assertEqual(body["range"], {"from": "2026-10-01", "to": "2026-10-31"})

        # Producidos: solo b1, en rango, quantity > 0
        self.assertEqual(body["produced"], [{"product_id": "p1", "product_name": "Pan", "quantity": 8}])

        # Vendidos: t1 (approved_date en rango aunque create_date no), t2 (respaldo create_date),
        # t3 (Pago Completado). Excluye t4 (Cancelada) y t7 (approved_date fuera de rango).
        sold = body["sold"]
        self.assertEqual(len(sold), 1)
        g = sold[0]
        self.assertEqual((g["product_id"], g["variant_label"], g["quantity"]), ("p1", "", 7))
        self.assertEqual(g["amount"], {"DOP": 300, "USD": 40})
        self.assertEqual({l["transaction_id"] for l in g["lines"]}, {"t1", "t2", "t3"})
        t1 = next(l for l in g["lines"] if l["transaction_id"] == "t1")
        self.assertEqual(t1["date"], "2026-10-02 09:00:00")
        self.assertEqual(t1["customer_name"], "Ana Paz")
        self.assertEqual(set(t1), {"transaction_id", "order_group", "customer_name", "quantity",
                                   "price", "currency", "status", "date"})

        # Promesas: t5 (último día, inclusivo) y t6 (primer día), agrupadas por p2+Grande
        promises = body["promises"]
        self.assertEqual(len(promises), 1)
        p = promises[0]
        self.assertEqual((p["product_id"], p["variant_label"], p["quantity"]), ("p2", "Grande", 4))
        self.assertEqual(p["by_status"], {"Pendiente de pago": 3, "Pendiente de aprobación": 1})
        self.assertEqual(p["amount"], {"DOP": 137.5})

        self.assertEqual(body["totals"], {
            "produced_units": 8, "sold_units": 7, "sold_amount": {"DOP": 300, "USD": 40},
            "promises_units": 4, "promises_amount": {"DOP": 137.5},
        })

    def test_no_range_includes_everything_sorted_desc(self):
        code, body = self._get()
        self.assertEqual(code, 200)
        self.assertEqual(body["range"], {"from": None, "to": None})
        self.assertEqual(body["totals"]["produced_units"], 10)
        self.assertEqual([(g["product_id"], g["quantity"]) for g in body["sold"]], [("p1", 7), ("p2", 5)])
        self.assertNotIn("t9", json.dumps(body))  # otro negocio

    def test_range_boundaries_inclusive(self):
        _, body = self._get({"from": "2026-10-03", "to": "2026-10-03"})
        self.assertEqual([l["transaction_id"] for l in body["sold"][0]["lines"]], ["t2"])
        _, body = self._get({"from": "2026-11-01", "to": "2026-11-01"})
        self.assertEqual(body["sold"][0]["product_id"], "p2")
        self.assertEqual(body["promises"], [])

    def test_invalid_range_400(self):
        for params in ({"from": "2026-13-01"}, {"to": "10/01/2026"},
                       {"from": "2026-10-05", "to": "2026-10-01"}):
            code, _ = self._get(params)
            self.assertEqual(code, 400, params)

    def test_requires_owner(self):
        code, _ = self._get(user_id="otro")
        self.assertEqual(code, 403)
        code, _ = self._get(path="/reports/otra")
        self.assertEqual(code, 404)


class LineDatesTest(OrderStockTestBase):
    def test_approve_sets_approved_date_once(self):
        self.use_customer([_line("t1", "p1", 1), _line("t2", "p2", 1)])
        with mock.patch.object(customers, "_now", return_value="2026-10-01 10:00:00"):
            customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.customers.tx("t1")["approved_date"], "2026-10-01 10:00:00")
        self.assertEqual(self.customers.tx("t2")["approved_date"], "2026-10-01 10:00:00")

        with mock.patch.object(customers, "_now", return_value="2026-10-05 10:00:00"):
            customers.delivered_transaction(self.ev(transaction_id="t1"))
            customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.customers.tx("t1")["approved_date"], "2026-10-01 10:00:00")

    def test_delivered_sets_delivered_date_on_transition_only(self):
        self.use_customer([_line("t1", "p1", 1, status="Aprobada")])
        with mock.patch.object(customers, "_now", return_value="2026-10-02 10:00:00"):
            customers.delivered_transaction(self.ev(transaction_id="t1"))
        tx = self.customers.tx("t1")
        self.assertEqual((tx["status"], tx["delivered_date"]), ("Entregada", "2026-10-02 10:00:00"))
        with mock.patch.object(customers, "_now", return_value="2026-10-09 10:00:00"):
            customers.delivered_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.customers.tx("t1")["delivered_date"], "2026-10-02 10:00:00")
        self.assertNotIn("approved_date", tx)


class RoutingTest(unittest.TestCase):
    def test_dashboard_routed(self):
        import types
        noop = lambda *a, **k: None  # noqa: E731
        names = {"contact_team": "contact_team_routes", "payment_methods": "payment_methods_routes",
                 "products": "products_routes", "business": "business_routes", "users": "users_routes",
                 "paddle": "paddle_routes", "categories": "categories_routes", "customers": "customers_routes",
                 "offers": "offers_routes", "delivery_reminder": "run_delivery_reminders",
                 "suggestions": "suggestions_routes", "root": "root_routes", "account_deletion": "WORKER_SOURCE",
                 "recipes": "recipes_routes"}
        fakes = {}
        for mod, attr in names.items():
            m = types.ModuleType(mod)
            setattr(m, attr, noop)
            fakes[mod] = m
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop("lambda_function", None)
            import lambda_function as lf
            sys.modules.pop("lambda_function", None)
        called = {}

        def fake_routes(**kw):
            called.update(kw)
            return {"statusCode": 200}

        ctx = mock.Mock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:f:Prod")
        with mock.patch.object(lf, "reports_routes", fake_routes):
            lf.lambda_handler({"rawPath": "/Prod/reports/dashboard", "headers": {},
                               "requestContext": {"http": {"method": "GET"}}}, ctx)
        self.assertEqual(called.get("path"), "/Prod/reports/dashboard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
