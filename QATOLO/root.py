import os
import json
import time
import base64
import hmac
import hashlib
import traceback

import boto3
import requests
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
cognito = boto3.client("cognito-idp")

business_table = dynamodb.Table("qatalo.business")
products_table = dynamodb.Table("qatalo.products")
customers_table = dynamodb.Table("qatalo.customers")
suggestions_table = dynamodb.Table("qatalo.suggestions")

REGION = os.getenv("AWS_REGION", "us-east-1")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "us-east-1_HqAKfGYFY")
ROOT_GROUP = "root"

_ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
_JWKS_URL = f"{_ISS}/.well-known/jwks.json"

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}
DATE_FMT = "%Y-%m-%d %H:%M:%S"

VALID_STATUSES = {
    "nueva",
    "en_revision",
    "planeada",
    "en_progreso",
    "completada",
    "descartada",
}

# ASN.1 DigestInfo para SHA-256 (para verificar el padding PKCS#1 v1.5)
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")

_jwks_cache = None


# ----------------- Helpers básicos -----------------
def _resp(status, body=None):
    out = {"statusCode": status, "headers": CORS}
    if body is not None:
        out["body"] = json.dumps(body, default=str)
    return out


def _now():
    return datetime.now().strftime(DATE_FMT)


def _b64url_dec(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def _token_from_event(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = headers.get("authorization", "") or headers.get("x-customer-token", "")
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    return raw.strip()


# ----------------- Verificación de firma RS256 (sin librerías) -----------------
def _get_jwks():
    global _jwks_cache
    if _jwks_cache is None:
        r = requests.get(_JWKS_URL, timeout=5)
        _jwks_cache = {k["kid"]: k for k in r.json().get("keys", [])}
    return _jwks_cache


def _verify_cognito_jwt(token):
    """
    Verifica firma RS256 + emisor + expiración contra el JWKS del User Pool.
    Devuelve los claims si es válido, o None. No usa PyJWT ni cryptography.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_b64url_dec(parts[0]))
        payload = json.loads(_b64url_dec(parts[1]))
        signature = _b64url_dec(parts[2])

        if header.get("alg") != "RS256":
            return None
        kid = header.get("kid")
        if not kid:
            return None

        jwk = _get_jwks().get(kid)
        if not jwk:
            # posible rotación de llaves: refrescar cache una vez
            global _jwks_cache
            _jwks_cache = None
            jwk = _get_jwks().get(kid)
            if not jwk:
                return None

        n = int.from_bytes(_b64url_dec(jwk["n"]), "big")
        e = int.from_bytes(_b64url_dec(jwk["e"]), "big")
        k = (n.bit_length() + 7) // 8

        sig_int = int.from_bytes(signature, "big")
        if sig_int >= n:
            return None

        # RSA verify: EM = sig^e mod n
        em = pow(sig_int, e, n).to_bytes(k, "big")

        # Reconstruir el EM esperado (EMSA-PKCS1-v1_5)
        signing_input = (parts[0] + "." + parts[1]).encode()
        digest = hashlib.sha256(signing_input).digest()
        t = _SHA256_DIGESTINFO + digest
        ps_len = k - len(t) - 3
        if ps_len < 8:
            return None
        expected = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t

        if not hmac.compare_digest(em, expected):
            return None

        # Validar claims mínimos
        if payload.get("iss") != _ISS:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None

        return payload
    except Exception as e:
        print(json.dumps({"event": "_verify_cognito_jwt", "Error": str(e)}))
        return None


def _is_root(claims):
    groups = claims.get("cognito:groups", []) or []
    return ROOT_GROUP in groups


def _require_root(event):
    """Devuelve (claims, None) si es root válido, o (None, error_response)."""
    token = _token_from_event(event)
    if not token:
        return None, _resp(401, {"message": "No autorizado"})
    claims = _verify_cognito_jwt(token)
    if not claims:
        return None, _resp(401, {"message": "Sesión inválida o expirada"})
    if not _is_root(claims):
        return None, _resp(403, {"message": "Acceso restringido"})
    return claims, None


# ----------------- Cognito: perfil del dueño -----------------
def _cognito_profile(user_id):
    """
    Trae email, estado y atributos custom del dueño desde Cognito.
    Como guardas el estatus de suscripción en Cognito, aquí sale disponible.
    """
    out = {
        "email": "",
        "enabled": None,
        "user_status": "",
        "groups": [],
        "custom": {},
        "created_at": "",
    }
    if not user_id:
        return out
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
        attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
        out["email"] = attrs.get("email", "")
        out["enabled"] = user.get("Enabled")
        out["user_status"] = user.get("UserStatus", "")
        created = user.get("UserCreateDate")
        out["created_at"] = created.strftime(DATE_FMT) if created else ""
        # Todos los atributos custom (ahí vive el estatus de suscripción)
        out["custom"] = {
            k.replace("custom:", ""): v for k, v in attrs.items() if k.startswith("custom:")
        }
    except Exception as e:
        print(json.dumps({"event": "_cognito_profile.attrs", "user_id": user_id, "Error": str(e)}))
    try:
        g = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=user_id)
        out["groups"] = [grp.get("GroupName", "") for grp in g.get("Groups", [])]
    except Exception as e:
        print(json.dumps({"event": "_cognito_profile.groups", "user_id": user_id, "Error": str(e)}))
    return out


# ----------------- Router -----------------
def root_routes(path, method, event, alias):
    try:
        # TODO lo de aquí requiere ser root válido (firma verificada + grupo)
        claims, err = _require_root(event)
        if err:
            return err

        if path == f"/{alias}/root/overview" and method == "GET":
            return get_overview()
        if path == f"/{alias}/root/businesses" and method == "GET":
            return get_businesses()
        if path == f"/{alias}/root/suggestions" and method == "GET":
            return get_all_suggestions()
        if path == f"/{alias}/root/suggestions/status" and method == "POST":
            return update_suggestion_status(event, editor=claims.get("email", "root"))

        return _resp(404, {"message": "Ruta no encontrada"})
    except Exception as e:
        print(json.dumps({"event": "root_routes", "Error": str(e), "trace": traceback.format_exc()}))
        return _resp(500, {"message": str(e)})


# ----------------- Overview -----------------
def get_overview():
    try:
        biz_count = business_table.scan(Select="COUNT").get("Count", 0)

        sug_items = suggestions_table.scan().get("Items", [])
        by_status = {}
        for s in sug_items:
            st = s.get("status", "nueva")
            by_status[st] = by_status.get(st, 0) + 1

        return _resp(
            200,
            {
                "businesses": biz_count,
                "suggestions_total": len(sug_items),
                "suggestions_by_status": by_status,
            },
        )
    except Exception as e:
        print(json.dumps({"event": "get_overview", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Negocios (clientes) -----------------
def get_businesses():
    """
    Lista todos los negocios con: datos básicos, # productos, # clientes,
    # órdenes, y el perfil Cognito del dueño (email, estatus, atributos custom
    donde vive la suscripción).
    NOTA: para muchos negocios esto hace varios scans; a la escala actual está
    bien. Si crece, conviene mover conteos a un índice o denormalizar.
    """
    try:
        businesses = business_table.scan().get("Items", [])
        result = []

        for b in businesses:
            bid = b.get("business_id", "")
            user_id = b.get("user_id", "")

            # Conteo de productos (COUNT no transfiere datos)
            try:
                products_count = products_table.scan(
                    FilterExpression=Attr("business_id").eq(bid), Select="COUNT"
                ).get("Count", 0)
            except Exception:
                products_count = None

            # Clientes y órdenes (order_groups distintos) del negocio
            customers_count, orders_count = 0, 0
            try:
                custs = customers_table.scan(
                    FilterExpression=Attr("business_id").eq(bid)
                ).get("Items", [])
                customers_count = len(custs)
                groups = set()
                for c in custs:
                    for t in c.get("transactions", []) or []:
                        groups.add(t.get("order_group") or t.get("transaction_id"))
                orders_count = len(groups)
            except Exception as e:
                print(json.dumps({"event": "get_businesses.orders", "Error": str(e)}))

            profile = _cognito_profile(user_id)

            result.append(
                {
                    "business_id": bid,
                    "business_name": b.get("business_name", "") or b.get("name", ""),
                    "slug": b.get("business_slug", "") or b.get("slug", ""),
                    "phone": b.get("business_phone", "") or b.get("phone", ""),
                    "user_id": user_id,
                    "owner_email": profile["email"],
                    "cognito_enabled": profile["enabled"],
                    "cognito_status": profile["user_status"],
                    "cognito_groups": profile["groups"],
                    "subscription": profile["custom"],  # atributos custom (suscripción/plan/etc.)
                    "subscription_status": profile["custom"].get("transaction_status", ""),
                    "owner_since": profile["created_at"],
                    "products_count": products_count,
                    "customers_count": customers_count,
                    "orders_count": orders_count,
                    "create_date": b.get("create_date", ""),
                }
            )

        result.sort(key=lambda x: (x.get("create_date") or ""), reverse=True)
        return _resp(200, result)
    except Exception as e:
        print(json.dumps({"event": "get_businesses", "Error": str(e), "trace": traceback.format_exc()}))
        return _resp(500, {"message": str(e)})


# ----------------- Sugerencias (todas) -----------------
def get_all_suggestions():
    try:
        items = suggestions_table.scan().get("Items", [])
        mapped = [
            {
                "suggestion_id": i.get("suggestion_id", ""),
                "title": i.get("title", ""),
                "description": i.get("description", ""),
                "type": i.get("type", "other"),
                "status": i.get("status", "nueva"),
                "admin_notes": i.get("admin_notes", ""),
                "business_id": i.get("business_id", ""),
                "business_name": i.get("business_name", ""),
                "email": i.get("email", ""),
                "user_id": i.get("user_id", ""),
                "create_date": i.get("create_date", ""),
                "update_date": i.get("update_date", ""),
            }
            for i in items
        ]
        mapped.sort(key=lambda x: (x.get("create_date") or ""), reverse=True)
        return _resp(200, mapped)
    except Exception as e:
        print(json.dumps({"event": "get_all_suggestions", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def update_suggestion_status(event, editor="root"):
    try:
        body = json.loads(event.get("body", "{}"))
        suggestion_id = body.get("suggestion_id", "")
        status = (body.get("status", "") or "").strip().lower()
        admin_notes = body.get("admin_notes", None)

        if not suggestion_id:
            return _resp(400, {"message": "Falta suggestion_id"})
        if status and status not in VALID_STATUSES:
            return _resp(400, {"message": f"Estado inválido: {status}"})

        existing = suggestions_table.get_item(
            Key={"suggestion_id": suggestion_id}
        ).get("Item")
        if not existing:
            return _resp(404, {"message": "Sugerencia no encontrada"})

        expr = ["update_date=:ud", "update_user=:uu"]
        vals = {":ud": _now(), ":uu": editor}
        if status:
            expr.append("#st=:st")
            vals[":st"] = status
        if admin_notes is not None:
            expr.append("admin_notes=:an")
            vals[":an"] = str(admin_notes)[:2000]

        names = {"#st": "status"} if status else None
        kwargs = {
            "Key": {"suggestion_id": suggestion_id},
            "UpdateExpression": "SET " + ", ".join(expr),
            "ExpressionAttributeValues": vals,
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names

        updated = suggestions_table.update_item(**kwargs).get("Attributes", {})
        return _resp(
            200,
            {
                "message": "Sugerencia actualizada",
                "status": updated.get("status", status),
                "admin_notes": updated.get("admin_notes", ""),
            },
        )
    except Exception as e:
        print(json.dumps({"event": "update_suggestion_status", "Error": str(e)}))
        return _resp(500, {"message": str(e)})