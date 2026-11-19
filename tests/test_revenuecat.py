"""Tests de suscripciones Apple vía RevenueCat (webhook + /subscription/sync) y convivencia con Paddle.

Ejecutar:  python3 -m pytest -q tests
No toca AWS ni red: Cognito es un fake en memoria y urllib se mockea.
"""
import io
import json
import os
import sys
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)

_mails_stub = sys.modules["SendMails.mails"]
for _n in ("welcome_email", "send_forgot_password_email", "account_deleted_email",
           "contact_team_email", "past_due_email", "delivery_reminder_email"):
    if not hasattr(_mails_stub, _n):
        setattr(_mails_stub, _n, lambda *a, **k: {"statusCode": 200})
if "paddle_billing.Notifications" not in sys.modules:
    test_offers_api._stub("paddle_billing")
    test_offers_api._stub("paddle_billing.Notifications", Secret=object, Verifier=object)

import lambda_function  # noqa: E402
import paddle  # noqa: E402
import revenuecat as rc  # noqa: E402

SUB = "11111111-aaaa-4bbb-8ccc-000000000001"
AUTH = "Bearer test-webhook-secret"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _future(days=30):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _past(days=1):
    return datetime.now(timezone.utc) - timedelta(days=days)


class UserNotFoundException(Exception):
    pass


class FakeCognito:
    def __init__(self, users=None):
        self.users = {k: dict(v) for k, v in (users or {}).items()}
        self.updates = []

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise UserNotFoundException("User does not exist.")
        return {"UserAttributes": [{"Name": k, "Value": v} for k, v in self.users[Username].items()]}

    def admin_update_user_attributes(self, UserPoolId, Username, UserAttributes):
        self.updates.append((Username, UserAttributes))
        for a in UserAttributes:
            self.users[Username][a["Name"]] = a["Value"]


def _http_response(payload):
    r = mock.MagicMock()
    r.read.return_value = json.dumps(payload).encode("utf-8")
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _subscriber(expires, product="qatalo_pro_monthly", entitlement="qatalo_pro", tx="2000000123"):
    ents = {}
    if entitlement:
        ents[entitlement] = {"expires_date": expires, "product_identifier": product,
                             "grace_period_expires_date": None}
    return {"subscriber": {
        "entitlements": ents,
        "subscriptions": {product: {"store": "app_store", "original_transaction_id": tx}},
    }}


def _webhook(event, auth=AUTH):
    return {"headers": {"Authorization": auth} if auth is not None else {},
            "body": json.dumps({"api_version": "1.0", "event": event})}


def _event(type_, exp=None, app_user_id=SUB, **kw):
    ev = {"type": type_, "app_user_id": app_user_id, "original_app_user_id": app_user_id,
          "aliases": [app_user_id], "product_id": "qatalo_pro_monthly",
          "entitlement_ids": ["qatalo_pro"], "original_transaction_id": "2000000123",
          "store": "APP_STORE", "environment": "SANDBOX"}
    if exp is not None:
        ev["expiration_at_ms"] = int(exp.timestamp() * 1000)
    ev.update(kw)
    return ev


