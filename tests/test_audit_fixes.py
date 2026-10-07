"""Tests de los arreglos de la auditoría:
  - business.py: low_stock_threshold / itbis_rate respetan 0 (antes `or default` lo convertía).
  - customers.py / offers.py: fechas en hora local de RD (UTC-4), mismo formato "YYYY-MM-DD HH:MM:SS".

Ejecutar:  python3 -m pytest -q tests
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
from test_offers_api import FakeTable  # noqa: E402
import business  # noqa: E402
import customers  # noqa: E402
import offers  # noqa: E402


class FakeScanTable(FakeTable):
    def scan(self, **kw):
        return {"Items": list(self.items.values())}


class NumHelperTests(unittest.TestCase):
    def test_zero_is_kept(self):
        self.assertEqual(business._num(0, 5), 0)
        self.assertEqual(business._num("0", 5), 0)
        self.assertEqual(business._num(Decimal("0"), 5), 0)
        self.assertEqual(business._num(0, 18.0, float), 0.0)

    def test_empty_falls_back_to_default(self):
        for v in (None, "", "   ", "abc"):
            self.assertEqual(business._num(v, 5), 5, v)

    def test_regular_values(self):
        self.assertEqual(business._num("3", 5), 3)
        self.assertEqual(business._num(Decimal("7"), 5), 7)
        self.assertEqual(business._num("16", 18.0, float), 16.0)


class BusinessZeroValuesTests(unittest.TestCase):
    def _existing(self, **extra):
        item = {"business_id": "b1", "user_id": "u1", "business_name": "Biz"}
        item.update(extra)
        return item

    def test_get_business_returns_zero_threshold_and_itbis(self):
        table = FakeScanTable("business_id", [self._existing(
            low_stock_threshold=Decimal("0"), itbis_rate=Decimal("0"))])
        with mock.patch.object(business, "business_table", table), \
                mock.patch.object(business, "_owner_sub_status", return_value="active"):
            resp = business.get_business_by_user_id("u1")
        body = json.loads(resp["body"])
        data = body.get("business", body)
        self.assertEqual(data["low_stock_threshold"], 0)
        self.assertEqual(data["itbis_rate"], 0.0)

    def test_get_business_missing_values_use_defaults(self):
        table = FakeScanTable("business_id", [self._existing()])
        with mock.patch.object(business, "business_table", table), \
                mock.patch.object(business, "_owner_sub_status", return_value="active"):
            resp = business.get_business_by_user_id("u1")
        body = json.loads(resp["body"])
        data = body.get("business", body)
        self.assertEqual(data["low_stock_threshold"], 5)
        self.assertEqual(data["itbis_rate"], 18.0)

    def _update(self, payload):
        table = FakeScanTable("business_id", [self._existing()])
        with mock.patch.object(business, "business_table", table):
            business.update_business({"body": json.dumps(payload)}, "u1", "b1")
        self.assertEqual(len(table.updates), 1)
        return table.updates[0]["ExpressionAttributeValues"]

    def test_update_saves_zero_threshold_and_itbis(self):
        vals = self._update({"low_stock_threshold": 0, "itbis_rate": "0"})
        self.assertEqual(vals[":lst"], 0)
        self.assertEqual(vals[":itr"], Decimal("0"))

    def test_update_empty_values_use_defaults(self):
        vals = self._update({"low_stock_threshold": "", "itbis_rate": None})
        self.assertEqual(vals[":lst"], 5)
        self.assertEqual(vals[":itr"], Decimal("18"))


class StockAlertThresholdTests(unittest.TestCase):
    def _run(self, biz_threshold, old_qty=10, new_qty=3):
        biz = {"business_id": "b1", "user_id": "u1", "name": "Biz"}
        if biz_threshold is not None:
            biz["low_stock_threshold"] = biz_threshold
        with mock.patch.object(customers, "_get_business", return_value=biz), \
                mock.patch.object(customers, "_owner_email", return_value="o@x.com"), \
                mock.patch.object(customers, "low_stock_alert_email") as low:
            customers._check_stock_alerts({"business_id": "b1"}, old_qty, new_qty)
        return low

    def test_business_threshold_zero_disables_alerts(self):
        self.assertFalse(self._run(Decimal("0")).called)

    def test_business_threshold_default_alerts(self):
        self.assertTrue(self._run(None).called)


RD = timezone(timedelta(hours=-4))
FMT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class RdDateTests(unittest.TestCase):
    def _assert_rd_now(self, value):
        self.assertRegex(value, FMT_RE)  # mismo formato que los datos existentes
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        expected = datetime.now(RD).replace(tzinfo=None)
        self.assertLess(abs((expected - parsed).total_seconds()), 5)

    def test_customers_now_is_rd_local_time(self):
        self._assert_rd_now(customers._now())

    def test_offers_now_and_today_are_rd_local(self):
        self._assert_rd_now(offers._now())
        self.assertEqual(offers._today(), datetime.now(RD).strftime("%Y-%m-%d"))

    def test_late_night_utc_still_today_in_rd(self):
        """02:30 UTC del día 8 = 22:30 del día 7 en RD: la orden debe quedar del día 7."""
        fixed = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        with mock.patch.object(customers, "datetime", FakeDT), \
                mock.patch.object(offers, "datetime", FakeDT):
            self.assertEqual(customers._now(), "2026-10-07 22:30:00")
            self.assertEqual(offers._today(), "2026-10-07")


if __name__ == "__main__":
    unittest.main()
