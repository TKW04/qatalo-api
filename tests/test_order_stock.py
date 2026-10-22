"""Tests del descuento/reposición de inventario en el ciclo de vida de un pedido
(customers.py): aprobar, entregar, cancelar (admin y cliente), reactivar, editar y borrar.

Invariante: el stock de una línea está descontado <=> su status es Aprobada o Entregada.

Ejecutar:  python3 -m pytest -q tests/test_order_stock.py
"""
import copy
import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
from botocore.exceptions import ClientError  # noqa: E402
import customers  # noqa: E402


def _cond_fail():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem"
    )


class FakeProducts:
    """Tabla de productos que evalúa la escritura condicional de _adjust_stock
    como DynamoDB (condición + escritura atómicas por ítem)."""

    def __init__(self, items):
        self.items = {i["product_id"]: copy.deepcopy(i) for i in items}
        self.writes = 0
        self.before_write = None  # hook para simular concurrencia

    def get_item(self, Key):
        it = self.items.get(Key["product_id"])
        return {"Item": copy.deepcopy(it)} if it is not None else {}

    def update_item(self, **kw):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self)
        it = self.items[kw["Key"]["product_id"]]
        vals = kw["ExpressionAttributeValues"]
        cond = kw.get("ConditionExpression", "")
        m = re.match(r"SET #var\[(\d+)\]\.quantity=:q", kw["UpdateExpression"])
        if m:
            target = it["variants"][int(m.group(1))]
            if target.get("variant_id") != vals[":vid"]:
                raise _cond_fail()
        else:
            target = it
        if "attribute_not_exists" in cond:
            if "quantity" in target:
                raise _cond_fail()
        elif target.get("quantity") != vals[":old"]:
            raise _cond_fail()
        target["quantity"] = vals[":q"]
        it["is_available"] = vals[":av"]
        self.writes += 1
        return {}

    def qty(self, pid, vid=None):
        it = self.items[pid]
        if vid:
            return next(v["quantity"] for v in it["variants"] if v["variant_id"] == vid)
        return it["quantity"]


class FakeCustomers:
    def __init__(self, customer):
        self.item = copy.deepcopy(customer)
        self.saves = 0
        self.fail_save = False

    def get_item(self, Key):
        return {"Item": copy.deepcopy(self.item)}

    def update_item(self, **kw):
        if self.fail_save:
            raise Exception("DynamoDB caído")
        self.item["transactions"] = copy.deepcopy(kw["ExpressionAttributeValues"][":t"])
        self.saves += 1
        return {}

    def tx(self, tid):
        return next(t for t in self.item["transactions"] if t["transaction_id"] == tid)


def _line(tid, pid, qty, status="Pendiente de pago", group="g1", variant_id=None, name="Prod"):
    t = {
        "transaction_id": tid,
        "order_group": group,
        "product_id": pid,
        "product_name": name,
        "quantity": qty,
        "price": 100,
        "status": status,
    }
    if variant_id:
        t["variant"] = {"variant_id": variant_id}
    return t


class OrderStockTestBase(unittest.TestCase):
    def setUp(self):
        self.products = FakeProducts([
            {"product_id": "p1", "product_name": "Camisa", "quantity": 10},
            {"product_id": "p2", "product_name": "Gorra", "quantity": 5},
            {"product_id": "pv", "product_name": "Zapato", "variants": [
                {"variant_id": "v1", "quantity": 4},
                {"variant_id": "v2", "quantity": 2},
            ]},
        ])
        patches = [
            mock.patch.object(customers, "products_table", self.products),
            mock.patch.object(customers, "_get_business", return_value={}),
            mock.patch.object(customers, "_owner_email", return_value=""),
            mock.patch.object(customers, "_check_stock_alerts"),
            mock.patch.object(customers, "_order_details_group", return_value={}),
            mock.patch.object(customers, "order_cancel_email",
                              return_value={"statusCode": 200}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def use_customer(self, lines):
        self.customers = FakeCustomers(
            {"customer_id": "c1", "business_id": "b1", "email": "c@x.com", "transactions": lines}
        )
        p = mock.patch.object(customers, "customers_table", self.customers)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def ev(**body):
        body.setdefault("customer_id", "c1")
        return {"body": json.dumps(body)}


class ApproveTests(OrderStockTestBase):
    def test_approve_deducts_once_and_is_idempotent(self):
        self.use_customer([_line("t1", "p1", 3), _line("t2", "pv", 1, variant_id="v1")])
        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 7)
        self.assertEqual(self.products.qty("pv", "v1"), 3)
        self.assertEqual(self.products.qty("pv", "v2"), 2)
        self.assertEqual(self.customers.tx("t1")["status"], "Aprobada")

        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 7)
        self.assertEqual(self.products.qty("pv", "v1"), 3)

    def test_approve_insufficient_stock_rejects_without_changes(self):
        self.use_customer([_line("t1", "p1", 3), _line("t2", "p2", 6)])
        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 400)
        body = json.loads(r["body"])
        self.assertEqual(body["error"], "stock_insuficiente")
        self.assertEqual((body["available"], body["requested"]), (5, 6))
        self.assertEqual(self.products.qty("p1"), 10)
        self.assertEqual(self.products.qty("p2"), 5)
        self.assertEqual(self.products.writes, 0)
        self.assertEqual(self.customers.saves, 0)
        self.assertEqual(self.customers.tx("t1")["status"], "Pendiente de pago")

    def test_insufficient_stock_aggregates_same_product_lines(self):
        self.use_customer([_line("t1", "p2", 3), _line("t2", "p2", 3)])
        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.products.qty("p2"), 5)

    def test_race_between_check_and_write_rolls_back(self):
        """Otro pedido se lleva el stock entre la validación y la escritura condicional."""
        self.use_customer([_line("t1", "p1", 3), _line("t2", "p2", 5)])
        calls = {"n": 0}
        orig = self.products.update_item

        def update(**kw):
            calls["n"] += 1
            if calls["n"] == 2:  # justo antes de descontar p2, alguien vende 1
                self.products.items["p2"]["quantity"] = 4
            return orig(**kw)

        with mock.patch.object(self.products, "update_item", side_effect=update):
            r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.products.qty("p1"), 10)  # revertido
        self.assertEqual(self.products.qty("p2"), 4)
        self.assertEqual(self.customers.saves, 0)

    def test_concurrent_write_is_retried(self):
        self.use_customer([_line("t1", "p1", 3)])
        self.products.before_write = lambda t: t.items["p1"].update(quantity=9)
        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 6)

    def test_save_failure_rolls_back_stock(self):
        self.use_customer([_line("t1", "p1", 3)])
        self.customers.fail_save = True
        r = customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 500)
        self.assertEqual(self.products.qty("p1"), 10)