class Base(unittest.TestCase):
    def setUp(self):
        self.cognito = FakeCognito({SUB: {"email": "a@b.c"}})
        self.patches = [
            mock.patch.object(rc, "cognito", self.cognito),
            mock.patch.object(paddle, "cognito", self.cognito),
            mock.patch.object(lambda_function, "cognito", self.cognito),
            mock.patch.dict(os.environ, {"REVENUECAT_WEBHOOK_AUTH": AUTH}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop("REVENUECAT_SECRET_API_KEY", None)
        os.environ.pop("REVENUECAT_ENTITLEMENT", None)
        rc._last_sync.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def attrs(self):
        return self.cognito.users[SUB]

    def hook(self, ev, auth=AUTH):
        return rc.revenuecat_routes(_webhook(ev, auth), "POST", "/Prod/revenuecat/webhook", "", "Prod")


class WebhookAuthTests(Base):
    def test_invalid_auth_returns_401(self):
        r = self.hook(_event("INITIAL_PURCHASE", _future()), auth="Bearer nope")
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(self.cognito.updates, [])

    def test_missing_auth_returns_401(self):
        r = self.hook(_event("INITIAL_PURCHASE", _future()), auth=None)
        self.assertEqual(r["statusCode"], 401)

    def test_unconfigured_secret_rejects(self):
        os.environ.pop("REVENUECAT_WEBHOOK_AUTH")
        r = self.hook(_event("INITIAL_PURCHASE", _future()))
        self.assertEqual(r["statusCode"], 401)

    def test_test_event_noop(self):
        r = self.hook({"type": "TEST", "app_user_id": SUB})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.cognito.updates, [])


class WebhookEventTests(Base):
    def test_initial_purchase_activates(self):
        exp = _future()
        r = self.hook(_event("INITIAL_PURCHASE", exp))
        self.assertEqual(r["statusCode"], 200)
        a = self.attrs()
        self.assertEqual(a["custom:transaction_status"], "active")
        self.assertEqual(a["custom:transaction_id"], "apple:2000000123")
        self.assertEqual(a["custom:customer_id"], f"rc:{SUB}")
        self.assertTrue(a["custom:due_date"].startswith(exp.strftime("%Y-%m-%dT%H:%M")))
        self.assertTrue(a["custom:due_date"].endswith("Z"))

    def test_cancellation_keeps_access_until_expiration(self):
        self.hook(_event("CANCELLATION", _future(5)))
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")

    def test_billing_issue_keeps_access_until_expiration(self):
        self.hook(_event("BILLING_ISSUE", _future(3)))
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")

    def test_expiration_deactivates(self):
        exp = _past()
        self.hook(_event("INITIAL_PURCHASE", exp))
        self.hook(_event("EXPIRATION", exp))
        self.assertEqual(self.attrs()["custom:transaction_status"], "expired")
        self.assertTrue(self.attrs()["custom:transaction_id"].startswith("apple:"))

    def test_other_entitlement_ignored(self):
        r = self.hook(_event("INITIAL_PURCHASE", _future(), entitlement_ids=["other"]))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.cognito.updates, [])

    def test_anonymous_id_ignored(self):
        anon = "$RCAnonymousID:abc123"
        r = self.hook(_event("INITIAL_PURCHASE", _future(), app_user_id=anon))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["ignored"], "anonymous")
        self.assertEqual(self.cognito.updates, [])

    def test_deleted_user_is_ok(self):
        r = self.hook(_event("INITIAL_PURCHASE", _future(), app_user_id="ghost-user"))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.cognito.updates, [])

    def test_cognito_failure_returns_500(self):
        with mock.patch.object(self.cognito, "admin_update_user_attributes",
                               side_effect=RuntimeError("throttled")):
            r = self.hook(_event("RENEWAL", _future()))
        self.assertEqual(r["statusCode"], 500)


class OutOfOrderEventTests(Base):
    """Sin REVENUECAT_SECRET_API_KEY: eventos atrasados no deben revertir el estado."""

    def test_late_expiration_after_renewal_is_ignored(self):
        first_exp = _past(1)
        renewed_exp = _future(30)
        self.hook(_event("INITIAL_PURCHASE", first_exp))
        self.hook(_event("RENEWAL", renewed_exp))
        r = self.hook(_event("EXPIRATION", first_exp))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["results"][SUB], "skipped_stale_event")
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")
        self.assertTrue(self.attrs()["custom:due_date"].startswith(
            renewed_exp.strftime("%Y-%m-%dT%H:%M")))

    def test_late_renewal_does_not_move_due_date_back(self):
        newer = _future(60)
        self.hook(_event("RENEWAL", newer))
        self.hook(_event("RENEWAL", _future(30)))
        self.assertTrue(self.attrs()["custom:due_date"].startswith(newer.strftime("%Y-%m-%dT%H:%M")))

    def test_expiration_matching_due_date_applies(self):
        exp = _past(1)
        self.hook(_event("CANCELLATION", exp))
        self.hook(_event("EXPIRATION", exp))
        self.assertEqual(self.attrs()["custom:transaction_status"], "expired")

    def test_rest_ignores_stale_guard(self):
        # Con REST la API manda aunque su fecha sea anterior a la guardada.
        self.hook(_event("RENEWAL", _future(30)))
        os.environ["REVENUECAT_SECRET_API_KEY"] = "sk_test"
        try:
            with mock.patch("urllib.request.urlopen",
                            return_value=_http_response(_subscriber(_iso(_past())))):
                self.hook(_event("EXPIRATION", _past()))
        finally:
            os.environ.pop("REVENUECAT_SECRET_API_KEY", None)
        self.assertEqual(self.attrs()["custom:transaction_status"], "expired")


class RestSourceOfTruthTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["REVENUECAT_SECRET_API_KEY"] = "sk_test"

    def tearDown(self):
        os.environ.pop("REVENUECAT_SECRET_API_KEY", None)
        super().tearDown()

    def test_rest_overrides_event(self):
        # El evento dice EXPIRATION, pero la REST dice que sigue activo (renovó después).
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(_iso(_future())))) as m:
            r = self.hook(_event("EXPIRATION", _past()))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith(f"/v1/subscribers/{SUB}"))
        self.assertEqual(req.get_header("Authorization"), "Bearer sk_test")

    def test_rest_expired_entitlement(self):
        self.cognito.users[SUB].update({"custom:transaction_id": "apple:1",
                                        "custom:transaction_status": "active"})
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(_iso(_past())))):
            self.hook(_event("RENEWAL", _future()))
        self.assertEqual(self.attrs()["custom:transaction_status"], "expired")

    def test_rest_lifetime_null_expiry_is_active(self):
        with mock.patch("urllib.request.urlopen", return_value=_http_response(_subscriber(None))):
            self.hook(_event("NON_RENEWING_PURCHASE"))
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")
        self.assertEqual(self.attrs()["custom:due_date"], "")

    def test_rest_failure_returns_500(self):
        err = urllib.error.HTTPError("u", 503, "down", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            r = self.hook(_event("RENEWAL", _future()))
        self.assertEqual(r["statusCode"], 500)
        self.assertEqual(self.cognito.updates, [])


class CoexistenceTests(Base):
    def _paddle_active(self):
        self.cognito.users[SUB].update({
            "custom:transaction_id": "sub_01paddle",
            "custom:transaction_status": "active",
            "custom:customer_id": "ctm_01",
            "custom:due_date": _iso(_future()),
        })

    def test_apple_expiration_does_not_override_paddle(self):
        self._paddle_active()
        r = self.hook(_event("EXPIRATION", _past()))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.attrs()["custom:transaction_id"], "sub_01paddle")
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")
        self.assertEqual(self.cognito.updates, [])

    def test_apple_activation_keeps_paddle_ids(self):
        # account_deletion necesita sub_/ctm_ para cancelar Paddle: Apple no los pisa.
        self._paddle_active()
        r = self.hook(_event("INITIAL_PURCHASE", _future()))
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["results"][SUB], "skipped_paddle_active")
        self.assertEqual(self.attrs()["custom:transaction_id"], "sub_01paddle")
        self.assertEqual(self.attrs()["custom:customer_id"], "ctm_01")
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")
        self.assertEqual(self.cognito.updates, [])

    def test_apple_sync_keeps_paddle_ids(self):
        self._paddle_active()
        os.environ["REVENUECAT_SECRET_API_KEY"] = "sk_test"
        try:
            with mock.patch("urllib.request.urlopen",
                            return_value=_http_response(_subscriber(_iso(_future())))):
                r = rc.handle_sync(SUB, "Prod")
        finally:
            os.environ.pop("REVENUECAT_SECRET_API_KEY", None)
        self.assertEqual(json.loads(r["body"]), {"transaction_status": "active", "active": True})
        self.assertEqual(self.attrs()["custom:transaction_id"], "sub_01paddle")
        self.assertEqual(self.attrs()["custom:customer_id"], "ctm_01")

    def test_apple_activation_after_paddle_canceled_writes(self):
        self._paddle_active()
        self.cognito.users[SUB]["custom:transaction_status"] = "canceled"
        self.hook(_event("INITIAL_PURCHASE", _future()))
        self.assertEqual(self.attrs()["custom:transaction_id"], "apple:2000000123")
        self.assertEqual(self.attrs()["custom:customer_id"], f"rc:{SUB}")

    def test_paddle_cancel_does_not_override_active_apple(self):
        self.hook(_event("INITIAL_PURCHASE", _future()))
        n = len(self.cognito.updates)
        paddle.canceled_event({"id": "sub_01", "customer_id": "ctm_01",
                               "custom_data": {"appUserId": SUB}}, "canceled")
        self.assertEqual(len(self.cognito.updates), n)
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")

    def test_paddle_cancel_applies_when_apple_expired(self):
        self.cognito.users[SUB].update({"custom:transaction_id": "apple:1",
                                        "custom:transaction_status": "active",
                                        "custom:due_date": _iso(_past())})
        paddle.canceled_event({"id": "sub_01", "customer_id": "ctm_01",
                               "custom_data": {"appUserId": SUB}}, "canceled")
        self.assertEqual(self.attrs()["custom:transaction_status"], "canceled")

    def test_paddle_activation_still_writes(self):
        self.hook(_event("INITIAL_PURCHASE", _future()))
        paddle.trialing_event({"id": "sub_02", "customer_id": "ctm_02",
                               "custom_data": {"appUserId": SUB},
                               "current_billing_period": {"ends_at": "2030-01-01T00:00:00.000000Z"}},
                              "trialing")
        self.assertEqual(self.attrs()["custom:transaction_id"], "sub_02")
        self.assertEqual(self.attrs()["custom:transaction_status"], "trialing")


