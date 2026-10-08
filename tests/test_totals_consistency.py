"""Coherencia de totales:
  1) PDF de factura/recibo (invoice_generator.calc_invoice_totals): la presentación
     suma a la vista (Subtotal − Descuento = Subtotal con descuento; desglose fiscal;
     + ITBIS agregado + Delivery = TOTAL) y el TOTAL NO cambia respecto al cálculo previo.
  2) Edición manual de transacciones (customers.update_transaction): si cambia el
     precio, la línea pierde su descuento automático; si no, se conserva.

Ejecutar:  python3 -m pytest -q tests
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
from test_offers_api import FakeTable  # noqa: E402
import customers  # noqa: E402
import invoice_generator  # noqa: E402

# (items, itbis_rate, with_ncf, TOTAL calculado por la versión anterior de
#  calc_invoice_totals, subtotal, descuento mostrado)
CASES = {
    "sin_descuento_included": (
        [{"quantity": 2, "price": "100", "itbis_mode": "included"}],
        18, True, "200.00", "200.00", "0.00"),
    "descuento_included_envio": (
        [{"quantity": 2, "price": "90", "original_price": "100", "discount_amount": "20",
          "itbis_mode": "included", "delivery_price": "150"}],
        18, True, "330.00", "200.00", "20.00"),
    "descuento_added": (
        [{"quantity": 3, "price": "80", "original_price": "100", "discount_amount": "60",
          "itbis_mode": "added"}],
        18, True, "283.20", "300.00", "60.00"),
    "descuento_exempt": (
        [{"quantity": 1, "price": "450", "original_price": "500", "discount_amount": "50",
          "itbis_mode": "exempt"}],
        18, True, "450.00", "500.00", "50.00"),
    "mixto_varias_lineas": (
        [{"quantity": 5, "price": "1300", "original_price": "1500", "discount_amount": "1000",
          "itbis_mode": "included"},
         {"quantity": 1, "price": "300", "itbis_mode": "added", "delivery_price": "200"},
         {"quantity": 2, "price": "33.333333", "original_price": "50", "discount_amount": "33.33",
          "itbis_mode": "exempt"}],
        18, True, "7120.67", "7900.00", "1033.33"),
    "recibo_mixto": (
        [{"quantity": 5, "price": "1300", "original_price": "1500", "discount_amount": "1000",
          "itbis_mode": "included"},
         {"quantity": 3, "price": "33.333333", "original_price": "50", "discount_amount": "50",
          "itbis_mode": "added"}],
        18, False, "6600.00", "7650.00", "1050.00"),
    "antigua_sin_original": (
        [{"quantity": 2, "price": "90", "discount_amount": "20", "itbis_mode": "included",
          "delivery_price": "100"}],
        18, True, "280.00", "200.00", "20.00"),
    "antigua_recibo": (
        [{"quantity": 4, "price": "25.5", "discount_amount": "8", "itbis_mode": "added"}],
        16, False, "102.00", "110.00", "8.00"),
    "original_viejo_sin_descuento": (
        # original_price incoherente (edición manual previa) sin discount_amount: no se inventa descuento
        [{"quantity": 1, "price": "120", "original_price": "100", "discount_amount": "0",
          "itbis_mode": "included"}],
        18, True, "120.00", "120.00", "0.00"),
    "tasa_cero": (
        [{"quantity": 2, "price": "90", "original_price": "100", "discount_amount": "20",
          "itbis_mode": "added"}],
        0, True, "180.00", "200.00", "20.00"),
    "redondeo_centimos": (
        # 3 × 6.67 = 20.01 cobrado; 3 × 10 = 30 de lista; discount_amount guardado = 10.00.
        # El descuento mostrado se deriva (30 − 20.01 = 9.99) para que cuadre a la vista.
        [{"quantity": 3, "price": "6.67", "original_price": "10", "discount_amount": "10",
          "itbis_mode": "exempt"}],
        18, True, "20.01", "30.00", "9.99"),
}


class InvoiceTotalsTest(unittest.TestCase):
    def test_total_unchanged_and_presentation_adds_up(self):
        for name, (items, rate, ncf, total, subtotal, desc) in CASES.items():
            with self.subTest(name):
                ic, t = invoice_generator.calc_invoice_totals(items, rate, ncf)
                self.assertEqual(t["total"], Decimal(total))
                self.assertEqual(t["subtotal"], Decimal(subtotal))
                self.assertEqual(t["descuento_aplicado"], Decimal(desc))
                # Cadena visible del PDF
                self.assertEqual(t["subtotal"] - t["descuento_aplicado"], t["neto"])
                self.assertEqual(t["neto"] + t["itbis_agregado"] + t["delivery"], t["total"])
                # Desglose fiscal del neto
                self.assertEqual(t["sub_gravado"] + t["sub_exento"] + t["itbis_incluido"], t["neto"])
                self.assertEqual(t["itbis_incluido"] + t["itbis_agregado"], t["itbis"])
                # Las líneas suman al subtotal y sus sub-filas de descuento al descuento
                self.assertEqual(sum(i["line_subtotal"] for i in ic), t["subtotal"])
                self.assertEqual(sum(i["line_discount"] for i in ic), t["descuento_aplicado"])
                if not ncf:
                    self.assertEqual(t["itbis"], Decimal("0"))

    def test_fiscal_values_unchanged(self):
        # included 18 %: 180 cobrado → base 152.54 + ITBIS 27.46 (sobre el precio descontado)
        _, t = invoice_generator.calc_invoice_totals(CASES["descuento_included_envio"][0], 18, True)
        self.assertEqual(t["sub_gravado"], Decimal("152.54"))
        self.assertEqual(t["itbis"], Decimal("27.46"))
        self.assertEqual(t["descuento"], Decimal("20.00"))  # dato crudo Σ discount_amount
        _, t = invoice_generator.calc_invoice_totals(CASES["descuento_added"][0], 18, True)
        self.assertEqual(t["sub_gravado"], Decimal("240.00"))
        self.assertEqual(t["itbis_agregado"], Decimal("43.20"))

    def test_line_shows_original_unit_price(self):
        ic, _ = invoice_generator.calc_invoice_totals(CASES["descuento_included_envio"][0], 18, True)
        self.assertEqual(ic[0]["unit_price"], Decimal("100"))
        self.assertEqual(ic[0]["unit_price_net"], Decimal("90"))
        self.assertEqual(ic[0]["line_subtotal"], Decimal("200.00"))
        self.assertEqual(ic[0]["line_discount"], Decimal("20.00"))
        # Orden antigua sin original_price: se reconstruye price + discount/qty
        ic, _ = invoice_generator.calc_invoice_totals(CASES["antigua_sin_original"][0], 18, True)
        self.assertEqual(ic[0]["unit_price"], Decimal("100"))


class ManualEditTest(unittest.TestCase):
    def setUp(self):
        self.customers = FakeTable("customer_id")
        self.patches = [
            mock.patch.object(customers, "customers_table", self.customers),
            mock.patch.object(customers, "_get_business",
                              return_value={"business_id": "b1", "user_id": "u1"}),
            mock.patch.object(customers, "_save_transactions"),
        ]
        for p in self.patches:
            p.start()
        offer = {"offer_id": "o1", "offer_name": "Promo", "offer_code": "ROLLO"}
        # Oferta repartida entre dos líneas de la misma orden
        self.tx1 = dict(transaction_id="t1", order_group="g", product_id="p1", price=Decimal("1300"),
                        original_price=Decimal("1500"), discount_amount=Decimal("1000"), quantity=5,
                        status="Pendiente de pago", **offer)
        self.tx2 = dict(transaction_id="t2", order_group="g", product_id="p2", price=Decimal("250"),
                        original_price=Decimal("300"), discount_amount=Decimal("50"), quantity=1,
                        status="Pendiente de pago", **offer)
        self.customers.items["c1"] = {"customer_id": "c1", "business_id": "b1", "email": "a@b.c",
                                      "transactions": [self.tx1, self.tx2]}

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _update(self, **body):
        body = {"customer_id": "c1", "transaction_id": "t1", **body}
        r = customers.update_transaction({"body": json.dumps(body)}, user_id="u1")
        self.assertEqual(r["statusCode"], 200, r["body"])
        return self.customers.items["c1"]["transactions"]

    def test_price_change_drops_line_discount(self):
        txs = self._update(price=1200, quantity=5)
        t1, t2 = txs
        self.assertEqual(t1["price"], Decimal("1200"))
        self.assertEqual(t1["original_price"], Decimal("1200"))
        self.assertEqual(t1["discount_amount"], Decimal("0"))
        self.assertEqual((t1["offer_id"], t1["offer_name"], t1["offer_code"]), ("", "", ""))
        # La otra línea de la orden conserva su parte del descuento
        self.assertEqual(t2["discount_amount"], Decimal("50"))
        self.assertEqual(t2["offer_id"], "o1")

    def test_same_price_keeps_discount(self):
        t1 = self._update(price=1300, quantity=5, delivery_day="2026-10-10")[0]
        self.assertEqual(t1["original_price"], Decimal("1500"))
        self.assertEqual(t1["discount_amount"], Decimal("1000"))
        self.assertEqual(t1["offer_id"], "o1")

    def test_only_delivery_day_keeps_everything(self):
        # changeDeliveryDay (web/app) no manda price
        t1 = self._update(delivery_day="2026-10-10")[0]
        self.assertEqual(t1["price"], Decimal("1300"))
        self.assertEqual(t1["discount_amount"], Decimal("1000"))
        self.assertEqual(t1["offer_code"], "ROLLO")

    def test_quantity_change_rescales_discount(self):
        t1 = self._update(price=1300, quantity=2)[0]
        self.assertEqual(t1["discount_amount"], Decimal("400.00"))  # (1500 − 1300) × 2
        self.assertEqual(t1["original_price"], Decimal("1500"))
        self.assertEqual(t1["offer_id"], "o1")

    def test_body_cannot_inject_discount(self):
        t1 = self._update(price=1300, quantity=5, original_price=9999, discount_amount=5,
                          offer_id="x", offer_name="x", offer_code="X")[0]
        self.assertEqual(t1["original_price"], Decimal("1500"))
        self.assertEqual(t1["discount_amount"], Decimal("1000"))
        self.assertEqual(t1["offer_id"], "o1")

    def test_line_consistent_for_invoice_after_edit(self):
        txs = self._update(price=1200, quantity=5)
        raw = [{"quantity": t["quantity"], "price": t["price"], "original_price": t["original_price"],
                "discount_amount": t["discount_amount"], "itbis_mode": "included"} for t in txs]
        _, tot = invoice_generator.calc_invoice_totals(raw, 18, True)
        self.assertEqual(tot["descuento_aplicado"], Decimal("50.00"))
        self.assertEqual(tot["subtotal"] - tot["descuento_aplicado"], tot["neto"])
        self.assertEqual(tot["total"], Decimal("6250.00"))


if __name__ == "__main__":
    unittest.main()