class DeliveredTests(OrderStockTestBase):
    def test_delivered_after_approve_does_not_deduct_again(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        r = customers.delivered_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 7)
        self.assertEqual(self.customers.tx("t1")["status"], "Entregada")
        customers.delivered_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 7)

    def test_delivered_from_pending_deducts_once(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.delivered_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 7)

    def test_delivered_from_pending_insufficient_stock(self):
        self.use_customer([_line("t1", "p2", 9)])
        r = customers.delivered_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.products.qty("p2"), 5)
        self.assertEqual(self.customers.tx("t1")["status"], "Pendiente de pago")

    def test_approve_after_delivered_does_not_deduct(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.delivered_transaction(self.ev(transaction_id="t1"))
        customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 7)


class CancelTests(OrderStockTestBase):
    def test_cancel_after_approve_restores_exactly(self):
        self.use_customer([_line("t1", "p1", 3), _line("t2", "pv", 2, variant_id="v2")])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("pv", "v2"), 0)
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 10)
        self.assertEqual(self.products.qty("pv", "v2"), 2)
        self.assertEqual(self.customers.tx("t1")["status_before_cancel"], "Aprobada")

    def test_cancel_after_delivered_restores_exactly(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        customers.delivered_transaction(self.ev(transaction_id="t1"))
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 10)

    def test_cancel_twice_is_idempotent(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 10)
        self.assertEqual(self.customers.tx("t1")["status_before_cancel"], "Aprobada")

    def test_cancel_pending_does_not_touch_stock(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.writes, 0)

    def test_cancel_then_reactivate_deducts_again(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        r = customers.reactivate_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 7)
        self.assertEqual(self.customers.tx("t1")["status"], "Aprobada")

    def test_cancel_by_token_saves_status_before_cancel(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.approve_transaction(self.ev(transaction_id="t1"))
        with mock.patch.object(customers, "_auth_customer",
                               side_effect=lambda e: (self.customers.get_item({})["Item"], {}, None)):
            customers.cancel_order_by_token(self.ev(transaction_id="t1"))
            customers.cancel_order_by_token(self.ev(transaction_id="t1"))
        tx = self.customers.tx("t1")
        self.assertEqual(tx["status"], "Cancelada")
        self.assertEqual(tx["status_before_cancel"], "Aprobada")
        self.assertEqual(self.products.qty("p1"), 10)


class EditDeleteTests(OrderStockTestBase):
    def _approved(self, *lines):
        self.use_customer(list(lines))
        customers.approve_transaction(self.ev(transaction_id=lines[0]["transaction_id"]))

    def test_edit_quantity_up_and_down(self):
        self._approved(_line("t1", "p1", 3))
        r = customers.update_transaction(self.ev(transaction_id="t1", quantity=5))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 5)
        customers.update_transaction(self.ev(transaction_id="t1", quantity=1))
        self.assertEqual(self.products.qty("p1"), 9)
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 10)

    def test_edit_quantity_insufficient_rejects(self):
        self._approved(_line("t1", "p2", 2))
        r = customers.update_transaction(self.ev(transaction_id="t1", quantity=9))
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.products.qty("p2"), 3)
        self.assertEqual(self.customers.tx("t1")["quantity"], 2)

    def test_edit_change_product(self):
        self._approved(_line("t1", "p1", 3))
        customers.update_transaction(self.ev(transaction_id="t1", product_id="p2"))
        self.assertEqual(self.products.qty("p1"), 10)
        self.assertEqual(self.products.qty("p2"), 2)

    def test_edit_change_variant(self):
        self._approved(_line("t1", "pv", 2, variant_id="v1"))
        customers.update_transaction(self.ev(transaction_id="t1", variant={"variant_id": "v2"}))
        self.assertEqual(self.products.qty("pv", "v1"), 4)
        self.assertEqual(self.products.qty("pv", "v2"), 0)

    def test_edit_pending_does_not_touch_stock(self):
        self.use_customer([_line("t1", "p1", 3)])
        customers.update_transaction(self.ev(transaction_id="t1", quantity=8))
        self.assertEqual(self.products.writes, 0)

    def test_delete_approved_restores(self):
        self._approved(_line("t1", "p1", 3))
        r = customers.delete_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.products.qty("p1"), 10)
        self.assertEqual(self.customers.item["transactions"], [])

    def test_delete_cancelled_does_not_restore_twice(self):
        self._approved(_line("t1", "p1", 3))
        customers.cancel_transaction(self.ev(transaction_id="t1"))
        customers.delete_transaction(self.ev(transaction_id="t1"))
        self.assertEqual(self.products.qty("p1"), 10)


if __name__ == "__main__":
    unittest.main()