class SyncTests(Base):
    CTX = SimpleNamespace(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:qatalo:Prod")

    def setUp(self):
        super().setUp()
        os.environ["REVENUECAT_SECRET_API_KEY"] = "sk_test"

    def tearDown(self):
        os.environ.pop("REVENUECAT_SECRET_API_KEY", None)
        super().tearDown()

    def _call(self, path="/Prod/subscription/sync", method="POST", sub=SUB):
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({"sub": sub, "email": "a@b.c"}).encode()).decode().rstrip("=")
        token = f"x.{payload}.y" if sub else None
        event = {"headers": {"Authorization": token} if token else {}, "rawPath": path,
                 "requestContext": {"http": {"method": method}}}
        return lambda_function.lambda_handler(event, self.CTX)

    def test_sync_activates_and_returns_status_payload(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(_iso(_future())))):
            r = self._call()
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"]), {"transaction_status": "active", "active": True})
        status = self._call("/Prod/subscription/status", "GET")
        self.assertEqual(json.loads(status["body"]), {"transaction_status": "active", "active": True})

    def test_sync_without_purchase_leaves_status(self):
        self.cognito.users[SUB]["custom:transaction_status"] = "pending"
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(None, entitlement=None))):
            r = self._call()
        self.assertEqual(json.loads(r["body"]), {"transaction_status": "pending", "active": False})
        self.assertEqual(self.cognito.updates, [])

    def test_sync_not_blocked_by_402_guard(self):
        # Usuario inactivo: la ruta está exenta del guard.
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(_iso(_past())))):
            r = self._call()
        self.assertEqual(r["statusCode"], 200)

    def test_sync_requires_user(self):
        r = self._call(sub=None)
        self.assertEqual(r["statusCode"], 401)

    def test_sync_rate_limited(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_http_response(_subscriber(_iso(_future())))):
            self.assertEqual(self._call()["statusCode"], 200)
            self.assertEqual(self._call()["statusCode"], 429)

    def test_sync_unconfigured(self):
        os.environ.pop("REVENUECAT_SECRET_API_KEY")
        self.assertEqual(self._call()["statusCode"], 503)

    def test_webhook_routed_and_exempt_from_guard(self):
        event = {"headers": {"Authorization": AUTH}, "rawPath": "/Prod/revenuecat/webhook",
                 "requestContext": {"http": {"method": "POST"}},
                 "body": json.dumps({"event": _event("INITIAL_PURCHASE", _future())})}
        os.environ.pop("REVENUECAT_SECRET_API_KEY")
        r = lambda_function.lambda_handler(event, self.CTX)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.attrs()["custom:transaction_status"], "active")


if __name__ == "__main__":
    unittest.main()
