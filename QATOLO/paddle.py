import os
import json
import time
import hmac
import hashlib
import base64
import requests

PADDLE_API_KEY = os.environ['PADDLE_API_KEY']            # sandbox API key
PADDLE_API_BASE = os.environ.get(
    'PADDLE_API_BASE', 'https://sandbox-api.paddle.com')
# endpoint secret key from Paddle
PADDLE_WEBHOOK_SECRET = os.environ['PADDLE_WEBHOOK_SECRET']

HEADERS = {
    "Authorization": f"Bearer {PADDLE_API_KEY}",
    "Content-Type": "application/json"
}

# -----------------------------
# Helper: obtener precio
# -----------------------------


def get_products():
    try:
        # 1. Obtener todos los precios
        prices_resp = requests.get(
            f"{PADDLE_API_BASE}/prices", headers=HEADERS, timeout=10)
        prices_resp.raise_for_status()
        prices_data = prices_resp.json().get("data", [])

        # Si quieres también traer datos de producto detallados, puedes llamar a /products
        products_resp = requests.get(
            f"{PADDLE_API_BASE}/products", headers=HEADERS, timeout=10)
        products_resp.raise_for_status()
        products_data = products_resp.json().get("data", [])

        # Crear un dict para buscar productos por id
        product_lookup = {p["id"]: p for p in products_data}

        # 2. Combinar precios con productos
        combined = []
        for price in prices_data:
            product_id = price.get("product_id")
            product_info = product_lookup.get(product_id, {})
            combined.append({
                "product_id": product_id,
                "product_name": product_info.get("name"),
                "product_description": product_info.get("description"),
                "price_id": price.get("id"),
                "currency": price.get("unit_price", {}).get("currency_code"),
                "unit_price": price.get("unit_price").get("amount") if price.get("unit_price") else None,
                "billing_interval": price.get("billing_interval") or "one_time"
            })

        return {
            "statusCode": 200,
            "body": json.dumps(combined, ensure_ascii=False)
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }

# -----------------------------
# Helper: crear transacción -> checkout.url
# -----------------------------


def create_checkout(price_id, email):
    headers = {
        "Authorization": f"Bearer {PADDLE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "items": [
            {
                "price_id": price_id,
                "quantity": 1
            }
        ],
        "customer": {
            "email": email
        },
        "success_url": "https://tusitio.com/pago-exitoso"
    }

    resp = requests.post(f"{PADDLE_API_BASE}/transactions",
                         headers=headers, json=payload)
    resp.raise_for_status()

    data = resp.json()
    checkout_url = data["data"]["checkout"]["url"]
    return {
        "statusCode": 200,
        "body": json.dumps({"checkout_url": checkout_url})
    }

# -----------------------------
# Helper: verificar firma Paddle (según docs)
# -----------------------------


def verify_paddle_signature(headers, raw_body, secret, tolerance_seconds=5):
    # 1) obtener header
    sig_header = headers.get(
        "Paddle-Signature") or headers.get("paddle-signature")
    if not sig_header:
        return False, "no signature header"

    # 2) parsear ts y h1
    kv = {}
    for part in sig_header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k] = v

    ts = kv.get("ts")
    h1 = kv.get("h1")
    if not ts or not h1:
        return False, "mal formato signature header"

    # 3) comprobar timestamp (protege replay)
    try:
        ts_int = int(ts)
    except:
        return False, "timestamp invalido"
    if abs(time.time() - ts_int) > tolerance_seconds:
        return False, "timestamp fuera de tolerancia"

    # 4) construir signed_payload = "{ts}:{raw_body}"
    # raw_body must be bytes (exact bytes Paddle sent)
    if isinstance(raw_body, str):
        raw_bytes = raw_body.encode("utf-8")
    else:
        raw_bytes = raw_body

    signed_payload = ts.encode("utf-8") + b":" + raw_bytes

    # 5) calcular HMAC-SHA256 con secret, comparar en timing-safe
    computed = hmac.new(secret.encode('utf-8'),
                        signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, h1):
        return False, "firma invalida"

    return True, None


def paddle_routes(event):
    path = event.get("rawPath") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get(
        "method") or event.get("httpMethod", "POST")

    # obtener productos
    if path.endswith("/paddle/products") and method.upper() == "GET":
        return get_products()

    # crear-checkout
    if path.endswith("/paddle/create-checkout") and method.upper() == "POST":
        body = json.loads(event.get("body") or "{}")
        price_id = body.get("price_id")
        email = body.get("email")
        if not price_id:
            return {"statusCode": 400, "body": json.dumps({"error": "missing price_id"})}

        try:
            checkout_url = create_checkout(price_id, email)
            return {"statusCode": 200, "body": json.dumps({"checkout_url": checkout_url})}
        except Exception as e:
            return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    # webhook
    if path.endswith("/paddle/webhook") and method.upper() == "POST":
        # obtener raw body EXACTO
        is_b64 = event.get("isBase64Encoded", False)
        body_raw = base64.b64decode(event["body"]) if is_b64 else (
            event["body"] or "").encode("utf-8")

        ok, reason = verify_paddle_signature(
            event.get("headers", {}), body_raw, PADDLE_WEBHOOK_SECRET)
        if not ok:
            return {"statusCode": 400, "body": json.dumps({"error": "signature_verification_failed", "reason": reason})}

        # parsear JSON y procesar
        payload = json.loads(body_raw.decode("utf-8"))
        # Ejemplo: guardar `event_id` para idempotencia y luego hacer fulfilment si es transaction.completed
        event_type = payload.get("event", {}).get(
            "name") or payload.get("type") or payload.get("event_type")
        event_id = payload.get("event", {}).get(
            "id") or payload.get("event_id") or payload.get("id")

        # TODO: almacenar event_id en DB y verificar duplicados
        # ejemplo rapido: si event_type == "transaction.completed": provisionar user/product

        # responder rápido
        return {"statusCode": 200, "body": json.dumps({"success": True})}

    return {"statusCode": 404, "body": "Not found"}
