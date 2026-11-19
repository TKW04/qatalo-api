"""Suscripciones de Apple (In-App Purchase) vía RevenueCat.

Escribe en Cognito los mismos atributos que Paddle (custom:transaction_status, transaction_id,
customer_id, due_date) para que el guard 402 y /subscription/status funcionen igual en ambos casos.

- app_user_id de RevenueCat = `sub` de Cognito (la app llama `Purchases.logIn(sub)`). Los ids
  anónimos (`$RCAnonymousID:...`) se ignoran.
- Si REVENUECAT_SECRET_API_KEY está configurada, cada evento se resuelve consultando
  GET /v1/subscribers/{id} (fuente de verdad; no importa el orden de llegada). Si no, se deduce
  del propio evento, descartando eventos obsoletos (expiration_at_ms anterior a la due_date Apple
  ya guardada, p. ej. un EXPIRATION que llega tarde tras un RENEWAL).
- Apple nunca pisa una suscripción Paddle activa: ni la desactiva ni sobrescribe
  transaction_id/customer_id (account_deletion los usa para cancelar Paddle).
- Errores transitorios (RevenueCat/Cognito) → 500 para que RevenueCat reintente.

Variables de entorno (cada una admite override por alias: NOMBRE_{ALIAS}, p. ej. _PROD):
  REVENUECAT_WEBHOOK_AUTH      valor exacto del header Authorization configurado en RevenueCat
  REVENUECAT_SECRET_API_KEY    clave secreta (sk_...) de la REST API v1 (opcional pero recomendada)
  REVENUECAT_ENTITLEMENT       id del entitlement que da acceso (default "qatalo_pro")
"""
import base64
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

cognito = boto3.client("cognito-idp")
USER_POOL_ID = os.environ.get("USER_POOL_ID")

RC_API_BASE = "https://api.revenuecat.com/v1"
ACTIVE_SUB_STATUSES = {"active", "trialing"}
APPLE_PREFIX = "apple:"
RC_CUSTOMER_PREFIX = "rc:"
ANONYMOUS_PREFIX = "$RCAnonymousID:"
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Eventos tras los que el usuario tiene acceso (o lo conserva hasta expiration_at_ms).
GRANTING_EVENTS = {
    "INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE", "UNCANCELLATION", "NON_RENEWING_PURCHASE",
    "SUBSCRIPTION_EXTENDED", "TEMPORARY_ENTITLEMENT_GRANT",
    # Apagar la renovación / problema de cobro: el acceso sigue hasta el vencimiento.
    "CANCELLATION", "BILLING_ISSUE",
}

SYNC_MIN_INTERVAL_S = 10
_last_sync = {}  # rate-limit por contenedor (best effort)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}


class TransientError(Exception):
    """Fallo recuperable (red, 5xx, Cognito): el webhook responde 500 para que RC reintente."""


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _env(name, alias=None, default=""):
    if alias:
        v = os.environ.get(f"{name}_{alias.upper()}")
        if v:
            return v
    return os.environ.get(name, default) or default


def _entitlement(alias=None):
    return _env("REVENUECAT_ENTITLEMENT", alias, "qatalo_pro")


def _now():
    return datetime.now(timezone.utc)


