"""Tests de recuperación de contraseña (forgot-password + change-password con token firmado).

Ejecutar:  python3 -m pytest -q tests
No toca AWS: Cognito y el envío de correo se reemplazan por fakes en memoria.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_account_deletion  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import users  # noqa: E402

SECRET = "s" * 48
SUB = "11111111-aaaa-4bbb-8ccc-000000000001"
OTHER_SUB = "22222222-aaaa-4bbb-8ccc-000000000002"
ALIAS = "Prod"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "x"}}


class FakeCognito:
    def __init__(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.users = {
            SUB: {"Username": SUB, "email": "ana@example.com", "Enabled": True, "lm": base},
            OTHER_SUB: {"Username": OTHER_SUB, "email": "otro@example.com", "Enabled": True, "lm": base},
        }
        self.set_calls = []
        self.signouts = []
        self.reject_password = False

    def _by_email(self, email):
        for u in self.users.values():
            if u["email"] == email or u["Username"] == email:
                return u
        raise ClientError("UserNotFoundException")

    def admin_get_user(self, UserPoolId, Username):
        u = self._by_email(Username)
        return {"Username": u["Username"], "Enabled": u["Enabled"], "UserLastModifiedDate": u["lm"],
                "UserAttributes": [{"Name": "sub", "Value": u["Username"]},
                                   {"Name": "email", "Value": u["email"]},
                                   {"Name": "given_name", "Value": "Ana"}]}

    def list_users(self, UserPoolId, Filter, Limit):
        sub = Filter.split('"')[1]
        u = self.users.get(sub)
        if not u:
            return {"Users": []}
        return {"Users": [{"Username": u["Username"], "Enabled": u["Enabled"], "UserLastModifiedDate": u["lm"]}]}

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        if self.reject_password:
            raise ClientError("InvalidPasswordException")
        self.set_calls.append((Username, Password, Permanent))
        self.users[Username]["lm"] += timedelta(seconds=5)  # Cognito actualiza UserLastModifiedDate

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.signouts.append(Username)


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.cog = FakeCognito()
        self.mails = []

        def fake_mail(to_address, to_name, resetLink):
            self.mails.append({"to": to_address, "link": resetLink})
            return {"statusCode": 200}

        patches = [
            mock.patch.object(users, "cognito", self.cog),
            mock.patch.object(users, "send_forgot_password_email", side_effect=fake_mail),
            mock.patch.object(users, "FRONT_END_URL", "https://qatalo.online/"),
            mock.patch.dict(os.environ, {"PASSWORD_RESET_SECRET": SECRET}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # helpers
    def forgot(self, email):
        return users.users_routes(f"/{ALIAS}/users/forgot-password/{email}", "POST", {}, ALIAS)

    def change(self, body):
        return users.users_routes(f"/{ALIAS}/users/change-password", "POST",
                                  {"body": json.dumps(body)}, ALIAS)

    def token_for(self, email="ana@example.com"):
        self.assertEqual(self.forgot(email)["statusCode"], 200)
        link = self.mails[-1]["link"]
        self.assertTrue(link.startswith("https://qatalo.online/reset-password/"))
        return link.rsplit("/", 1)[1]

    # forgot-password
    def test_forgot_sends_signed_link_not_sub(self):
        tok = self.token_for()
        self.assertNotIn(SUB, self.mails[-1]["link"])
        self.assertEqual(users.verify_reset_token(tok, SECRET.encode())["sub"], SUB)

    def test_forgot_is_generic_for_unknown_email(self):
        known = self.forgot("ana@example.com")
        unknown = self.forgot("nadie@example.com")
        self.assertEqual(unknown["statusCode"], 200)
        self.assertEqual(json.loads(known["body"]), json.loads(unknown["body"]))
        self.assertEqual(len(self.mails), 1)
        self.assertNotIn("UserNotFound", unknown["body"])

    def test_forgot_url_encoded_email(self):
        self.forgot("ana%40example.com")
        self.assertEqual(len(self.mails), 1)

    def test_forgot_without_secret_fails_closed(self):
        with mock.patch.dict(os.environ, {"PASSWORD_RESET_SECRET": ""}, clear=False):
            r = self.forgot("ana@example.com")
        self.assertEqual(r["statusCode"], 503)
        self.assertEqual(self.mails, [])

    # change-password
    def test_valid_token_resets_password(self):
        tok = self.token_for()
        r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.cog.set_calls, [(SUB, "NuevaClave1!", True)])
        self.assertEqual(self.cog.signouts, [SUB])

    def test_token_reused_after_reset_is_rejected(self):
        tok = self.token_for()
        self.assertEqual(self.change({"token": tok, "password": "NuevaClave1!"})["statusCode"], 200)
        r = self.change({"token": tok, "password": "OtraClave2!"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["code"], "invalid_token")
        self.assertEqual(len(self.cog.set_calls), 1)

    def test_expired_token_rejected(self):
        tok = self.token_for()
        later = users.time.time() + users.RESET_TOKEN_TTL_SECONDS + 1
        with mock.patch.object(users.time, "time", return_value=later):
            r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.cog.set_calls, [])

    def test_bad_signature_rejected(self):
        tok = self.token_for()
        p64, sig = tok.split(".")
        forged_payload = users._b64e(json.dumps(
            {"sub": OTHER_SUB, "exp": int(users.time.time()) + 600, "v": "x"}).encode())
        for bad in (f"{forged_payload}.{sig}", f"{p64}.{sig[:-2]}AA", tok + "x", "", "a.b.c",
                    users.make_reset_token(SUB, "v", b"otro-secreto" * 4)):
            r = self.change({"token": bad, "password": "NuevaClave1!"})
            self.assertEqual(r["statusCode"], 400, bad)
        self.assertEqual(self.cog.set_calls, [])

    def test_raw_sub_as_token_rejected(self):
        # El enlace antiguo /reset-password/<sub> ya no sirve.
        r = self.change({"token": SUB, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.cog.set_calls, [])

    def test_unknown_sub_rejected(self):
        tok = self.token_for()
        del self.cog.users[SUB]
        r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["code"], "invalid_token")

    def test_signed_token_for_nonexistent_sub(self):
        tok = users.make_reset_token("33333333-aaaa-4bbb-8ccc-000000000003", "v", SECRET.encode())
        r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 400)

    def test_username_in_body_is_ignored(self):
        tok = self.token_for()
        r = self.change({"token": tok, "username": "otro@example.com", "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.cog.set_calls[0][0], SUB)

    def test_legacy_body_username_only_rejected(self):
        r = self.change({"username": "ana@example.com", "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(self.cog.set_calls, [])

    def test_missing_secret_fails_closed(self):
        tok = self.token_for()
        with mock.patch.dict(os.environ, {"PASSWORD_RESET_SECRET": "corto"}, clear=False):
            r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 503)
        self.assertEqual(self.cog.set_calls, [])

    def test_alias_specific_secret(self):
        with mock.patch.dict(os.environ, {"PASSWORD_RESET_SECRET_PROD": "p" * 40}, clear=False):
            tok = self.token_for()
            self.assertIsNone(users.verify_reset_token(tok, SECRET.encode()))
            r = self.change({"token": tok, "password": "NuevaClave1!"})
        self.assertEqual(r["statusCode"], 200)

    def test_short_password_rejected(self):
        tok = self.token_for()
        r = self.change({"token": tok, "password": "abc"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["code"], "invalid_password")
        self.assertEqual(self.cog.set_calls, [])

    def test_cognito_policy_rejection_is_400_and_token_still_valid(self):
        tok = self.token_for()
        self.cog.reject_password = True
        r = self.change({"token": tok, "password": "solominusculas"})
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["code"], "invalid_password")
        self.cog.reject_password = False
        self.assertEqual(self.change({"token": tok, "password": "NuevaClave1!"})["statusCode"], 200)

    def test_disabled_user_rejected(self):
        tok = self.token_for()
        self.cog.users[SUB]["Enabled"] = False
        self.assertEqual(self.change({"token": tok, "password": "NuevaClave1!"})["statusCode"], 400)

    def test_logs_contain_no_pii_or_token(self):
        with mock.patch("builtins.print") as pr:
            tok = self.token_for()
            self.change({"token": tok, "password": "NuevaClave1!"})
            self.forgot("nadie@example.com")
        out = " ".join(str(c) for c in pr.call_args_list)
        for secret_bit in ("ana@example.com", "nadie@example.com", SUB, tok, "NuevaClave1!"):
            self.assertNotIn(secret_bit, out)


if __name__ == "__main__":
    unittest.main()
