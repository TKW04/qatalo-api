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


def paddle_routes(event, user_name, method, path):

    # obtener productos
    if path.endswith("/paddle/products") and method.upper() == "GET":
        return get_products()

    # crear-checkout
    if path.endswith("/paddle/checkout") and method.upper() == "POST":
        body = json.loads(event.get("body") or "{}")
        price_id = body.get("price_id")
        email = user_name
        if not price_id:
            return {"statusCode": 400, "body": json.dumps({"error": "missing price_id"})}

        try:
            return create_checkout(price_id, email)
            # return {"statusCode": 200, "body": json.dumps({"checkout_url": checkout_url})}
        except Exception as e:
            return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    # webhook
    if path.endswith("/paddle/webhook") and method.upper() == "POST":
        print("entro webhook")

        raw_body = get_raw_body_from_event(event)
        headers = event.get("headers", {}) or {}
        ok, err = verify_paddle_signature(headers, raw_body, PADDLE_WEBHOOK_SECRET)
        if not ok:
            return {"statusCode": 400, "body": json.dumps({"ok": False, "error": err})}

        # ✅ firma válida: procesa el JSON
        payload = json.loads(raw_body.decode("utf-8"))
        print(payload)
        # ... tu lógica aquí ...
        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    return {"statusCode": 404, "body": "Not found"}


def get_products():
    try:
        # 1. Obtener todos los precios
        prices_resp = requests.get(
            f"{PADDLE_API_BASE}/prices", headers=HEADERS, timeout=10)
        prices_resp.raise_for_status()
        prices_data = prices_resp.json().get("data", [])

        # 2. Combinar precios con productos
        combined = []
        for price in prices_data:
            combined.append({
                "price_id": price.get("id"),
                "product_id": price.get("product_id", ""),
                "product_name": price.get("name", ""),
                "currency": price.get("unit_price", {}).get("currency_code"),
                "unit_price": int(price.get("unit_price").get("amount"))/100 if price.get("unit_price") else 0,
                "billing_cycle": price.get("billing_cycle").get("interval") if price.get("billing_cycle") else None,
                "trial_period_interval": price.get("trial_period", {}).get("interval") if price.get("trial_period") else None,
                "trial_period_frequency": price.get("trial_period", {}).get("frequency") if price.get("trial_period") else None,
                "description": price.get("description", "")
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


def create_checkout(price_id, email):
    resp = requests.post(
        f"{PADDLE_API_BASE}/transactions",
        headers={"Authorization": f"Bearer {PADDLE_API_KEY}", },
        json={
            "items": [
                {"price_id": price_id, "quantity": 1}
            ],
            "customer_email": email,
            "success_url": f"{os.environ.get('FRONTEND_URL', 'https://example.com')}/checkout/success",
            "cancel_url": f"{os.environ.get('FRONTEND_URL', 'https://example.com')}/checkout/cancel",
            "metadata": {"user_id": email}
        }
    )

    checkout_url = resp.json()["data"]
    checkout_url = os.environ.get(
        "PADDLE_PAY_URL", "") + f"/{os.environ.get('PADDLE_HSC_TOKEN', '')}/?transaction_id={checkout_url.get('id')}"

    return {
        "statusCode": 200,
        "body": json.dumps({"checkout_url": checkout_url})
    }

# -----------------------------
# Helper: verificar firma Paddle (según docs)
# -----------------------------


def get_raw_body_from_event(event):
    """Devuelve los bytes EXACTOS que Paddle firmó."""
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    # Importante: NO json.dumps ni normalizar; devolver tal cual:
    return body.encode("utf-8") if isinstance(body, str) else body


def parse_paddle_signature(sig_header: str):
    """
    Soporta espacios y múltiples h1: ej 'ts=1671552777; h1=abc; h1=def'
    Devuelve (ts:int, [h1a, h1b, ...])
    """
    if not sig_header:
        return None, []
    ts = None
    h1_list = []
    for part in sig_header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "ts":
            try:
                ts = int(v)
            except:
                return None, []
        elif k == "h1":
            h1_list.append(v)
    return ts, h1_list


def verify_paddle_signature(headers, raw_body: bytes, secret: str, tolerance_seconds=300):
    # 1) Header (case-insensitive)
    sig_header = headers.get(
        "Paddle-Signature") or headers.get("paddle-signature")
    if not sig_header:
        return False, "no signature header"

    # 2) Parsear ts + TODOS los h1
    ts, h1_list = parse_paddle_signature(sig_header)
    if not ts or not h1_list:
        return False, "mal formato signature header"

    # 3) Tolerancia anti-replay (más realista)
    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        return False, "timestamp fuera de tolerancia"

    # 4) signed_payload = f"{ts}:{raw_body}"
    signed_payload = str(ts).encode("utf-8") + b":" + raw_body

    # 5) HMAC-SHA256 con el secret del destino
    computed = hmac.new(secret.encode("utf-8"),
                        signed_payload, hashlib.sha256).hexdigest()

    # 6) Comparar contra cualquiera de los h1 (rotación de secret)
    for h1 in h1_list:
        if hmac.compare_digest(computed, h1):
            return True, None

    return False, "firma invalida"