def _fmt(dt):
    """Mismo formato que los ends_at de Paddle (RFC 3339 UTC con microsegundos)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- RevenueCat REST

def fetch_subscriber(app_user_id, alias=None):
    """GET /v1/subscribers/{id}. None si no hay clave secreta configurada."""
    key = _env("REVENUECAT_SECRET_API_KEY", alias)
    if not key:
        return None
    url = f"{RC_API_BASE}/subscribers/{urllib.parse.quote(app_user_id, safe='')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"subscriber": {}}
        raise TransientError(f"revenuecat_http_{e.code}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise TransientError(f"revenuecat_unreachable: {type(e).__name__}") from e


def state_from_subscriber(res, alias=None, now=None):
    """{active, expires_at(datetime|None), product_id, transaction_id} según el entitlement."""
    now = now or _now()
    sub = (res or {}).get("subscriber") or {}
    ent = (sub.get("entitlements") or {}).get(_entitlement(alias))
    if not ent:
        return {"active": False, "expires_at": None, "product_id": None, "transaction_id": None}
    product_id = ent.get("product_identifier")
    exp = _parse(ent.get("expires_date"))
    grace = _parse(ent.get("grace_period_expires_date"))
    if exp and grace and grace > exp:
        exp = grace
    lifetime = ent.get("expires_date") is None
    active = lifetime or (exp is not None and exp > now)
    tx = None
    if product_id:
        s = (sub.get("subscriptions") or {}).get(product_id) or {}
        tx = s.get("original_transaction_id") or s.get("store_transaction_id")
        if not tx:
            nons = (sub.get("non_subscriptions") or {}).get(product_id) or []
            if nons:
                tx = nons[-1].get("store_transaction_id") or nons[-1].get("id")
    return {"active": active, "expires_at": exp, "product_id": product_id, "transaction_id": tx}


def state_from_event(ev, alias=None, now=None):
    """Estado deducido solo del evento; None si el evento no cambia nada."""
    now = now or _now()
    ids = ev.get("entitlement_ids") or ([ev["entitlement_id"]] if ev.get("entitlement_id") else [])
    if ids and _entitlement(alias) not in ids:
        return None
    exp_ms = ev.get("expiration_at_ms")
    exp = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc) if isinstance(exp_ms, (int, float)) else None
    base = {
        "expires_at": exp,
        "product_id": ev.get("product_id"),
        "transaction_id": ev.get("original_transaction_id") or ev.get("transaction_id"),
    }
    t = ev.get("type")
    if t == "EXPIRATION":
        return {**base, "active": False}
    if t in GRANTING_EVENTS:
        # CANCELLATION/BILLING_ISSUE: activo solo mientras no haya vencido.
        return {**base, "active": exp is None or exp > now}
    return None


# ---------------------------------------------------------------- Cognito

def _get_attrs(user_id):
    """Atributos actuales; None si el usuario no existe (cuenta borrada)."""
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
    except Exception as e:
        if "UserNotFound" in type(e).__name__ or "UserNotFound" in str(e):
            return None
        raise TransientError(f"cognito_get_user: {type(e).__name__}") from e
    return {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}


def paddle_sub_active(attrs):
    """True si el usuario tiene una suscripción Paddle (no Apple) vigente.

    No se exige due_date futura: los eventos `active` de Paddle no la actualizan, y Paddle
    notifica por sí mismo cancelación/impago.
    """
    tid = attrs.get("custom:transaction_id", "") or ""
    status = attrs.get("custom:transaction_status", "") or ""
    if not tid or tid.startswith(APPLE_PREFIX) or status not in ACTIVE_SUB_STATUSES:
        return False
    due = _parse(attrs.get("custom:due_date"))
    return due is None or due > _now() or status == "active"


def _is_stale(attrs, state):
    """True si el estado (deducido de un evento) es más antiguo que el estado Apple guardado.

    Los webhooks pueden llegar fuera de orden: un EXPIRATION o RENEWAL atrasado trae una
    expiration_at_ms anterior a la due_date que ya escribió un evento más reciente.
    """
    if not (attrs.get("custom:transaction_id", "") or "").startswith(APPLE_PREFIX):
        return False
    exp = state.get("expires_at")
    stored = _parse(attrs.get("custom:due_date"))
    # Sin fecha en el evento (lifetime) no hay con qué ordenar: se aplica.
    return exp is not None and stored is not None and exp < stored


def apply_state(user_id, state, from_event=False):
    """Escribe el estado Apple en Cognito respetando la convivencia con Paddle.

    `from_event=True` (sin REST API) activa la protección contra eventos fuera de orden.
    Devuelve "saved", "skipped_paddle_active", "skipped_stale_event", "skipped_no_entitlement"
    o "no_user". Lanza TransientError si Cognito falla.
    """
    attrs = _get_attrs(user_id)
    if attrs is None:
        return "no_user"
    if paddle_sub_active(attrs):
        # Paddle sigue mandando (el acceso ya es activo). No se pisan transaction_id/customer_id:
        # account_deletion los necesita (sub_.../ctm_...) para cancelar la suscripción Paddle.
        return "skipped_paddle_active"
    if from_event and _is_stale(attrs, state):
        return "skipped_stale_event"

    exp = state.get("expires_at")
    if state["active"]:
        status = "active"
    elif exp is not None or attrs.get("custom:transaction_id", "").startswith(APPLE_PREFIX):
        status = "expired"
    else:
        # Nunca tuvo el entitlement (sync sin compra): no tocar lo que haya.
        return "skipped_no_entitlement"

    tx = state.get("transaction_id") or state.get("product_id") or "unknown"
    attributes = [
        {"Name": "custom:transaction_id", "Value": f"{APPLE_PREFIX}{tx}"},
        {"Name": "custom:transaction_status", "Value": status},
        {"Name": "custom:customer_id", "Value": f"{RC_CUSTOMER_PREFIX}{user_id}"},
        {"Name": "custom:due_date", "Value": _fmt(exp) if exp else ""},
    ]
    try:
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID, Username=user_id, UserAttributes=attributes)
    except Exception as e:
        raise TransientError(f"cognito_update: {type(e).__name__}") from e
    return "saved"


# ---------------------------------------------------------------- Webhook

def _is_user_id(v):
    return isinstance(v, str) and not v.startswith(ANONYMOUS_PREFIX) and bool(_USER_ID_RE.match(v))


def affected_user_ids(ev):
    if ev.get("type") == "TRANSFER":
        ids = list(ev.get("transferred_from") or []) + list(ev.get("transferred_to") or [])
        return [i for i in dict.fromkeys(ids) if _is_user_id(i)]
    candidates = [ev.get("app_user_id"), ev.get("original_app_user_id"), *(ev.get("aliases") or [])]
    first = next((c for c in candidates if _is_user_id(c)), None)
    return [first] if first else []


def _auth_ok(event, alias):
    expected = _env("REVENUECAT_WEBHOOK_AUTH", alias)
    if not expected:
        return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    got = headers.get("authorization") or ""
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def handle_webhook(event, alias):
    if not _auth_ok(event, alias):
        return _resp(401, {"error": "unauthorized"})
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        ev = (json.loads(raw) or {}).get("event") or {}
    except (ValueError, AttributeError):
        return _resp(400, {"error": "invalid_body"})
    if not isinstance(ev, dict) or not ev.get("type"):
        return _resp(400, {"error": "invalid_body"})

    if ev["type"] == "TEST":
        return _resp(200, {"ok": True, "ignored": "test"})
    users = affected_user_ids(ev)
    if not users:
        return _resp(200, {"ok": True, "ignored": "anonymous"})

    results = {}
    try:
        for uid in users:
            remote = fetch_subscriber(uid, alias)
            # TRANSFER sin REST no trae fechas: no se le aplica la protección por orden.
            from_event = remote is None and ev["type"] != "TRANSFER"
            if remote is not None:
                state = state_from_subscriber(remote, alias)
            elif ev["type"] == "TRANSFER":
                # Sin REST: quien cede pierde el acceso; quien recibe lo verá al sincronizar.
                state = ({"active": False, "expires_at": None, "product_id": ev.get("product_id"),
                          "transaction_id": None}
                         if uid in (ev.get("transferred_from") or []) else None)
            else:
                state = state_from_event(ev, alias)
            results[uid] = (apply_state(uid, state, from_event=from_event)
                            if state is not None else "unchanged")
    except TransientError as e:
        print(json.dumps({"event": "revenuecat.webhook", "type": ev.get("type"), "Error": str(e)}))
        return _resp(500, {"error": "transient"})
    print(json.dumps({"event": "revenuecat.webhook", "type": ev.get("type"), "results": results}))
    return _resp(200, {"ok": True, "results": results})


# ---------------------------------------------------------------- Sync (app)

def handle_sync(user_id, alias):
    """POST /{alias}/subscription/sync: refresca Cognito desde RevenueCat para el usuario."""
    if not user_id:
        return _resp(401, {"error": "unauthorized"})
    if not _env("REVENUECAT_SECRET_API_KEY", alias):
        return _resp(503, {"error": "revenuecat_not_configured"})
    now = time.monotonic()
    last = _last_sync.get(user_id)
    if last is not None and now - last < SYNC_MIN_INTERVAL_S:
        return _resp(429, {"error": "too_many_requests"})
    _last_sync[user_id] = now
    try:
        state = state_from_subscriber(fetch_subscriber(user_id, alias), alias)
        apply_state(user_id, state)
        attrs = _get_attrs(user_id) or {}
    except TransientError as e:
        _last_sync.pop(user_id, None)
        print(json.dumps({"event": "revenuecat.sync", "Error": str(e)}))
        return _resp(502, {"error": "upstream_error"})
    status = attrs.get("custom:transaction_status", "") or ""
    return _resp(200, {"transaction_status": status, "active": status in ACTIVE_SUB_STATUSES})


def revenuecat_routes(event, method, path, user_id, alias):
    if path == f"/{alias}/revenuecat/webhook" and method.upper() == "POST":
        return handle_webhook(event, alias)
    if path == f"/{alias}/subscription/sync" and method.upper() == "POST":
        return handle_sync(user_id, alias)
    return _resp(404, {"error": "not_found"})
