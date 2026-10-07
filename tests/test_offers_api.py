"""Tests de offers.py (campos v2 / validación) y de la validación de ofertas en customers.py.

Ejecutar:  python3 tests/test_offers_api.py      (o: python3 -m pytest tests)
No toca AWS: las tablas de DynamoDB se reemplazan por fakes en memoria y los
módulos de terceros que no se usan aquí (mailersend, requests_toolbelt...) se simulan.
"""
import json
import os
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "QATOLO"))
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _stub(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Dependencias que viven en la Lambda Layer y no se necesitan para estos tests
try:
    import requests  # noqa: F401
except ImportError:
    _stub("requests")
try:
    import requests_toolbelt.multipart  # noqa: F401
except ImportError:
    _stub("requests_toolbelt")
    _stub("requests_toolbelt.multipart", decoder=mock.MagicMock())
_noop = lambda *a, **k: None  # noqa: E731
_stub(
    "SendMails.mails",
    **{n: _noop for n in (
        "new_order_create_email order_cancel_email order_create_email order_delivered_email "
        "order_receipt_email order_verified_email order_access_code_email low_stock_alert_email "
        "out_of_stock_alert_email invoice_email"
    ).split()},
)
_stub("SendMails")
try:
    import fpdf  # noqa: F401
except ImportError:
    _stub("fpdf", FPDF=object)

import offers  # noqa: E402
import customers  # noqa: E402


class FakeTable:
    def __init__(self, key, items=()):
        self.key = key
        self.items = {i[key]: dict(i) for i in items}
        self.updates = []

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {"Item": it} if it is not None else {}

    def put_item(self, Item):
        self.items[Item[self.key]] = Item

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def scan(self, **kw):
        # Filtro mínimo: business_id/code de Attr(...).eq(...) no es evaluable aquí;
        # devolvemos todo y que el llamador filtre (suficiente para estos tests).
        return {"Items": list(self.items.values())}


# ───────── offers.py ─────────
class OffersModuleTest(unittest.TestCase):
    def test_map_legacy_defaults(self):
        m = offers._map({"offer_id": "x", "discount_type": "fixed", "discount_value": Decimal("50")})
        self.assertEqual(m["fixed_mode"], "order")
        self.assertEqual(m["min_quantity"], 0)
        self.assertEqual(m["applies_to"], "all")

    def test_map_v2_fields(self):
        m = offers._map({"offer_id": "x", "fixed_mode": "per_unit", "min_quantity": Decimal("5")})
        self.assertEqual(m["fixed_mode"], "per_unit")
        self.assertEqual(m["min_quantity"], 5)

    def test_build_item_v2(self):
        it = offers._build_item(
            {"discount_type": "fixed", "discount_value": 200, "fixed_mode": "per_unit", "min_quantity": "5"}, "b1"
        )
        self.assertEqual(it["fixed_mode"], "per_unit")
        self.assertEqual(it["min_quantity"], 5)
        self.assertEqual(it["discount_value"], Decimal("200"))

    def test_build_item_defaults(self):
        it = offers._build_item({"discount_type": "percentage", "discount_value": 10}, "b1")
        self.assertEqual(it["fixed_mode"], "order")
        self.assertEqual(it["min_quantity"], 0)

    def test_validation_errors(self):
        bad = [
            {"fixed_mode": "raro"},
            {"min_quantity": -1},
            {"min_quantity": "2.5"},
            {"min_quantity": "abc"},
            {"discount_type": "percentage", "discount_value": 150},
            {"discount_type": "otro"},
            {"applies_to": "x"},
            {"discount_value": -5},
            {"discount_type": "buy_x_get_y", "buy_quantity": 2, "paid_quantity": 2},
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(offers.OfferValidationError):
                    offers._build_item(data, "b1")

    def test_create_returns_400_on_invalid(self):
        table = FakeTable("offer_id")
        with mock.patch.object(offers, "_get_biz", return_value={"business_id": "b1"}), \
                mock.patch.object(offers, "offers_table", table):
            r = offers.create_offer({"body": json.dumps({"fixed_mode": "nope"})}, "u1")
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(table.items, {})

    def test_update_includes_new_fields(self):
        table = FakeTable("offer_id", [{"offer_id": "o1", "business_id": "b1"}])
        body = {"discount_type": "fixed", "discount_value": 100, "fixed_mode": "order", "min_quantity": 5}
        with mock.patch.object(offers, "_get_biz", return_value={"business_id": "b1"}), \
                mock.patch.object(offers, "offers_table", table):
            r = offers.update_offer({"body": json.dumps(body)}, "o1", "u1")
        self.assertEqual(r["statusCode"], 200)
        upd = table.updates[0]
        self.assertIn("fixed_mode=:fm", upd["UpdateExpression"])
        self.assertIn("min_quantity=:mq", upd["UpdateExpression"])
        self.assertEqual(upd["ExpressionAttributeValues"][":mq"], 5)


# ───────── offer_pricing (precios de variantes / monedas) ─────────
import offer_pricing as op  # noqa: E402


class CatalogPriceTest(unittest.TestCase):
    base = {"product_id": "p", "price": Decimal("100"), "currency": "DOP"}

    def test_base(self):
        self.assertEqual(op.catalog_unit_price(self.base, {"currency": "DOP"}), (Decimal("100"), None))

    def test_clothing_extra_price(self):
        p = {**self.base, "is_customizable": True, "variant_type": "clothing",
             "variants": [{"variant_id": "v1", "extra_price": Decimal("25")}]}
        self.assertEqual(op.catalog_unit_price(p, {"variant": {"variant_id": "v1"}})[0], Decimal("125"))

    def test_size_direct_price(self):
        p = {**self.base, "is_customizable": True, "variant_type": "size",
             "variants": [{"variant_id": "s1", "price": Decimal("300")}]}
        self.assertEqual(op.catalog_unit_price(p, {"variant": {"variant_id": "s1"}})[0], Decimal("300"))

    def test_unknown_variant_is_unverifiable(self):
        p = {**self.base, "is_customizable": True, "variants": [{"variant_id": "v1"}]}
        self.assertEqual(op.catalog_unit_price(p, {"variant_id": "zz"}), (None, "variante_no_encontrada"))

    def test_alt_currency(self):
        p = {**self.base, "alt_prices": [{"currency": "USD", "price": "2.5"}]}
        self.assertEqual(op.catalog_unit_price(p, {"currency": "USD"})[0], Decimal("2.5"))
        self.assertEqual(op.catalog_unit_price(p, {"currency": "EUR"}), (None, "moneda_no_configurada"))

    def test_offer_current(self):
        o = {"business_id": "b1", "is_active": True, "valid_from": "2026-01-01", "valid_until": "2026-12-31",
             "max_uses": 3, "uses_count": 1}
        self.assertIsNone(op.offer_is_current(o, "b1", "2026-06-01"))
        self.assertEqual(op.offer_is_current(o, "b2", "2026-06-01"), "oferta_de_otro_negocio")
        self.assertEqual(op.offer_is_current({**o, "is_active": False}, "b1", "2026-06-01"), "oferta_inactiva")
        self.assertEqual(op.offer_is_current(o, "b1", "2027-01-01"), "oferta_vencida")
        self.assertEqual(op.offer_is_current(o, "b1", "2025-01-01"), "oferta_aun_no_vigente")
        self.assertEqual(op.offer_is_current({**o, "uses_count": 3}, "b1", "2026-06-01"),
                         "oferta_sin_usos_disponibles")


# ───────── customers.py: checkouts y admin ─────────
PRODUCTS = [
    {"product_id": "rollo", "business_id": "b1", "price": Decimal("1500"), "currency": "DOP",
     "category_id": "c1", "quantity": 100, "is_available": "available"},
    {"product_id": "cinta", "business_id": "b1", "price": Decimal("300"), "currency": "DOP",
     "category_id": "c2", "quantity": 100, "is_available": "available"},
]
OFFER = {"offer_id": "o1", "business_id": "b1", "name": "200 por rollo", "is_active": True,
         "trigger": "code", "code": "ROLLO", "discount_type": "fixed", "fixed_mode": "per_unit",
         "discount_value": Decimal("200"), "applies_to": "products", "product_ids": ["rollo"],
         "uses_count": 0, "max_uses": None, "valid_from": "", "valid_until": "", "priority": "media"}


class CustomersOfferTest(unittest.TestCase):
    def setUp(self):
        self.products = FakeTable("product_id", PRODUCTS)
        self.offers = FakeTable("offer_id", [OFFER])
        self.customers = FakeTable("customer_id")
        self.inc = mock.MagicMock(return_value=True)  # reserve_offer_use
        self.release = mock.MagicMock()
        self.patches = [
            mock.patch.object(customers, "products_table", self.products),
            mock.patch.object(customers, "offers_table", self.offers),
            mock.patch.object(customers, "customers_table", self.customers),
            mock.patch.object(customers, "payment_methods_table", FakeTable("payment_method_id")),
            mock.patch.object(customers, "reserve_offer_use", self.inc),
            mock.patch.object(customers, "release_offer_use", self.release),
            mock.patch.object(customers, "_get_business", return_value={"business_id": "b1", "user_id": "u1"}),
            mock.patch.object(customers, "_customer_magic_link", return_value="x"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _guest(self, **over):
        body = {
            "business_id": "b1", "email": "a@b.c", "offer_id": "o1", "offer_name": "x", "offer_code": "ROLLO",
            "items": [
                # el cliente manda precios manipulados: deben ignorarse
                {"product_id": "rollo", "price": 1, "original_price": 1, "discount_amount": 9999, "quantity": 5,
                 "currency": "DOP"},
                {"product_id": "cinta", "price": 300, "quantity": 1, "currency": "DOP"},
            ],
        }
        body.update(over)
        r = customers.create_customer_cart({"body": json.dumps(body)})
        self.assertEqual(r["statusCode"], 200, r["body"])
        cust = next(iter(self.customers.items.values()))
        return json.loads(r["body"]), cust["transactions"]

    def test_guest_server_prices_and_increments_uses(self):
        resp, txs = self._guest()
        self.assertEqual(set(resp), {"message", "customer_id", "order_group"})
        self.assertEqual(txs[0]["original_price"], Decimal("1500"))
        self.assertEqual(txs[0]["discount_amount"], Decimal("1000.00"))
        self.assertEqual(txs[0]["price"], Decimal("1300.00"))
        self.assertEqual(txs[0]["offer_id"], "o1")
        self.assertEqual(txs[0]["offer_name"], "200 por rollo")
        self.assertEqual(txs[1]["discount_amount"], 0)
        self.inc.assert_called_once_with("o1")

    def test_guest_invalid_offer_creates_order_without_discount(self):
        self.offers.items["o1"]["is_active"] = False
        _, txs = self._guest()
        self.assertEqual(txs[0]["price"], Decimal("1500"))
        self.assertEqual(txs[0]["discount_amount"], 0)
        self.assertEqual(txs[0]["offer_id"], "")
        self.inc.assert_not_called()

    def test_guest_wrong_code(self):
        _, txs = self._guest(offer_code="OTRO")
        self.assertEqual(txs[0]["offer_id"], "")
        self.inc.assert_not_called()

    def test_guest_other_business_offer(self):
        self.offers.items["o1"]["business_id"] = "b2"
        _, txs = self._guest()
        self.assertEqual(txs[0]["discount_amount"], 0)

    def test_unverifiable_price_disables_discount(self):
        self.products.items["rollo"]["alt_prices"] = [{"currency": "USD", "price": "25"}]
        items = [{"product_id": "rollo", "price": 30, "quantity": 5, "currency": "EUR"}]
        _, txs = self._guest(items=items)
        self.assertEqual(txs[0]["price"], Decimal("30"))  # precio del cliente (no verificable)
        self.assertEqual(txs[0]["discount_amount"], 0)
        self.inc.assert_not_called()

    def test_token_checkout(self):
        cust = {"customer_id": "c1", "business_id": "b1", "email": "a@b.c", "transactions": []}
        self.customers.items["c1"] = cust
        body = {"offer_id": "o1", "offer_code": "ROLLO",
                "items": [{"product_id": "rollo", "price": 1500, "quantity": 2, "currency": "DOP"}]}
        with mock.patch.object(customers, "_auth_customer", return_value=(cust, {"business_id": "b1"}, None)), \
                mock.patch.object(customers, "_save_transactions") as save:
            r = customers.checkout_cart_by_token({"body": json.dumps(body)})
        self.assertEqual(r["statusCode"], 200, r["body"])
        self.assertEqual(set(json.loads(r["body"])), {"message", "order_group"})
        tx = save.call_args[0][1][0]
        self.assertEqual(tx["discount_amount"], Decimal("400.00"))
        self.inc.assert_called_once_with("o1")

    def _admin(self, offer_id, items_payload=None):
        tx1 = {"transaction_id": "t1", "order_group": "g", "product_id": "rollo", "price": Decimal("1500"),
               "quantity": 5, "status": "Pendiente de pago"}
        tx2 = {"transaction_id": "t2", "order_group": "g", "product_id": "cinta", "price": Decimal("300"),
               "quantity": 1, "status": "Pendiente de pago"}
        cust = {"customer_id": "c1", "business_id": "b1", "email": "a@b.c", "transactions": [tx1, tx2]}
        self.customers.items["c1"] = cust
        body = {"customer_id": "c1", "transaction_id": "t1", "offer_id": offer_id, "offer_code": "",
                "items": items_payload or []}
        with mock.patch.object(customers, "_save_transactions") as save:
            r = customers.apply_offer_to_order({"body": json.dumps(body)}, user_id="u1")
        return r, cust["transactions"], save

    def test_admin_apply_and_remove(self):
        r, txs, _ = self._admin("o1", [{"transaction_id": "t1", "price": 0, "discount_amount": 7500}])
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["offer_id"], "o1")
        self.assertEqual(txs[0]["discount_amount"], Decimal("1000.00"))
        self.assertEqual(txs[0]["original_price"], Decimal("1500"))
        r, txs, _ = self._admin("")
        self.assertEqual(r["statusCode"], 200)

    def test_admin_invalid_offer_400(self):
        self.offers.items["o1"]["valid_until"] = "2000-01-01"
        r, txs, save = self._admin("o1")
        self.assertEqual(r["statusCode"], 400)
        save.assert_not_called()


# ───────── Hueco 2: reserva atómica de usos (max_uses) ─────────
from botocore.exceptions import ClientError  # noqa: E402


class ConditionalOffersTable(FakeTable):
    """FakeTable que evalúa las dos condiciones que usa offers.py como lo haría DynamoDB
    (la evaluación + escritura es atómica por ítem) y aplica `ADD uses_count`."""

    def update_item(self, **kw):
        self.updates.append(kw)
        it = self.items.get(kw["Key"][self.key])
        cond = kw.get("ConditionExpression", "")
        vals = kw.get("ExpressionAttributeValues", {})
        if cond == offers._RESERVE_CONDITION:
            mu = it.get("max_uses") if it else None
            ok = it is not None and (
                "max_uses" not in it or mu is None or mu == 0
                or ("uses_count" in it and it["uses_count"] < mu)
                or ("uses_count" not in it and mu > 0)
            )
        elif cond:
            ok = it is not None and it.get("uses_count", 0) > 0
        else:
            ok = True
        if not ok:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")
        if kw["UpdateExpression"].startswith("ADD uses_count"):
            delta = vals.get(":one", vals.get(":minus", 0))
            it["uses_count"] = it.get("uses_count", 0) + delta
        return {}


class ReserveOfferUseTest(unittest.TestCase):
    def _table(self, **offer):
        t = ConditionalOffersTable("offer_id", [{"offer_id": "o1", "business_id": "b1", **offer}])
        return t

    def test_expression_is_conditional_add(self):
        t = self._table(max_uses=1, uses_count=0)
        with mock.patch.object(offers, "offers_table", t):
            self.assertTrue(offers.reserve_offer_use("o1"))
        kw = t.updates[0]
        self.assertEqual(kw["UpdateExpression"], "ADD uses_count :one")
        for frag in ("attribute_not_exists(max_uses)", "max_uses = :zero", "uses_count < max_uses",
                     "attribute_exists(offer_id)"):
            self.assertIn(frag, kw["ConditionExpression"])

    def test_last_use_only_once(self):
        t = self._table(max_uses=2, uses_count=1)
        with mock.patch.object(offers, "offers_table", t):
            self.assertTrue(offers.reserve_offer_use("o1"))
            self.assertFalse(offers.reserve_offer_use("o1"))  # la 2ª orden "concurrente" no consigue uso
        self.assertEqual(t.items["o1"]["uses_count"], 2)

    def test_unlimited_and_legacy(self):
        for offer in ({"max_uses": None, "uses_count": 7}, {}, {"max_uses": 0, "uses_count": 3},
                      {"max_uses": 1}):
            with self.subTest(offer=offer):
                t = self._table(**offer)
                with mock.patch.object(offers, "offers_table", t):
                    self.assertTrue(offers.reserve_offer_use("o1"))

    def test_missing_offer_and_release(self):
        t = self._table(max_uses=1, uses_count=1)
        with mock.patch.object(offers, "offers_table", t):
            self.assertFalse(offers.reserve_offer_use("zz"))
            self.assertNotIn("zz", t.items)
            offers.release_offer_use("o1")
            self.assertEqual(t.items["o1"]["uses_count"], 0)
            offers.release_offer_use("o1")  # nunca baja de 0
            self.assertEqual(t.items["o1"]["uses_count"], 0)


class CheckoutReservationTest(CustomersOfferTest):
    """Checkouts usando la reserva real contra ConditionalOffersTable."""

    def setUp(self):
        super().setUp()
        self.offers = ConditionalOffersTable("offer_id", [dict(OFFER, max_uses=1, uses_count=0)])
        for p in self.patches:
            p.stop()
        self.patches = [p for p in self.patches if p.attribute not in ("offers_table", "reserve_offer_use",
                                                                         "release_offer_use")]
        self.patches += [
            mock.patch.object(customers, "offers_table", self.offers),
            mock.patch.object(offers, "offers_table", self.offers),
        ]
        for p in self.patches:
            p.start()

    # Los tests heredados usan mocks de reserva; aquí solo los específicos.
    test_guest_server_prices_and_increments_uses = None
    test_guest_invalid_offer_creates_order_without_discount = None
    test_guest_wrong_code = None
    test_guest_other_business_offer = None
    test_unverifiable_price_disables_discount = None
    test_token_checkout = None
    test_admin_apply_and_remove = None
    test_admin_invalid_offer_400 = None

    def test_race_lost_creates_order_without_discount(self):
        # La validación ve 0/1 usos, pero otra orden reserva el último uso antes que esta.
        real = customers.reserve_offer_use

        def racing(oid):
            self.offers.items["o1"]["uses_count"] = 1
            return real(oid)
        with mock.patch.object(customers, "reserve_offer_use", side_effect=racing):
            _, txs = self._guest()
        self.assertEqual(txs[0]["offer_id"], "")
        self.assertEqual(txs[0]["discount_amount"], 0)
        self.assertEqual(txs[0]["price"], Decimal("1500"))
        self.assertEqual(self.offers.items["o1"]["uses_count"], 1)

    def test_success_consumes_use(self):
        _, txs = self._guest()
        self.assertEqual(txs[0]["offer_id"], "o1")
        self.assertEqual(self.offers.items["o1"]["uses_count"], 1)
        # segunda orden: agotada → sin descuento, sin sobrepasar max_uses
        self.customers.items.clear()
        _, txs = self._guest()
        self.assertEqual(txs[0]["offer_id"], "")
        self.assertEqual(self.offers.items["o1"]["uses_count"], 1)

    def test_write_failure_releases_use(self):
        cust = {"customer_id": "c1", "business_id": "b1", "email": "a@b.c", "transactions": []}
        body = {"offer_id": "o1", "offer_code": "ROLLO",
                "items": [{"product_id": "rollo", "price": 1500, "quantity": 2, "currency": "DOP"}]}
        with mock.patch.object(customers, "_auth_customer", return_value=(cust, {"business_id": "b1"}, None)), \
                mock.patch.object(customers, "_save_transactions", side_effect=RuntimeError("ddb down")):
            r = customers.checkout_cart_by_token({"body": json.dumps(body)})
        self.assertEqual(r["statusCode"], 500)
        self.assertEqual(self.offers.items["o1"]["uses_count"], 0)

    def test_no_offer_no_reservation(self):
        _, txs = self._guest(offer_id="", offer_code="")
        self.assertEqual(self.offers.updates, [])


# ───────── Hueco 1: add_transaction_by_token ─────────
class AddTransactionByTokenTest(CustomersOfferTest):
    def _add(self, **over):
        cust = {"customer_id": "c1", "business_id": "b1", "email": "a@b.c", "transactions": []}
        tr = {"product_id": "rollo", "product_name": "Rollo", "quantity": 5, "currency": "DOP",
              "price": 1, "original_price": 1, "discount_amount": 9999,
              "offer_id": "o1", "offer_name": "x", "offer_code": "ROLLO"}
        tr.update(over)
        with mock.patch.object(customers, "_auth_customer", return_value=(cust, {"business_id": "b1"}, None)), \
                mock.patch.object(customers, "_save_transactions") as save:
            r = customers.add_transaction_by_token({"body": json.dumps({"transaction": tr})})
        self.assertEqual(r["statusCode"], 200, r["body"])
        self.assertEqual(set(json.loads(r["body"])), {"message", "transaction_id"})
        return save.call_args[0][1][-1]

    def test_server_price_and_discount(self):
        tx = self._add()
        self.assertEqual(tx["original_price"], Decimal("1500"))
        self.assertEqual(tx["discount_amount"], Decimal("1000.00"))
        self.assertEqual(tx["price"], Decimal("1300.00"))
        self.assertEqual(tx["offer_name"], "200 por rollo")
        self.inc.assert_called_once_with("o1")

    def test_no_offer_ignores_client_discount(self):
        tx = self._add(offer_id="", offer_code="")
        self.assertEqual(tx["price"], Decimal("1500"))
        self.assertEqual(tx["discount_amount"], 0)
        self.assertEqual(tx["offer_id"], "")
        self.inc.assert_not_called()

    def test_wrong_code_no_discount_no_use(self):
        tx = self._add(offer_code="OTRO")
        self.assertEqual(tx["discount_amount"], 0)
        self.inc.assert_not_called()

    def test_unverifiable_price_keeps_client_price_without_discount(self):
        tx = self._add(product_id="no-existe", price=50, original_price=50)
        self.assertEqual(tx["price"], Decimal("50"))
        self.assertEqual(tx["discount_amount"], 0)
        self.inc.assert_not_called()

    def test_reservation_failed_strips_discount(self):
        self.inc.return_value = False
        tx = self._add()
        self.assertEqual(tx["price"], Decimal("1500"))
        self.assertEqual(tx["discount_amount"], 0)
        self.assertEqual(tx["offer_id"], "")

    # no repetir los heredados
    test_guest_server_prices_and_increments_uses = None
    test_guest_invalid_offer_creates_order_without_discount = None
    test_guest_wrong_code = None
    test_guest_other_business_offer = None
    test_unverifiable_price_disables_discount = None
    test_token_checkout = None
    test_admin_apply_and_remove = None
    test_admin_invalid_offer_400 = None


# ───────── Hueco 3: endpoint público sin códigos + validate-code ─────────
AUTO = {"offer_id": "a1", "business_id": "b1", "name": "Auto 10%", "is_active": True, "trigger": "automatic",
        "code": "", "discount_type": "percentage", "discount_value": Decimal("10"), "applies_to": "all",
        "uses_count": 0, "max_uses": None, "valid_from": "", "valid_until": "", "priority": "baja"}
LEGACY = {"offer_id": "l1", "business_id": "b1", "name": "Legacy", "is_active": True, "code": "VIEJO",
          "discount_type": "percentage", "discount_value": Decimal("5")}  # sin trigger → 'code'


class PublicOffersTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("offer_id", [OFFER, AUTO, LEGACY,
                                            dict(OFFER, offer_id="o2", code="VENCIDO", valid_until="2000-01-01"),
                                            dict(OFFER, offer_id="o3", code="AGOTADO", max_uses=2, uses_count=2),
                                            dict(OFFER, offer_id="o4", code="OTRONEG", business_id="b2"),
                                            dict(OFFER, offer_id="o5", code="APAGADO", is_active=False)])
        self.p = mock.patch.object(offers, "offers_table", self.table)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _route(self, path, method, body=None):
        return offers.offers_routes(f"/Prod{path}", method, {"body": json.dumps(body) if body is not None else None},
                                    None, "Prod")

    def test_public_list_hides_code_offers(self):
        r = self._route("/offers/public/b1", "GET")
        data = json.loads(r["body"])
        self.assertEqual([o["offer_id"] for o in data], ["a1"])
        for o in data:
            self.assertNotIn("code", o)
            self.assertNotIn("uses_count", o)
            self.assertNotIn("max_uses", o)

    def _validate(self, code, biz="b1"):
        r = self._route(f"/offers/public/{biz}/validate-code", "POST", {"code": code, "items": []})
        self.assertEqual(r["statusCode"], 200)
        return json.loads(r["body"])

    def test_validate_ok(self):
        d = self._validate(" rollo ")
        self.assertTrue(d["valid"])
        o = d["offer"]
        self.assertEqual(o["offer_id"], "o1")
        self.assertEqual(o["code"], "ROLLO")
        self.assertEqual(o["fixed_mode"], "per_unit")
        self.assertEqual(o["product_ids"], ["rollo"])
        for k in ("uses_count", "max_uses", "business_id", "is_active"):
            self.assertNotIn(k, o)

    def test_legacy_code_offer(self):
        self.assertTrue(self._validate("VIEJO")["valid"])

    def test_generic_response_for_invalid(self):
        generic = self._validate("NOEXISTE")
        self.assertFalse(generic["valid"])
        for code, biz in (("VENCIDO", "b1"), ("AGOTADO", "b1"), ("OTRONEG", "b1"), ("APAGADO", "b1"),
                          ("ROLLO", "b2"), ("", "b1"), ("X" * 200, "b1")):
            with self.subTest(code=code[:10], biz=biz):
                self.assertEqual(self._validate(code, biz), generic)

    def test_automatic_offer_not_returned_by_code(self):
        self.table.items["a1"]["code"] = "AUTO"
        self.assertFalse(self._validate("AUTO")["valid"])

    def test_bad_body(self):
        r = offers.validate_offer_code({"body": "{no json"}, "b1")
        self.assertEqual(json.loads(r["body"])["valid"], False)
        r = offers.validate_offer_code({"body": json.dumps({"code": 123})}, "b1")
        self.assertEqual(json.loads(r["body"])["valid"], False)

    def test_get_route_unchanged(self):
        self.assertEqual(self._route("/offers/public/b1/validate-code", "GET")["statusCode"], 404)


class LambdaGuardTest(unittest.TestCase):
    def test_validate_code_not_blocked_by_subscription(self):
        # Los routers que no se prueban aquí dependen de la layer: se simulan solo para importar.
        names = {"contact_team": "contact_team_routes", "payment_methods": "payment_methods_routes",
                 "products": "products_routes", "business": "business_routes", "users": "users_routes",
                 "paddle": "paddle_routes", "categories": "categories_routes",
                 "delivery_reminder": "run_delivery_reminders", "suggestions": "suggestions_routes",
                 "root": "root_routes"}
        fakes = {}
        for mod, attr in names.items():
            m = types.ModuleType(mod)
            setattr(m, attr, _noop)
            fakes[mod] = m
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop("lambda_function", None)
            import lambda_function as lf
            sys.modules.pop("lambda_function", None)
        self.assertFalse(lf._needs_subscription("/Prod/offers/public/b1/validate-code", "POST"))
        self.assertFalse(lf._needs_subscription("/Prod/offers/public/b1", "GET"))
        self.assertTrue(lf._needs_subscription("/Prod/offers", "POST"))
        self.assertTrue(lf._needs_subscription("/Prod/offers/o1", "PUT"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
