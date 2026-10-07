"""Tests del motor de ofertas v2 (paridad con qatalo-web/src/helpers/offerEngine.js).

Ejecutar:  python3 tests/test_offer_engine.py      (o: python3 -m pytest tests)
Carga tests/fixtures/offerCases.json y, si existe, el canónico de qatalo-web.
"""
import json
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "QATOLO"))

import offer_engine as eng  # noqa: E402

FIXTURES = [
    os.path.join(HERE, "fixtures", "offerCases.json"),
    os.path.normpath(
        os.path.join(HERE, "..", "..", "qatalo-web", "src", "helpers", "__fixtures__", "offerCases.json")
    ),
]


def _close(a, b, tol):
    return abs(Decimal(str(a or 0)) - Decimal(str(b or 0))) <= Decimal(str(tol))


def run_case(c):
    errors = []
    items, exp = c["items"], c["expected"]
    winner, discount = eng.pick_winning_offer(c["offers"], items)
    wid = winner["offer_id"] if winner else None
    if wid != exp["winner_offer_id"]:
        errors.append(f"ganadora {wid} != {exp['winner_offer_id']}")
    if not _close(discount, exp["total_discount"], "0.005"):
        errors.append(f"descuento {discount} != {exp['total_discount']}")
    if winner and eng.calc_discount(winner, items) != discount:
        errors.append("calc_discount != pick_winning_offer")
    lines = eng.distribute_discount(winner, items, discount)
    s = sum((l["discount_amount"] for l in lines), Decimal(0))
    if not _close(s, exp["total_discount"], "0.005"):
        errors.append(f"suma por línea {s} != {exp['total_discount']}")
    if len(lines) != len(items):
        errors.append("número de líneas distinto")
    for i, el in enumerate(exp["lines"]):
        l = lines[i]
        if items[i].get("line_id") != el["line_id"]:
            errors.append(f"orden de línea {items[i].get('line_id')} != {el['line_id']}")
        if not _close(l["discount_amount"], el["discount_amount"], "0.005"):
            errors.append(f"{el['line_id']}.discount_amount {l['discount_amount']} != {el['discount_amount']}")
        if not _close(l["price"], el["price"], "0.01"):
            errors.append(f"{el['line_id']}.price {l['price']} != {el['price']}")
        if l["price"] < 0:
            errors.append(f"{el['line_id']} precio negativo")
        if l["original_price"] != Decimal(str(items[i]["price"])):
            errors.append(f"{el['line_id']}.original_price alterado")
    return errors


class FixtureParityTest(unittest.TestCase):
    def test_fixtures(self):
        loaded = 0
        for path in FIXTURES:
            if not os.path.exists(path):
                continue
            loaded += 1
            with open(path, encoding="utf-8") as f:
                cases = json.load(f)["cases"]
            self.assertTrue(cases, path)
            for c in cases:
                with self.subTest(fixture=os.path.basename(os.path.dirname(os.path.dirname(path))), case=c["name"]):
                    errs = run_case(c)
                    self.assertEqual(errs, [], f"{c['name']}: {errs}")
        self.assertGreaterEqual(loaded, 1, "no se encontró ningún offerCases.json")

    def test_fixture_copy_identical(self):
        a, b = FIXTURES
        if os.path.exists(b):
            with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
                self.assertEqual(json.load(fa), json.load(fb), "tests/fixtures/offerCases.json difiere del canónico web")


class EngineUnitTest(unittest.TestCase):
    def items(self, *rows):
        return [{"product_id": p, "category_id": c, "price": pr, "quantity": q} for p, c, pr, q in rows]

    def test_percentage_residual_sums_to_total(self):
        offer = {"offer_id": "o", "discount_type": "percentage", "discount_value": 10, "applies_to": "all"}
        its = self.items(("a", "", "3.33", 1), ("b", "", "3.33", 1), ("c", "", "3.35", 1))
        lines = eng.compute_line_discounts(offer, its)
        self.assertEqual(sum(lines), eng.round2(Decimal("10.01") * Decimal("0.1")))

    def test_fixed_order_proportional_residual(self):
        offer = {"offer_id": "o", "discount_type": "fixed", "discount_value": 100, "applies_to": "all"}
        its = self.items(("a", "", 100, 1), ("b", "", 100, 1), ("c", "", 100, 1))
        lines = eng.compute_line_discounts(offer, its)
        self.assertEqual(sum(lines), Decimal("100.00"))
        self.assertEqual(lines[0], Decimal("33.34"))  # residuo → primera de mayor subtotal

    def test_bxgy_invalid_config(self):
        offer = {"offer_id": "o", "discount_type": "buy_x_get_y", "buy_quantity": 2, "paid_quantity": 2}
        self.assertEqual(eng.calc_discount(offer, self.items(("a", "", 10, 4))), 0)

    def test_percentage_capped_at_100(self):
        offer = {"offer_id": "o", "discount_type": "percentage", "discount_value": 150}
        d = eng.distribute_discount(offer, self.items(("a", "", 10, 2)))
        self.assertEqual(d[0]["discount_amount"], Decimal("20.00"))
        self.assertEqual(d[0]["price"], Decimal("0"))

    def test_no_offer_keeps_prices(self):
        d = eng.distribute_discount(None, self.items(("a", "", "12.5", 2)))
        self.assertEqual(d[0]["price"], Decimal("12.5"))
        self.assertEqual(d[0]["discount_amount"], 0)

    def test_unknown_scope_no_discount(self):
        offer = {"offer_id": "o", "discount_type": "fixed", "discount_value": 5, "applies_to": "raro"}
        self.assertEqual(eng.calc_discount(offer, self.items(("a", "", 10, 1))), 0)

    def test_unit_price_no_exponent(self):
        offer = {"offer_id": "o", "discount_type": "fixed", "discount_value": 100, "applies_to": "all"}
        d = eng.distribute_discount(offer, self.items(("a", "", 200, 1)))
        self.assertEqual(str(d[0]["price"]), "100.00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
