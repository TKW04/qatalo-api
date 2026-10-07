"""
Eliminación de cuenta del dueño (App Store 5.1.1(v)).

DELETE /{alias}/users/me   body: {"confirm": "ELIMINAR"}
  Authorization: <ID token de Cognito obtenido en una re-autenticación reciente>

Diseño (la Lambda tiene timeout corto → borrado por pasos reanudables):

  1. La petición verifica el token (firma RS256 + emisor + expiración + auth_time reciente),
     crea (o retoma) un registro de borrado y ejecuta pasos mientras quede tiempo.
  2. Si no termina, se re-invoca a sí misma en asíncrono ({"source": "qatalo.account_deletion"})
     y cada invocación sigue donde quedó. La regla diaria (EventBridge) retoma lo que haya quedado
     colgado y reintenta la cancelación de Paddle fallida.
  3. Todos los pasos son idempotentes (re-ejecutarlos no hace daño).

Pasos (en orden):
  deactivate      marca los negocios con account_deletion_pending → el catálogo público responde 404
  cognito         AdminDeleteUser (el usuario ya no puede iniciar sesión)
  notify          correo "Tu cuenta fue eliminada" (solo si la invocación trae el destinatario)
  paddle          cancela la suscripción con effective_from=immediately (sin reembolso). Si falla NO
                  bloquea el borrado: queda paddle.state = retry/manual en el registro.
  retention       copia mínima de los comprobantes fiscales con NCF (ver _build_retention_items)
  customers       borra clientes (+ comprobantes de pago en S3 del cliente)
  offers, payment_methods, categories, suggestions
  products        borra productos (+ imágenes propias en S3)
  business        borra logo/fuentes/facturas propias en S3 y el ítem del negocio
  finalize        status=completed; se eliminan del registro los identificadores ya innecesarios

Dónde viven los registros (sin tablas nuevas): en `qatalo.customers`, con claves que no chocan con
un customer_id (UUID) y SIN atributo business_id, para que ninguna consulta existente los vea:
  account_deletion#<deletion_id>                  registro de borrado (estado / auditoría)
  legal_retention#<business_id>#<order_group>     comprobante fiscal retenido (retained_for_legal)

S3: solo se borran objetos del bucket BUCKET_NAME cuya clave pertenece al dueño (ver _is_owned_key).
Nunca se borra una URL "prestada" de otra cuenta (p. ej. una cuenta demo que reutiliza imágenes).

Los logs solo llevan deletion_id (hash del sub), nombres de paso y contadores: sin correos ni nombres.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import unquote, urlparse

import boto3
from boto3.dynamodb.conditions import Attr

REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=REGION)
business_table = dynamodb.Table("qatalo.business")
customers_table = dynamodb.Table("qatalo.customers")
products_table = dynamodb.Table("qatalo.products")
categories_table = dynamodb.Table("qatalo.categories")
payment_methods_table = dynamodb.Table("qatalo.payment_methods")
offers_table = dynamodb.Table("qatalo.offers")
suggestions_table = dynamodb.Table("qatalo.suggestions")
records_table = customers_table  # registros de borrado y de retención (ver docstring)

s3 = boto3.client("s3")
cognito = boto3.client("cognito-idp")
lambda_client = boto3.client("lambda")

USER_POOL_ID = os.environ.get("USER_POOL_ID")
CONFIRM_WORD = "ELIMINAR"
WORKER_SOURCE = "qatalo.account_deletion"
RECORD_PREFIX = "account_deletion#"
RETENTION_PREFIX = "legal_retention#"
MAX_HOPS = 25                 # re-invocaciones encadenadas máximas por petición/barrido
PADDLE_MAX_ATTEMPTS = 3       # luego queda en "manual"
SAFETY_MARGIN_MS = 1200       # no empezar trabajo nuevo con menos tiempo que esto
DEFAULT_BUDGET_MS = 25000     # si no hay context (tests / invocación local)

STEPS = (
    "deactivate", "cognito", "notify", "paddle", "retention", "customers",
    "offers", "payment_methods", "categories", "suggestions", "products",
    "business", "finalize",
)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}

# Estados de suscripción de Paddle que aún pueden cobrar → hay que cancelarlos.
PADDLE_CANCELABLE = ("active", "trialing", "past_due", "paused")


class StepIncomplete(Exception):
    """Se acabó el tiempo a mitad de un paso; se retoma en la siguiente invocación."""


class PaddleError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = str(code)


# ───────────────────────── helpers ─────────────────────────
def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(event, **kw):
    """Log de auditoría: solo identificadores opacos y contadores (sin datos personales)."""
    print(json.dumps({"event": f"account_deletion.{event}", **kw}, default=str))


def deletion_id_for(sub):
    return hashlib.sha256(f"qatalo-account:{sub}".encode()).hexdigest()[:32]


class Budget:
    def __init__(self, context=None, margin_ms=SAFETY_MARGIN_MS):
        remaining = DEFAULT_BUDGET_MS
        try:
            if context is not None and hasattr(context, "get_remaining_time_in_millis"):
                remaining = int(context.get_remaining_time_in_millis())
        except Exception:
            pass
        self.deadline = time.monotonic() * 1000 + remaining
        self.margin = margin_ms

    def left_ms(self):
        return self.deadline - time.monotonic() * 1000

    def ok(self, need_ms=0):
        return self.left_ms() > self.margin + need_ms

    def check(self):
        if not self.ok():
            raise StepIncomplete()


def _token_from_event(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = headers.get("authorization", "") or ""
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    return raw.strip()


def _verify_jwt(token):
    # Reutiliza la verificación RS256 contra el JWKS del User Pool (root.py), sin librerías extra.
    from root import _verify_cognito_jwt
    return _verify_cognito_jwt(token)


def _max_auth_age():
    try:
        return int(os.environ.get("ACCOUNT_DELETION_MAX_AUTH_AGE", "900"))
    except ValueError:
        return 900


def _scan_all(table, filter_expr, budget):
    """Itera ítems de un scan paginado; corta (StepIncomplete) si se acaba el tiempo."""
    kwargs = {"FilterExpression": filter_expr} if filter_expr is not None else {}
    while True:
        budget.check()
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            yield item
        lek = page.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _or_filter(conds):
    expr = None
    for c in conds:
        expr = c if expr is None else (expr | c)
    return expr


# ───────────────────────── registro de borrado ─────────────────────────
def _record_key(deletion_id):
    return {"customer_id": f"{RECORD_PREFIX}{deletion_id}"}


def load_record(deletion_id):
    return records_table.get_item(Key=_record_key(deletion_id)).get("Item")


def _save(record):
    record["updated_at"] = _now_iso()
    records_table.put_item(Item=record)


def _new_record(deletion_id, sub, username, alias, paddle_txn_id, paddle_customer_id, cognito_found):
    return {
        "customer_id": f"{RECORD_PREFIX}{deletion_id}",
        "record_type": "account_deletion",
        "deletion_id": deletion_id,
        "status": "in_progress",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "alias": alias or "",
        # Necesarios mientras dura el proceso; se eliminan al finalizar (ver _finalize).
        "user_sub": sub,
        "cognito_username": username or sub,
        "business_ids": [],
        "business_snapshot": {},
        "steps_done": [],
        "counts": {},
        "email_sent": False,
        "cognito_found": bool(cognito_found),
        "paddle": {
            "state": "pending",
            "attempts": 0,
            "transaction_id": paddle_txn_id or "",
            "customer_id": paddle_customer_id or "",
            "canceled_ids": [],
            "last_error": "",
            "needs_manual": False,
        },
    }


def _create_record_if_absent(record):
    try:
        records_table.put_item(
            Item=record, ConditionExpression="attribute_not_exists(customer_id)"
        )
        return record, True
    except Exception as e:  # ConditionalCheckFailedException → otro intento ya lo creó
        if "ConditionalCheckFailed" not in str(e) and "ConditionalCheckFailed" not in type(e).__name__:
            raise
        return load_record(record["deletion_id"]), False


def _bump(record, key, n):
    counts = record.setdefault("counts", {})
    counts[key] = int(counts.get(key, 0)) + int(n)


# ───────────────────────── S3: pertenencia de claves ─────────────────────────
def _bucket():
    return os.getenv("BUCKET_NAME", "")


def s3_key_from_url(url, bucket=None):
    """Clave S3 si la URL apunta al bucket propio; None si es de otro bucket/dominio."""
    bucket = bucket if bucket is not None else _bucket()
    if not bucket or not isinstance(url, str) or not url.startswith("http"):
        return None
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    path = unquote(p.path or "")
    if host == f"{bucket}.s3.amazonaws.com" or (
        host.startswith(f"{bucket}.s3.") or host.startswith(f"{bucket}.s3-")
    ) and host.endswith(".amazonaws.com"):
        key = path.lstrip("/")
    elif (host == "s3.amazonaws.com" or host.startswith("s3.") or host.startswith("s3-")) \
            and host.endswith(".amazonaws.com") and path.startswith(f"/{bucket}/"):
        key = path[len(bucket) + 2:]
    else:
        return None
    return key or None


def _valid_id(v):
    return isinstance(v, str) and len(v.strip()) >= 8 and "/" not in v


def is_owned_key(key, sub, business_ids=(), product_ids=(), customer_ids=()):
    """
    True solo si la clave fue creada para este dueño:
      - algún segmento empieza por "<sub>_"   (presign: "<folder>/<sub>_<tipo><ext>": logo, fuentes, imágenes)
      - "invoices/<business_id>/..."          (PDF de facturas/recibos del negocio)
      - "business/products/<product_id>_..."  (subida legacy de imágenes de producto)
      - "customers/<customer_id>_receipt_..." (comprobantes de pago de clientes del negocio)
    El sub/IDs son UUID: una clave de otra cuenta nunca cumple estas reglas.
    """
    if not key or not isinstance(key, str):
        return False
    if _valid_id(sub) and any(seg.startswith(f"{sub}_") for seg in key.split("/")):
        return True
    for bid in business_ids or ():
        if _valid_id(bid) and key.startswith(f"invoices/{bid}/"):
            return True
    for pid in product_ids or ():
        if _valid_id(pid) and key.startswith(f"business/products/{pid}_"):
            return True
    for cid in customer_ids or ():
        if _valid_id(cid) and key.startswith(f"customers/{cid}_receipt_"):
            return True
    return False


def _collect_urls(obj, out):
    if isinstance(obj, str):
        if obj.startswith("http"):
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_urls(v, out)
    return out


def _list_keys(prefix, budget):
    bucket = _bucket()
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    keys = []
    while True:
        budget.check()
        page = s3.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in page.get("Contents", []) or [])
        if not page.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = page.get("NextContinuationToken")


def _delete_owned_keys(record, keys, ownership, budget):
    """Borra las claves que pasan is_owned_key; ignora (y cuenta) las ajenas."""
    bucket = _bucket()
    if not bucket:
        return 0
    owned, skipped = [], 0
    for k in dict.fromkeys(keys):  # sin duplicados, preserva orden
        if is_owned_key(k, **ownership):
            owned.append(k)
        else:
            skipped += 1
    if skipped:
        _bump(record, "s3_skipped_not_owned", skipped)
    for i in range(0, len(owned), 1000):
        budget.check()
        chunk = owned[i:i + 1000]
        res = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
        )
        errors = (res or {}).get("Errors") or []
        if errors:
            raise RuntimeError(f"s3_delete_errors:{len(errors)}:{errors[0].get('Code', '')}")
    if owned:
        _bump(record, "s3_objects", len(owned))
    return len(owned)


# ───────────────────────── Paddle ─────────────────────────
def _paddle_conf(alias):
    a = (alias or "").upper()
    key = os.environ.get(f"PADDLE_API_KEY_{a}", "")
    base = os.environ.get(f"PADDLE_API_BASE_{a}", "https://api.paddle.com")
    return key, base.rstrip("/")


def _paddle_request(method, url, key, budget, body=None):
    import requests
    timeout = max(1.0, min(8.0, (budget.left_ms() - SAFETY_MARGIN_MS) / 1000.0))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=timeout)
    else:
        r = requests.post(url, headers=headers, data=json.dumps(body or {}), timeout=timeout)
    return r


def cancel_paddle_subscriptions(alias, transaction_id, customer_id, budget):
    """
    Cancela de inmediato (effective_from=immediately, sin reembolso) toda suscripción viva del dueño.
    Candidatas: custom:transaction_id si es "sub_..." + las suscripciones vivas del customer "ctm_...".
    Devuelve (state, canceled_ids). Lanza PaddleError si algo falla.
    """
    candidates = []
    has_ids = (transaction_id or "").startswith("sub_") or (customer_id or "").startswith("ctm_")
    if not has_ids:
        return "not_applicable", []
    key, base = _paddle_conf(alias)
    if not key:
        raise PaddleError("missing_api_key")
    if (transaction_id or "").startswith("sub_"):
        candidates.append(transaction_id)
    if (customer_id or "").startswith("ctm_"):
        try:
            r = _paddle_request(
                "GET",
                f"{base}/subscriptions?customer_id={customer_id}&status={','.join(PADDLE_CANCELABLE)}&per_page=50",
                key, budget,
            )
        except Exception as e:
            raise PaddleError(f"list_exception:{type(e).__name__}")
        if r.status_code != 200:
            raise PaddleError(f"list_http_{r.status_code}")
        for s in (r.json() or {}).get("data", []) or []:
            if s.get("id") and s.get("id") not in candidates:
                candidates.append(s["id"])

    canceled = []
    for sub_id in candidates:
        budget.check()
        try:
            g = _paddle_request("GET", f"{base}/subscriptions/{sub_id}", key, budget)
        except Exception as e:
            raise PaddleError(f"get_exception:{type(e).__name__}")
        if g.status_code == 404:
            continue
        if g.status_code != 200:
            raise PaddleError(f"get_http_{g.status_code}")
        status = ((g.json() or {}).get("data") or {}).get("status", "")
        if status == "canceled":
            continue
        try:
            c = _paddle_request(
                "POST", f"{base}/subscriptions/{sub_id}/cancel", key, budget,
                body={"effective_from": "immediately"},
            )
        except Exception as e:
            raise PaddleError(f"cancel_exception:{type(e).__name__}")
        if c.status_code != 200:
            code = ""
            try:
                code = ((c.json() or {}).get("error") or {}).get("code", "")
            except Exception:
                pass
            raise PaddleError(f"cancel_http_{c.status_code}{(':' + code) if code else ''}")
        canceled.append(sub_id)
    return ("canceled" if canceled or candidates else "not_applicable"), canceled


def _try_paddle(record, budget):
    p = record.setdefault("paddle", {})
    if p.get("state") in ("canceled", "not_applicable"):
        return
    p["attempts"] = int(p.get("attempts", 0)) + 1
    try:
        state, canceled = cancel_paddle_subscriptions(
            record.get("alias", ""), p.get("transaction_id", ""), p.get("customer_id", ""), budget
        )
        p["state"] = state
        p["canceled_ids"] = list(dict.fromkeys((p.get("canceled_ids") or []) + canceled))
        p["last_error"] = ""
        p["needs_manual"] = False
        _log("paddle", deletion_id=record["deletion_id"], state=state, canceled=len(canceled))
    except StepIncomplete:
        p["attempts"] = int(p["attempts"]) - 1
        raise
    except Exception as e:
        code = e.code if isinstance(e, PaddleError) else f"exception:{type(e).__name__}"
        p["last_error"] = code
        manual = p["attempts"] >= PADDLE_MAX_ATTEMPTS
        p["state"] = "manual" if manual else "retry"
        p["needs_manual"] = manual
        _log("paddle_failed", deletion_id=record["deletion_id"], error=code,
             attempts=p["attempts"], needs_manual=manual)


# ───────────────────────── retención legal ─────────────────────────
def _retention_years():
    try:
        return int(os.environ.get("LEGAL_RETENTION_YEARS", "10"))
    except ValueError:
        return 10


def _dec(v, default="0"):
    try:
        return Decimal(str(v if v not in (None, "") else default))
    except Exception:
        return Decimal(default)


def build_retention_items(customer, business_snapshot, deletion_id, product_mode_lookup):
    """
    Una entrada por comprobante (order_group con NCF asignado) con SOLO lo que figura en el comprobante
    fiscal: NCF, tipo, fecha, montos, ITBIS, emisor (nombre/RNC) y comprador (nombre y RNC si existe).
    No se guarda teléfono, correo, dirección, líneas de producto ni comprobantes de pago.
    """
    from invoice_generator import calc_invoice_totals

    groups = {}
    for t in customer.get("transactions", []) or []:
        if not t.get("ncf_used"):
            continue
        g = t.get("order_group") or t.get("transaction_id") or ""
        if g:
            groups.setdefault(g, []).append(t)
    if not groups:
        return []

    bid = customer.get("business_id", "")
    biz = (business_snapshot or {}).get(bid, {}) or {}
    itbis_rate = biz.get("itbis_rate")
    itbis_rate = Decimal("18") if itbis_rate in (None, "") else _dec(itbis_rate, "18")
    buyer_name = (
        customer.get("full_name")
        or f"{customer.get('given_name', '')} {customer.get('family_name', '')}".strip()
        or "Consumidor final"
    )
    out = []
    for g, txns in groups.items():
        raw = []
        for t in txns:
            raw.append({
                "quantity": t.get("quantity", 1),
                "price": t.get("price", 0),
                "itbis_mode": product_mode_lookup(t.get("product_id", "")),
                "delivery_price": t.get("delivery_price", 0),
                "discount_amount": t.get("discount_amount", 0),
            })
        _, totals = calc_invoice_totals(raw, itbis_rate, True)
        first = txns[0]
        ncf = str(first.get("ncf_used", ""))
        ncf_date = str(first.get("ncf_date", "") or "")
        try:
            issued = datetime.strptime(ncf_date[:10], "%Y-%m-%d")
        except ValueError:
            issued = datetime.now(timezone.utc)
        try:
            until = issued.replace(year=issued.year + _retention_years())
        except ValueError:  # 29 de febrero
            until = issued.replace(year=issued.year + _retention_years(), day=28)
        retain_until = until.strftime("%Y-%m-%d")
        buyer_rnc = (
            first.get("buyer_rnc") or first.get("rnc") or customer.get("rnc")
            or customer.get("buyer_rnc") or ""
        )
        item = {
            "customer_id": f"{RETENTION_PREFIX}{bid}#{g}",
            "record_type": "legal_retention",
            "retained_for_legal": True,
            "deletion_id": deletion_id,
            "issuer_business_id": bid,
            "issuer_name": biz.get("business_name", "") or "",
            "issuer_rnc": biz.get("rnc", "") or "",
            "ncf": ncf,
            "ncf_type": ncf[:3].upper(),
            "invoice_type": first.get("invoice_type", "factura") or "factura",
            "ncf_date": ncf_date,
            "order_ref": str(g)[:8].upper(),
            "currency": ((first.get("payment_method") or {}).get("currency")
                         or first.get("currency") or ""),
            "itbis_rate": itbis_rate,
            "totals": {k: _dec(v) for k, v in totals.items()},
            "buyer_name": buyer_name,
            "retained_at": _now_iso(),
            "retain_until": retain_until,
        }
        if buyer_rnc:
            item["buyer_rnc"] = str(buyer_rnc)
        out.append(item)
    return out


# ───────────────────────── pertenencia de ítems DynamoDB ─────────────────────────
def _owns(item, record, by_user=True):
    bids = set(record.get("business_ids") or [])
    sub = record.get("user_sub") or ""
    item_bid = item.get("business_id") or ""
    if item_bid:
        return item_bid in bids
    return bool(by_user and sub and item.get("user_id") == sub)


def _owner_filter(record, by_user=True):
    conds = [Attr("business_id").eq(b) for b in record.get("business_ids") or []]
    if by_user and record.get("user_sub"):
        conds.append(Attr("user_id").eq(record["user_sub"]))
    return _or_filter(conds)


# ───────────────────────── pasos ─────────────────────────
def _step_deactivate(record, budget, ctx):
    sub = record.get("user_sub") or ""
    bids = list(record.get("business_ids") or [])
    snap = dict(record.get("business_snapshot") or {})
    if sub:
        for b in _scan_all(business_table, Attr("user_id").eq(sub), budget):
            if b.get("user_id") != sub:
                continue
            bid = b.get("business_id")
            if not bid:
                continue
            if bid not in bids:
                bids.append(bid)
            snap.setdefault(bid, {
                "business_name": b.get("business_name", "") or "",
                "rnc": b.get("rnc", "") or "",
                "itbis_rate": b.get("itbis_rate") if b.get("itbis_rate") not in (None, "") else Decimal("18"),
            })
            if not b.get("account_deletion_pending"):
                business_table.update_item(
                    Key={"business_id": bid},
                    UpdateExpression="SET account_deletion_pending = :t, update_date = :u",
                    ExpressionAttributeValues={":t": True, ":u": _now_iso()},
                )
    record["business_ids"] = bids
    record["business_snapshot"] = snap
    _save(record)  # business_ids debe quedar persistido antes de cualquier otro paso
    return True


def _step_cognito(record, budget, ctx):
    if not record.get("cognito_found", True):
        return True
    username = record.get("cognito_username") or record.get("user_sub")
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    except Exception as e:
        if "UserNotFound" not in type(e).__name__ and "UserNotFound" not in str(e):
            raise
    return True


def _step_notify(record, budget, ctx):
    notify = ctx.get("notify") or {}
    if record.get("email_sent") or not notify.get("email"):
        return True  # sin destinatario en esta invocación: no se reintenta (no guardamos el correo)
    ok = False
    try:
        ok = _send_account_deleted_email(notify.get("email"), notify.get("name") or "")
    except Exception as e:
        _log("notify_failed", deletion_id=record["deletion_id"], error=type(e).__name__)
    record["email_sent"] = bool(ok)
    return True


def _send_account_deleted_email(email, name):
    from SendMails import mails
    fn = getattr(mails, "account_deleted_email", None)
    if fn is None:
        return False
    res = fn(to_address=email, to_name=name or email)
    return isinstance(res, dict) and res.get("statusCode") == 200


def _step_paddle(record, budget, ctx):
    _try_paddle(record, budget)
    return True  # nunca bloquea el borrado


def _step_retention(record, budget, ctx):
    if not record.get("business_ids"):
        return True
    cache = {}

    def mode(pid):
        if not pid:
            return "included"
        if pid not in cache:
            p = products_table.get_item(Key={"product_id": pid}).get("Item") or {}
            cache[pid] = p.get("itbis_mode", "included") or "included"
        return cache[pid]

    n = 0
    for c in _scan_all(customers_table, _owner_filter(record, by_user=False), budget):
        if not _owns(c, record, by_user=False) or str(c.get("customer_id", "")).startswith(
                (RECORD_PREFIX, RETENTION_PREFIX)):
            continue
        for item in build_retention_items(c, record.get("business_snapshot"), record["deletion_id"], mode):
            budget.check()
            records_table.put_item(Item=item)  # determinista → re-escribir es idempotente
            n += 1
    record.setdefault("counts", {})["retained_invoices"] = n
    return True


def _step_customers(record, budget, ctx):
    if not record.get("business_ids"):
        return True
    sub = record.get("user_sub") or ""
    bids = record.get("business_ids") or []
    n = 0
    for c in _scan_all(customers_table, _owner_filter(record, by_user=False), budget):
        cid = str(c.get("customer_id", ""))
        if not _owns(c, record, by_user=False) or cid.startswith((RECORD_PREFIX, RETENTION_PREFIX)):
            continue
        budget.check()
        own = {"sub": sub, "business_ids": bids, "customer_ids": [cid]}
        keys = _list_keys(f"customers/{cid}_receipt_", budget) if _valid_id(cid) else []
        keys += [k for k in (s3_key_from_url(u) for u in _collect_urls(c, [])) if k]
        _delete_owned_keys(record, keys, own, budget)
        customers_table.delete_item(Key={"customer_id": cid})
        n += 1
    _bump(record, "customers", n)
    return True


def _make_simple_step(table_attr, key_name, count_key, by_user=True):
    def step(record, budget, ctx):
        table = globals()[table_attr]
        flt = _owner_filter(record, by_user=by_user)
        if flt is None:
            return True
        n = 0
        for it in _scan_all(table, flt, budget):
            if not _owns(it, record, by_user=by_user) or not it.get(key_name):
                continue
            budget.check()
            table.delete_item(Key={key_name: it[key_name]})
            n += 1
        _bump(record, count_key, n)
        return True
    return step


def _step_suggestions(record, budget, ctx):
    sub = record.get("user_sub") or ""
    bids = set(record.get("business_ids") or [])
    flt = _owner_filter(record, by_user=True)
    if flt is None:
        return True
    n = 0
    for it in _scan_all(suggestions_table, flt, budget):
        mine = (sub and it.get("user_id") == sub) or (it.get("business_id") and it.get("business_id") in bids)
        if not mine or not it.get("suggestion_id"):
            continue
        budget.check()
        suggestions_table.delete_item(Key={"suggestion_id": it["suggestion_id"]})
        n += 1
    _bump(record, "suggestions", n)
    return True


def _step_products(record, budget, ctx):
    sub = record.get("user_sub") or ""
    bids = record.get("business_ids") or []
    flt = _owner_filter(record, by_user=True)
    if flt is None:
        return True
    n = 0
    for p in _scan_all(products_table, flt, budget):
        pid = p.get("product_id")
        if not pid or not _owns(p, record, by_user=True):
            continue
        budget.check()
        own = {"sub": sub, "business_ids": bids, "product_ids": [pid]}
        keys = _list_keys(f"business/products/{pid}_", budget) if _valid_id(pid) else []
        keys += [k for k in (s3_key_from_url(u) for u in _collect_urls(p, [])) if k]
        _delete_owned_keys(record, keys, own, budget)
        products_table.delete_item(Key={"product_id": pid})
        n += 1
    _bump(record, "products", n)
    return True


def _step_business(record, budget, ctx):
    sub = record.get("user_sub") or ""
    bids = record.get("business_ids") or []
    own = {"sub": sub, "business_ids": bids}
    keys = []
    if _valid_id(sub):
        for folder in ("business", "products"):
            keys += _list_keys(f"{folder}/{sub}_", budget)
    for bid in bids:
        if _valid_id(bid):
            keys += _list_keys(f"invoices/{bid}/", budget)
    items = []
    for bid in bids:
        it = business_table.get_item(Key={"business_id": bid}).get("Item")
        if it and it.get("user_id") == sub:
            items.append(it)
            keys += [k for k in (s3_key_from_url(u) for u in _collect_urls(it, [])) if k]
    _delete_owned_keys(record, keys, own, budget)
    for it in items:
        budget.check()
        business_table.delete_item(Key={"business_id": it["business_id"]})
    _bump(record, "businesses", len(items))
    return True


def _step_finalize(record, budget, ctx):
    record["status"] = "completed"
    record["completed_at"] = _now_iso()
    # Minimización: lo que ya no hace falta para reintentos/auditoría se elimina del registro.
    for k in ("cognito_username", "business_snapshot"):
        record.pop(k, None)
    p = record.get("paddle") or {}
    if p.get("state") in ("canceled", "not_applicable"):
        p.pop("transaction_id", None)
        p.pop("customer_id", None)
        record.pop("user_sub", None)
    return True


STEP_FUNCS = {
    "deactivate": _step_deactivate,
    "cognito": _step_cognito,
    "notify": _step_notify,
    "paddle": _step_paddle,
    "retention": _step_retention,
    "customers": _step_customers,
    "offers": _make_simple_step("offers_table", "offer_id", "offers", by_user=False),
    "payment_methods": _make_simple_step("payment_methods_table", "payment_method_id", "payment_methods"),
    "categories": _make_simple_step("categories_table", "category_id", "categories"),
    "suggestions": _step_suggestions,
    "products": _step_products,
    "business": _step_business,
    "finalize": _step_finalize,
}


def process(record, budget, notify=None):
    """Ejecuta los pasos pendientes mientras haya tiempo. Devuelve True si terminó."""
    ctx = {"notify": notify}
    done = record.setdefault("steps_done", [])
    for step in STEPS:
        if step in done:
            continue
        if not budget.ok():
            _save(record)
            return False
        try:
            STEP_FUNCS[step](record, budget, ctx)
        except StepIncomplete:
            _save(record)
            _log("paused", deletion_id=record["deletion_id"], step=step)
            return False
        except Exception as e:
            record["last_error"] = f"{step}:{type(e).__name__}"
            _bump(record, "errors", 1)
            _save(record)
            _log("step_failed", deletion_id=record["deletion_id"], step=step, error=type(e).__name__)
            return False
        done.append(step)
        record.pop("last_error", None)
        _save(record)
        _log("step_done", deletion_id=record["deletion_id"], step=step,
             counts=record.get("counts", {}))
    return record.get("status") == "completed"


def _public_status(record):
    p = (record or {}).get("paddle") or {}
    paddle_state = {
        "canceled": "canceled",
        "not_applicable": "not_applicable",
        "retry": "pending_retry",
        "manual": "manual_review",
    }.get(p.get("state"), "pending")
    completed = (record or {}).get("status") == "completed"
    return {
        "status": "completed" if completed else "in_progress",
        "deletion_id": (record or {}).get("deletion_id"),
        "subscription_cancellation": paddle_state,
        "message": (
            "Tu cuenta fue eliminada." if completed else
            "Tu cuenta fue eliminada. Estamos terminando de borrar tus datos; no necesitas hacer nada más."
        ),
    }


def _invoke_continuation(context, deletion_id, hop, notify=None):
    arn = getattr(context, "invoked_function_arn", None)
    if not arn:
        return False
    try:
        payload = {"source": WORKER_SOURCE, "deletion_id": deletion_id, "hop": hop}
        if notify:
            payload["notify"] = notify
        lambda_client.invoke(FunctionName=arn, InvocationType="Event",
                             Payload=json.dumps(payload).encode())
        return True
    except Exception as e:
        _log("continuation_failed", deletion_id=deletion_id, error=type(e).__name__)
        return False


# ───────────────────────── entradas ─────────────────────────
def handle_delete_request(event, context, alias):
    try:
        token = _token_from_event(event)
        claims = _verify_jwt(token) if token else None
        if not claims or not claims.get("sub"):
            return _resp(401, {"error": "unauthorized", "message": "Sesión inválida o expirada"})
        if claims.get("token_use") not in (None, "id", "access"):
            return _resp(401, {"error": "unauthorized", "message": "Sesión inválida o expirada"})
        max_age = _max_auth_age()
        if max_age > 0:
            auth_time = int(claims.get("auth_time") or 0)
            if not auth_time or time.time() - auth_time > max_age:
                return _resp(403, {
                    "error": "reauth_required",
                    "message": "Por seguridad vuelve a ingresar tu contraseña e inténtalo de nuevo.",
                })
        try:
            body = json.loads(event.get("body") or "{}")
        except (TypeError, ValueError):
            body = {}
        if not isinstance(body, dict) or str(body.get("confirm", "")).strip().upper() != CONFIRM_WORD:
            return _resp(400, {
                "error": "confirmation_required",
                "message": f'Para eliminar tu cuenta envía {{"confirm": "{CONFIRM_WORD}"}}.',
            })

        sub = claims["sub"]
        username = claims.get("cognito:username") or claims.get("username") or sub
        deletion_id = deletion_id_for(sub)
        record = load_record(deletion_id)
        if record and record.get("status") == "completed":
            _log("already_completed", deletion_id=deletion_id)
            return _resp(200, _public_status(record))

        attrs, cognito_found = {}, True
        if not record or not record.get("email_sent"):
            try:
                u = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
                attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
            except Exception as e:
                if "UserNotFound" in type(e).__name__ or "UserNotFound" in str(e):
                    cognito_found = False
                elif not record:
                    raise  # sin los IDs de Paddle no empezamos; el cliente reintenta
        if not record:
            record, created = _create_record_if_absent(_new_record(
                deletion_id, sub, username, alias,
                attrs.get("custom:transaction_id", ""), attrs.get("custom:customer_id", ""),
                cognito_found,
            ))
            if created:
                _log("requested", deletion_id=deletion_id)
            if record.get("status") == "completed":
                return _resp(200, _public_status(record))

        notify = None
        if not record.get("email_sent"):
            email = claims.get("email") or attrs.get("email")
            name = " ".join(x for x in (
                claims.get("given_name") or attrs.get("given_name"),
                claims.get("family_name") or attrs.get("family_name"),
            ) if x)
            if email:
                notify = {"email": email, "name": name}

        done = process(record, Budget(context), notify=notify)
        if done:
            _log("completed", deletion_id=deletion_id, counts=record.get("counts", {}),
                 paddle=(record.get("paddle") or {}).get("state"))
            return _resp(200, _public_status(record))
        pending_notify = notify if notify and not record.get("email_sent") and "notify" not in record.get("steps_done", []) else None
        _invoke_continuation(context, deletion_id, 1, pending_notify)
        return _resp(202, _public_status(record))
    except Exception as e:
        _log("request_failed", error=type(e).__name__)
        return _resp(500, {"error": "internal_error",
                           "message": "No pudimos eliminar la cuenta. Inténtalo de nuevo."})


def handle_worker_event(event, context):
    deletion_id = str(event.get("deletion_id", ""))
    hop = int(event.get("hop", 1) or 1)
    record = load_record(deletion_id) if deletion_id else None
    if not record:
        _log("worker_no_record", deletion_id=deletion_id)
        return {"ok": False}
    if record.get("status") == "completed":
        return {"ok": True, "status": "completed"}
    done = process(record, Budget(context), notify=event.get("notify"))
    if not done and hop < MAX_HOPS:
        notify = event.get("notify") if "notify" not in record.get("steps_done", []) else None
        _invoke_continuation(context, deletion_id, hop + 1, notify)
    elif not done:
        _log("worker_hops_exhausted", deletion_id=deletion_id, step_error=record.get("last_error", ""))
    return {"ok": True, "status": record.get("status")}


def resume_pending(context=None):
    """Barrido (regla diaria): retoma borrados colgados y reintenta Paddle en estado retry."""
    budget = Budget(context)
    flt = Attr("record_type").eq("account_deletion")
    resumed = 0
    try:
        for rec in _scan_all(records_table, flt, budget):
            if rec.get("record_type") != "account_deletion":
                continue
            if rec.get("status") != "completed":
                if not process(rec, budget):
                    _invoke_continuation(context, rec["deletion_id"], 1)
                resumed += 1
            elif (rec.get("paddle") or {}).get("state") == "retry" and budget.ok(3000):
                _try_paddle(rec, budget)
                p = rec.get("paddle") or {}
                if p.get("state") in ("canceled", "not_applicable"):
                    p.pop("transaction_id", None)
                    p.pop("customer_id", None)
                    rec.pop("user_sub", None)
                _save(rec)
                resumed += 1
    except StepIncomplete:
        pass
    _log("sweep", resumed=resumed)
    return {"resumed": resumed}
