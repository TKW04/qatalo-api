"""Tests de eliminación de cuenta (DELETE /users/me) y del correo de bienvenida.

Ejecutar:  python3 -m pytest -q tests
No toca AWS: DynamoDB, S3, Cognito, Lambda y Paddle se reemplazan por fakes en memoria.
"""
import json
import os
import sys
import time
import unittest
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)

_mails_stub = sys.modules["SendMails.mails"]
for _n in ("welcome_email", "send_forgot_password_email", "account_deleted_email",
           "contact_team_email", "past_due_email", "delivery_reminder_email"):
    if not hasattr(_mails_stub, _n):
        setattr(_mails_stub, _n, lambda *a, **k: {"statusCode": 200})
# paddle.py (importado por lambda_function) usa el SDK de Paddle de la Layer
if "paddle_billing.Notifications" not in sys.modules:
    test_offers_api._stub("paddle_billing")
    test_offers_api._stub("paddle_billing.Notifications", Secret=object, Verifier=object)

import account_deletion as ad  # noqa: E402
import business  # noqa: E402
import users  # noqa: E402

SUB = "11111111-aaaa-4bbb-8ccc-000000000001"
OTHER_SUB = "22222222-aaaa-4bbb-8ccc-000000000002"
B1, B2 = "b1000000-0000-0000-0000-000000000001", "b2000000-0000-0000-0000-000000000002"
P1, P2 = "p1000000-0000-0000-0000-000000000001", "p2000000-0000-0000-0000-000000000002"
C1, C2 = "c1000000-0000-0000-0000-000000000001", "c2000000-0000-0000-0000-000000000002"
BUCKET = "qatalo"
URL = f"https://{BUCKET}.s3.us-east-1.amazonaws.com/"


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    def __init__(self, key, items=()):
        self.key = key
        self.items = {i[key]: dict(i) for i in items}
        self.deleted = []

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {"Item": _copy(it)} if it is not None else {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and Item[self.key] in self.items:
            raise ConditionalCheckFailedException("ConditionalCheckFailedException")
        self.items[Item[self.key]] = _copy(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kw):
        it = self.items.setdefault(Key[self.key], dict(Key))
        for part in UpdateExpression.replace("SET", "", 1).split(","):
            name, val = [x.strip() for x in part.split("=")]
            it[name] = ExpressionAttributeValues[val]
        return {}

    def delete_item(self, Key):
        self.deleted.append(Key[self.key])
        self.items.pop(Key[self.key], None)

    def scan(self, **kw):
        # El filtro del servidor NO se evalúa: el código debe re-verificar la pertenencia en Python.
        return {"Items": [_copy(i) for i in self.items.values()]}


def _copy(x):
    if isinstance(x, dict):
        return {k: _copy(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_copy(v) for v in x]
    return x


class FakeS3:
    def __init__(self, keys):
        self.objects = set(keys)
        self.deleted = []

    def list_objects_v2(self, Bucket, Prefix, **kw):
        assert Bucket == BUCKET
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        assert Bucket == BUCKET
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.discard(o["Key"])
        return {}


class UserNotFoundException(Exception):
    pass


class FakeCognito:
    def __init__(self):
        self.users = {SUB: {"email": "dueno@example.com", "given_name": "Ana", "family_name": "Pérez",
                            "custom:transaction_id": "sub_01abc", "custom:customer_id": "ctm_01xyz"}}
        self.deleted = []

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise UserNotFoundException("UserNotFoundException")
        return {"UserAttributes": [{"Name": k, "Value": v} for k, v in self.users[Username].items()]}

    def admin_delete_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise UserNotFoundException("UserNotFoundException")
        self.deleted.append(Username)
        del self.users[Username]


class FakeResp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class FakePaddle:
    """Simula la API de Paddle Billing. fail=True → responde 500 a todo."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.status = {"sub_01abc": "active", "sub_02def": "trialing"}

    def __call__(self, method, url, key, budget, body=None):
        self.calls.append((method, url, body))
        if self.fail:
            return FakeResp(500, {"error": {"code": "internal_error"}})
        if method == "GET" and "/subscriptions?customer_id=ctm_01xyz" in url:
            return FakeResp(200, {"data": [{"id": s, "status": st} for s, st in self.status.items()
                                           if st != "canceled"]})
        if method == "GET":
            sid = url.rsplit("/", 1)[-1]
            return FakeResp(200, {"data": {"id": sid, "status": self.status.get(sid, "canceled")}})
        if method == "POST" and url.endswith("/cancel"):
            sid = url.split("/")[-2]
            self.status[sid] = "canceled"
            return FakeResp(200, {"data": {"id": sid, "status": "canceled"}})
        return FakeResp(404)


def _claims(**over):
    c = {"sub": SUB, "email": "dueno@example.com", "given_name": "Ana", "family_name": "Pérez",
         "token_use": "id", "auth_time": int(time.time()) - 30, "cognito:username": SUB}
    c.update(over)
    return c


def _event(body=None, token="tok"):
    return {"headers": {"Authorization": token} if token else {},
            "body": json.dumps(body if body is not None else {"confirm": "ELIMINAR"}),
            "rawPath": "/Prod/users/me",
            "requestContext": {"http": {"method": "DELETE"}}}


class Ctx:
    invoked_function_arn = "arn:aws:lambda:us-east-1:123:function:qatolo:Prod"

    def __init__(self, ms=25000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


class World:
    """Datos de dos negocios: el del usuario que se borra (B1/SUB) y otro (B2/OTHER_SUB)."""

    def __init__(self, paddle_fail=False):
        self.business = FakeTable("business_id", [
            {"business_id": B1, "user_id": SUB, "business_name": "Mi Tienda", "business_slug": "mitienda",
             "rnc": "131000001", "itbis_rate": Decimal("18"),
             "business_logo_url": URL + f"business/{SUB}_logo.png",
             "custom_fonts": [{"url": URL + f"business/{SUB}_font_1.woff2"}]},
            {"business_id": B2, "user_id": OTHER_SUB, "business_name": "Otra", "business_slug": "otra",
             "business_logo_url": URL + f"business/{OTHER_SUB}_logo.png"},
        ])
        self.products = FakeTable("product_id", [
            # Producto propio que (como una cuenta demo) reutiliza la imagen de OTRA cuenta + una propia.
            {"product_id": P1, "business_id": B1, "user_id": SUB, "itbis_mode": "included",
             "imagesUrl": [URL + f"products/{OTHER_SUB}_prod-9.jpg", URL + f"products/{SUB}_prod-1.jpg",
                           "https://otro-bucket.s3.amazonaws.com/x.jpg", URL + "qatalo.png"]},
            {"product_id": P2, "business_id": B2, "user_id": OTHER_SUB,
             "imagesUrl": [URL + f"products/{OTHER_SUB}_prod-9.jpg"]},
        ])
        self.categories = FakeTable("category_id", [
            {"category_id": "cat-mine", "business_id": B1, "user_id": SUB},
            {"category_id": "cat-other", "business_id": B2, "user_id": OTHER_SUB},
            # creada por SUB pero apuntando a OTRO negocio: no es de su negocio → no se borra
            {"category_id": "cat-cross", "business_id": B2, "user_id": SUB},
        ])
        self.payment_methods = FakeTable("payment_method_id", [
            {"payment_method_id": "pm-mine", "business_id": B1, "user_id": SUB},
            {"payment_method_id": "pm-other", "business_id": B2, "user_id": OTHER_SUB},
        ])
        self.offers = FakeTable("offer_id", [
            {"offer_id": "of-mine", "business_id": B1},
            {"offer_id": "of-other", "business_id": B2},
        ])
        self.suggestions = FakeTable("suggestion_id", [
            {"suggestion_id": "sg-mine", "business_id": B1, "user_id": SUB},
            {"suggestion_id": "sg-other", "business_id": B2, "user_id": OTHER_SUB},
        ])
        self.customers = FakeTable("customer_id", [
            {"customer_id": C1, "business_id": B1, "full_name": "Juan Cliente", "phone": "8095550000",
             "email": "juan@example.com", "rnc": "001-0000000-1",
             "transactions": [
                 {"transaction_id": "t1", "order_group": "og-ncf-0001", "product_id": P1, "quantity": 2,
                  "price": Decimal("118"), "delivery_price": Decimal("100"), "status": "Entregada",
                  "ncf_used": "B0100000001", "ncf_date": "2026-05-01 10:00:00", "invoice_type": "factura",
                  "delivery_address": "Calle 1", "payment_method": {"currency": "DOP"},
                  "receipt_url": URL + f"customers/{C1}_receipt_t1.jpg"},
                 {"transaction_id": "t2", "order_group": "og-sin-ncf", "product_id": P1, "quantity": 1,
                  "price": Decimal("50"), "status": "Aprobada"},
             ]},
            {"customer_id": C2, "business_id": B2, "full_name": "Otro Cliente",
             "transactions": [{"transaction_id": "t9", "order_group": "og-otro", "ncf_used": "B0100000099",
                               "ncf_date": "2026-05-01 10:00:00", "price": Decimal("10"), "quantity": 1}]},
        ])
        self.s3 = FakeS3([
            f"business/{SUB}_logo.png", f"business/{SUB}_font_1.woff2", f"products/{SUB}_prod-1.jpg",
            f"business/products/{P1}_image.jpg", f"customers/{C1}_receipt_t1.jpg", f"invoices/{B1}/factura_A.pdf",
            # ajenos
            f"business/{OTHER_SUB}_logo.png", f"products/{OTHER_SUB}_prod-9.jpg",
            f"business/products/{P2}_image.jpg", f"customers/{C2}_receipt_t9.jpg",
            f"invoices/{B2}/factura_B.pdf", "qatalo.png",
        ])
        self.cognito = FakeCognito()
        self.paddle = FakePaddle(fail=paddle_fail)
        self.lambda_client = mock.MagicMock()
        self.emails = []

    def patches(self, claims=None):
        p = [
            mock.patch.object(ad, "business_table", self.business),
            mock.patch.object(ad, "products_table", self.products),
            mock.patch.object(ad, "categories_table", self.categories),
            mock.patch.object(ad, "payment_methods_table", self.payment_methods),
            mock.patch.object(ad, "offers_table", self.offers),
            mock.patch.object(ad, "suggestions_table", self.suggestions),
            mock.patch.object(ad, "customers_table", self.customers),
            mock.patch.object(ad, "records_table", self.customers),
            mock.patch.object(ad, "s3", self.s3),
            mock.patch.object(ad, "cognito", self.cognito),
            mock.patch.object(ad, "lambda_client", self.lambda_client),
            mock.patch.object(ad, "_paddle_request", self.paddle),
            mock.patch.object(ad, "_verify_jwt", return_value=claims or _claims()),
            mock.patch.object(ad, "_send_account_deleted_email",
                              side_effect=lambda e, n: self.emails.append(e) or True),
            mock.patch.dict(os.environ, {"BUCKET_NAME": BUCKET, "PADDLE_API_KEY_PROD": "pdl_key",
                                         "PADDLE_API_BASE_PROD": "https://api.paddle.test"}),
        ]
        return p

    def run(self, fn, *a, claims=None, **kw):
        ps = self.patches(claims)
        for p in ps:
            p.start()
        try:
            return fn(*a, **kw)
        finally:
            for p in reversed(ps):
                p.stop()

    def delete(self, body=None, ctx=None, claims=None):
        r = self.run(ad.handle_delete_request, _event(body), ctx or Ctx(), "Prod", claims=claims)
        return r["statusCode"], json.loads(r["body"])

    def record(self):
        return self.customers.items.get(f"account_deletion#{ad.deletion_id_for(SUB)}")


class ValidationTests(unittest.TestCase):
    def test_requires_confirmation(self):
        w = World()
        for body in ({}, {"confirm": "si"}, {"confirm": ""}):
            status, data = w.delete(body)
            self.assertEqual(status, 400)
            self.assertEqual(data["error"], "confirmation_required")
        self.assertEqual(w.cognito.deleted, [])
        self.assertIn(B1, w.business.items)

    def test_invalid_token(self):
        with mock.patch.object(ad, "_verify_jwt", return_value=None):
            r = ad.handle_delete_request(_event(), Ctx(), "Prod")
        self.assertEqual(r["statusCode"], 401)
        r = ad.handle_delete_request(_event(token=None), Ctx(), "Prod")
        self.assertEqual(r["statusCode"], 401)

    def test_requires_recent_authentication(self):
        w = World()
        status, data = w.delete(claims=_claims(auth_time=int(time.time()) - 3600))
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "reauth_required")
        self.assertEqual(w.cognito.deleted, [])


class FullDeletionTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.status, self.data = self.w.delete()

    def test_response_and_record(self):
        self.assertEqual(self.status, 200)
        self.assertEqual(self.data["status"], "completed")
        self.assertEqual(self.data["subscription_cancellation"], "canceled")
        rec = self.w.record()
        self.assertEqual(rec["status"], "completed")
        self.assertNotIn("user_sub", rec)               # minimización al terminar
        self.assertNotIn("business_id", rec)            # invisible para las consultas por negocio
        self.assertEqual(self.w.emails, ["dueno@example.com"])
        self.assertNotIn("dueno@example.com", json.dumps(rec, default=str))

    def test_owner_data_deleted(self):
        w = self.w
        self.assertEqual(w.cognito.deleted, [SUB])
        self.assertNotIn(B1, w.business.items)
        self.assertNotIn(P1, w.products.items)
        self.assertNotIn("cat-mine", w.categories.items)
        self.assertNotIn("pm-mine", w.payment_methods.items)
        self.assertNotIn("of-mine", w.offers.items)
        self.assertNotIn("sg-mine", w.suggestions.items)
        self.assertNotIn(C1, w.customers.items)

    def test_other_business_untouched(self):
        w = self.w
        self.assertIn(B2, w.business.items)
        self.assertIn(P2, w.products.items)
        self.assertIn("cat-other", w.categories.items)
        self.assertIn("cat-cross", w.categories.items)
        self.assertIn("pm-other", w.payment_methods.items)
        self.assertIn("of-other", w.offers.items)
        self.assertIn("sg-other", w.suggestions.items)
        self.assertIn(C2, w.customers.items)
        self.assertFalse(any(k.startswith("legal_retention#" + B2) for k in w.customers.items))

    def test_s3_only_owned_prefixes(self):
        w = self.w
        self.assertEqual(w.s3.objects, {
            f"business/{OTHER_SUB}_logo.png", f"products/{OTHER_SUB}_prod-9.jpg",
            f"business/products/{P2}_image.jpg", f"customers/{C2}_receipt_t9.jpg",
            f"invoices/{B2}/factura_B.pdf", "qatalo.png",
        })
        for k in w.s3.deleted:
            self.assertTrue(ad.is_owned_key(k, SUB, [B1], [P1], [C1]), k)
        # la imagen "prestada" de otra cuenta (demo) se ignoró, no se intentó borrar
        self.assertNotIn(f"products/{OTHER_SUB}_prod-9.jpg", w.s3.deleted)

    def test_ncf_invoice_retained_minimal(self):
        ret = [v for k, v in self.w.customers.items.items() if k.startswith("legal_retention#")]
        self.assertEqual(len(ret), 1)
        r = ret[0]
        self.assertTrue(r["retained_for_legal"])
        self.assertEqual(r["ncf"], "B0100000001")
        self.assertEqual(r["ncf_type"], "B01")
        self.assertEqual(r["ncf_date"], "2026-05-01 10:00:00")
        self.assertEqual(r["issuer_rnc"], "131000001")
        self.assertEqual(r["buyer_name"], "Juan Cliente")
        self.assertEqual(r["buyer_rnc"], "001-0000000-1")
        self.assertEqual(r["totals"]["total"], Decimal("336.00"))   # 2×118 + 100 delivery
        self.assertEqual(r["totals"]["itbis"], Decimal("36.00"))
        self.assertEqual(r["retain_until"], "2036-05-01")
        self.assertNotIn("business_id", r)
        dump = json.dumps(r, default=str)
        for pii in ("8095550000", "juan@example.com", "Calle 1", "receipt", "og-sin-ncf"):
            self.assertNotIn(pii, dump)

    def test_paddle_canceled_immediately(self):
        posts = [c for c in self.w.paddle.calls if c[0] == "POST"]
        self.assertEqual({c[1] for c in posts}, {
            "https://api.paddle.test/subscriptions/sub_01abc/cancel",
            "https://api.paddle.test/subscriptions/sub_02def/cancel",
        })
        for c in posts:
            self.assertEqual(c[2], {"effective_from": "immediately"})

    def test_idempotent_retry(self):
        w = self.w
        calls_before = len(w.paddle.calls)
        status, data = w.delete()
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(len(w.paddle.calls), calls_before)
        self.assertEqual(w.emails, ["dueno@example.com"])
        self.assertEqual(w.cognito.deleted, [SUB])
        self.assertIn(B2, w.business.items)

    def test_catalog_hidden_while_pending(self):
        table = FakeTable("business_id", [{"business_id": "bx", "user_id": "u", "business_slug": "x",
                                           "account_deletion_pending": True}])
        with mock.patch.object(business, "business_table", table), \
                mock.patch.object(business, "_owner_sub_status", return_value="active"):
            r = business.get_business_by_slug("x")
        self.assertEqual(r["statusCode"], 404)


class PaddleFailureTests(unittest.TestCase):
    def test_paddle_failure_does_not_abort(self):
        w = World(paddle_fail=True)
        status, data = w.delete()
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["subscription_cancellation"], "pending_retry")
        self.assertNotIn(B1, w.business.items)
        self.assertEqual(w.cognito.deleted, [SUB])
        rec = w.record()
        self.assertEqual(rec["paddle"]["state"], "retry")
        self.assertTrue(rec["paddle"]["last_error"].startswith("list_http_500"))
        self.assertEqual(rec["paddle"]["customer_id"], "ctm_01xyz")   # se conserva para reintentar
        # reintentos en el barrido diario → al 3er fallo queda para revisión manual
        w.run(ad.resume_pending, Ctx())
        w.run(ad.resume_pending, Ctx())
        rec = w.record()
        self.assertEqual(rec["paddle"]["state"], "manual")
        self.assertTrue(rec["paddle"]["needs_manual"])
        # Paddle se recupera, pero "manual" ya no se reintenta solo
        w.paddle.fail = False
        w.run(ad.resume_pending, Ctx())
        self.assertEqual(w.record()["paddle"]["state"], "manual")

    def test_paddle_retry_succeeds_in_sweep(self):
        w = World(paddle_fail=True)
        w.delete()
        w.paddle.fail = False
        w.run(ad.resume_pending, Ctx())
        rec = w.record()
        self.assertEqual(rec["paddle"]["state"], "canceled")
        self.assertNotIn("customer_id", rec["paddle"])
        self.assertNotIn("user_sub", rec)

    def test_no_subscription(self):
        w = World()
        w.cognito.users[SUB]["custom:transaction_id"] = "0"
        w.cognito.users[SUB]["custom:customer_id"] = ""
        status, data = w.delete()
        self.assertEqual(data["subscription_cancellation"], "not_applicable")
        self.assertEqual(w.paddle.calls, [])


class ResumableTests(unittest.TestCase):
    def test_partial_then_worker_completes(self):
        w = World()
        real_ok = ad.Budget.ok
        state = {"n": 0}

        def limited_ok(self, need_ms=0):
            state["n"] += 1
            return state["n"] <= 6 and real_ok(self, need_ms)

        with mock.patch.object(ad.Budget, "ok", limited_ok):
            status, data = w.delete()
        self.assertEqual(status, 202)
        self.assertEqual(data["status"], "in_progress")
        # El catálogo ya se desactivó aunque el borrado siga en curso
        self.assertTrue(w.business.items[B1].get("account_deletion_pending"))
        w.lambda_client.invoke.assert_called_once()
        payload = json.loads(w.lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["source"], ad.WORKER_SOURCE)
        self.assertEqual(w.lambda_client.invoke.call_args.kwargs["InvocationType"], "Event")

        out = w.run(ad.handle_worker_event, payload, Ctx())
        self.assertEqual(out["status"], "completed")
        self.assertNotIn(B1, w.business.items)
        self.assertIn(B2, w.business.items)
        self.assertEqual(w.emails, ["dueno@example.com"])

    def test_sweep_resumes_stuck_deletion(self):
        w = World()
        with mock.patch.object(ad.Budget, "ok", lambda self, need_ms=0: False):
            w.delete()
        self.assertEqual(w.record()["status"], "in_progress")
        w.run(ad.resume_pending, Ctx())
        self.assertEqual(w.record()["status"], "completed")
        self.assertNotIn(C1, w.customers.items)
        self.assertIn(C2, w.customers.items)

    def test_router_dispatch(self):
        import lambda_function
        with mock.patch.object(lambda_function.account_deletion, "handle_delete_request",
                               return_value={"statusCode": 200}) as h:
            ev = _event()
            r = lambda_function.lambda_handler(ev, Ctx())
        self.assertEqual(r["statusCode"], 200)
        h.assert_called_once()
        with mock.patch.object(lambda_function.account_deletion, "handle_worker_event",
                               return_value={"ok": True}) as hw:
            lambda_function.lambda_handler({"source": ad.WORKER_SOURCE, "deletion_id": "x"}, Ctx())
            hw.assert_called_once()
            hw.reset_mock()
            # Un evento HTTP no puede hacerse pasar por el worker
            ev = _event()
            ev["source"] = ad.WORKER_SOURCE
            with mock.patch.object(lambda_function.account_deletion, "handle_delete_request",
                                   return_value={"statusCode": 200}):
                lambda_function.lambda_handler(ev, Ctx())
            hw.assert_not_called()


class OwnershipUnitTests(unittest.TestCase):
    def test_is_owned_key(self):
        own = dict(sub=SUB, business_ids=[B1], product_ids=[P1], customer_ids=[C1])
        self.assertTrue(ad.is_owned_key(f"business/{SUB}_logo.png", **own))
        self.assertTrue(ad.is_owned_key(f"products/{SUB}_prod-x.jpg", **own))
        self.assertTrue(ad.is_owned_key(f"invoices/{B1}/f.pdf", **own))
        self.assertTrue(ad.is_owned_key(f"business/products/{P1}_image.png", **own))
        self.assertTrue(ad.is_owned_key(f"customers/{C1}_receipt_t1.jpg", **own))
        for k in (f"business/{OTHER_SUB}_logo.png", f"invoices/{B2}/f.pdf", "qatalo.png",
                  f"business/products/{P2}_image.png", f"customers/{C2}_receipt_t.jpg",
                  f"business/x{SUB}_logo.png", f"invoices/{B1}", "", None):
            self.assertFalse(ad.is_owned_key(k, **own), k)
        # IDs vacíos o cortos nunca habilitan prefijos amplios
        self.assertFalse(ad.is_owned_key("invoices/x/f.pdf", "", ["", "x"], [""], [""]))

    def test_s3_key_from_url(self):
        self.assertEqual(ad.s3_key_from_url(URL + "products/a%20b.jpg", BUCKET), "products/a b.jpg")
        self.assertEqual(ad.s3_key_from_url("https://qatalo.s3.amazonaws.com/x/y.png", BUCKET), "x/y.png")
        self.assertEqual(ad.s3_key_from_url("https://s3.us-east-1.amazonaws.com/qatalo/x.png", BUCKET), "x.png")
        self.assertIsNone(ad.s3_key_from_url("https://otro.s3.amazonaws.com/x.png", BUCKET))
        self.assertIsNone(ad.s3_key_from_url("https://evil.com/qatalo.s3.amazonaws.com/x", BUCKET))
        self.assertIsNone(ad.s3_key_from_url("https://qatalo.s3.amazonaws.com.evil.com/x", BUCKET))


# ───────── correo de bienvenida ─────────
class WelcomeEmailTests(unittest.TestCase):
    def _register(self, mail_side_effect=None, env=None):
        cog = mock.MagicMock()
        calls = []

        def fake_welcome(*a, **k):
            calls.append((a, k))
            if isinstance(mail_side_effect, Exception):
                raise mail_side_effect
            return mail_side_effect or {"statusCode": 200}

        with mock.patch.object(users, "cognito", cog), \
                mock.patch.object(users, "welcome_email", side_effect=fake_welcome), \
                mock.patch.object(users, "FRONT_END_URL", "https://qatalo.online"), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            r = users.register_user({"body": json.dumps({
                "email": "nuevo@example.com", "given_name": "Luis", "family_name": "Gómez",
                "password": "Secreta123!"})})
        return r, calls, cog

    def test_register_sends_welcome_with_plans_link(self):
        r, calls, cog = self._register()
        self.assertEqual(r["statusCode"], 200)
        self.assertTrue(json.loads(r["body"])["welcome_email_sent"])
        a, k = calls[0]
        self.assertEqual(a[0], "nuevo@example.com")
        self.assertEqual(k["plans_link"], "https://qatalo.online/payment")
        self.assertEqual(k["to_name"], "Luis Gómez")
        cog.admin_create_user.assert_called_once()

    def test_mail_failure_does_not_break_register(self):
        for eff in (RuntimeError("smtp down"), {"statusCode": 500}):
            r, calls, _ = self._register(mail_side_effect=eff)
            self.assertEqual(r["statusCode"], 200)
            self.assertFalse(json.loads(r["body"])["welcome_email_sent"])

    def test_can_be_disabled(self):
        r, calls, _ = self._register(env={"WELCOME_EMAIL_ENABLED": "false"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(calls, [])

    def test_templates_render(self):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(os.path.join(HERE, "..", "QATOLO", "templates")))
        html = env.get_template("welcome.html").render(
            name="Luis", plans_link="https://qatalo.online/payment", login_link="https://qatalo.online/login",
            support_email="info@qatalo.online", opt_out_link="mailto:info@qatalo.online?subject=x")
        for s in ("Luis", "https://qatalo.online/payment", "info@qatalo.online", "activar tu catálogo",
                  "correos informativos", "mailto:info@qatalo.online?subject=x"):
            self.assertIn(s, html)
        html = env.get_template("account_deleted.html").render(name="Ana", support_email="info@qatalo.online")
        self.assertIn("Tu cuenta fue eliminada", html)
        self.assertIn("info@qatalo.online", html)


if __name__ == "__main__":
    unittest.main()
