"""Tests de GET /{alias}/app/config (actualización obligatoria de la app móvil).

No toca AWS: el cliente SSM es un fake en memoria.
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_revenuecat  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import app_config  # noqa: E402
import lambda_function  # noqa: E402

CTX = SimpleNamespace(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:qatalo:Prod")


class FakeSSM:
    def __init__(self, params=None, error=None):
        self.params = params or {}
        self.error = error
        self.calls = 0

    def get_parameters(self, Names):
        self.calls += 1
        if self.error:
            raise self.error
        return {
            "Parameters": [{"Name": n, "Value": self.params[n]} for n in Names if n in self.params],
            "InvalidParameters": [n for n in Names if n not in self.params],
        }


class AppConfigTests(unittest.TestCase):
    def setUp(self):
        app_config._cache.update({"at": 0.0, "values": None})
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("APP_IOS_MIN_BUILD", None)
        os.environ.pop("APP_IOS_LATEST_BUILD", None)

    def tearDown(self):
        self.env.stop()
        app_config._cache.update({"at": 0.0, "values": None})

    def _with_ssm(self, ssm):
        p = mock.patch.object(app_config, "_ssm", ssm)
        p.start()
        self.addCleanup(p.stop)
        return ssm

    def _call(self, method="GET", path="/Prod/app/config", headers=None):
        event = {"headers": headers or {}, "rawPath": path, "requestContext": {"http": {"method": method}}}
        return lambda_function.lambda_handler(event, CTX)

    def test_returns_values_from_ssm(self):
        self._with_ssm(FakeSSM({app_config.PARAM_IOS_MIN_BUILD: "42", app_config.PARAM_IOS_LATEST_BUILD: "45"}))
        r = self._call()
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"]), {
            "ios": {"minBuild": 42, "latestBuild": 45, "storeUrl": "https://apps.apple.com/app/id6820258202"},
            "android": {"minBuild": None, "latestBuild": None, "storeUrl": None},
        })
        self.assertIn("max-age=300", r["headers"]["Cache-Control"])
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "*")

    def test_missing_params_fall_back_to_env(self):
        self._with_ssm(FakeSSM({}))
        os.environ["APP_IOS_MIN_BUILD"] = "7"
        body = json.loads(self._call()["body"])
        self.assertEqual(body["ios"]["minBuild"], 7)
        self.assertIsNone(body["ios"]["latestBuild"])

    def test_missing_params_and_env_default_zero(self):
        self._with_ssm(FakeSSM({}))
        self.assertEqual(json.loads(self._call()["body"])["ios"]["minBuild"], 0)

    def test_invalid_ssm_value_falls_back(self):
        self._with_ssm(FakeSSM({app_config.PARAM_IOS_MIN_BUILD: "abc"}))
        self.assertEqual(json.loads(self._call()["body"])["ios"]["minBuild"], 0)

    def test_ssm_error_fails_open(self):
        self._with_ssm(FakeSSM(error=RuntimeError("AccessDenied")))
        r = self._call()
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["ios"]["minBuild"], 0)

    def test_ssm_error_uses_env_fallback(self):
        self._with_ssm(FakeSSM(error=RuntimeError("boom")))
        os.environ["APP_IOS_MIN_BUILD"] = "3"
        self.assertEqual(json.loads(self._call()["body"])["ios"]["minBuild"], 3)

    def test_cache_for_five_minutes(self):
        ssm = self._with_ssm(FakeSSM({app_config.PARAM_IOS_MIN_BUILD: "10"}))
        with mock.patch.object(app_config.time, "time", return_value=1000.0):
            self._call()
            ssm.params[app_config.PARAM_IOS_MIN_BUILD] = "20"
            self.assertEqual(json.loads(self._call()["body"])["ios"]["minBuild"], 10)
        self.assertEqual(ssm.calls, 1)
        with mock.patch.object(app_config.time, "time", return_value=1000.0 + 301):
            self.assertEqual(json.loads(self._call()["body"])["ios"]["minBuild"], 20)
        self.assertEqual(ssm.calls, 2)

    def test_public_no_auth_and_no_subscription_lookup(self):
        self._with_ssm(FakeSSM({app_config.PARAM_IOS_MIN_BUILD: "5"}))
        cognito = mock.MagicMock()
        with mock.patch.object(lambda_function, "cognito", cognito):
            r = self._call()
        self.assertEqual(r["statusCode"], 200)
        cognito.admin_get_user.assert_not_called()

    def test_non_get_not_allowed_and_not_blocked_by_guard(self):
        self._with_ssm(FakeSSM({}))
        cognito = mock.MagicMock()
        with mock.patch.object(lambda_function, "cognito", cognito):
            r = self._call(method="POST")
        self.assertEqual(r["statusCode"], 405)
        cognito.admin_get_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()
