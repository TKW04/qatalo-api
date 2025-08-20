import os
import json
import base64
import requests
import boto3
from types import SimpleNamespace
from paddle_billing.Notifications import Secret, Verifier

PADDLE_API_KEY = os.environ['PADDLE_API_KEY']            # sandbox API key
PADDLE_API_BASE = os.environ.get(
    'PADDLE_API_BASE', 'https://sandbox-api.paddle.com')
# endpoint secret key from Paddle
PADDLE_WEBHOOK_SECRET = os.environ['PADDLE_WEBHOOK_SECRET']
cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')

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
        verify_paddle_signature(event)

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
    try:
        data = resp.json()["data"]
        transaction_id = data.get('id')
        update_user(email=email, transaction_id=transaction_id,
                    transaction_status="pending", customer_id="", due_date="")

        checkout_url = os.environ.get(
            "PADDLE_PAY_URL", "") + f"/{os.environ.get('PADDLE_HSC_TOKEN', '')}/?transaction_id={transaction_id}"
    except Exception as e:
        print(f"Error al crear checkout: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
    return {
        "statusCode": 200,
        "body": json.dumps({"checkout_url": checkout_url})
    }


def verify_paddle_signature(event):

    verifier = Verifier(300)
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(body)
    else:
        raw_body = body.encode("utf-8") if isinstance(body, str) else body

    # 2) Headers del request
    headers = event.get("headers", {}) or {}
    normalized_headers = {k.title(): v for k, v in headers.items()}
    # Verifica que exista el header
    if "Paddle-Signature" not in normalized_headers:
        print(f"Headers recibidos: {list(headers.keys())}")
        return {
            "statusCode": 400,
            "body": json.dumps({"ok": False, "error": "faltó Paddle-Signature"})
        }

    # 3) Armar objeto tipo request (el SDK necesita .headers y .body)
    request_like = SimpleNamespace(headers=normalized_headers, body=raw_body)

    try:
        integrity = verifier.verify(
            request_like, Secret(PADDLE_WEBHOOK_SECRET))
    except Exception as e:
        print(f"Error verificando webhook: {e}")
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "verifier exception"})}

    if not integrity:
        print(f"Firma inválida: {integrity.failure_reason}")
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "invalid signature"})}

    # # 5) Firma válida → payload parseado
    payload = json.loads(raw_body.get("data", {}), strict=False)
    status = payload.get("status")

    if status == "completed":
        completed_event(payload)

    # # --- tu lógica de negocio aquí ---
    # event_type = payload.get("event_type") or payload.get(
    #     "event", {}).get("type")
    # print(f"Procesando evento {event_type}")

    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def completed_event(payload):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    # email = get_user_by_transaction_id(transaction_id)
    # due_date = payload.get("billing_period").get("ends_at")
    # update_user(email=email, transaction_id=transaction_id,
    #             transaction_status="completed", customer_id=customer_id, due_date=due_date)

    print(transaction_id)
    print(f"customer_id: {customer_id}")
    # print(f"email: {email}")
    # print(f"due_date: {due_date}")


def get_user_by_transaction_id(transaction_id):
    try:

        response = cognito.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'custom:transaction_id = "{transaction_id}"'
        )
        return response.get("Users", [])[0] if response.get("Users") else {}
    except Exception as e:
        print(json.dumps(
            {"event": "get_user_by_transaction_id", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_user(email, transaction_id, transaction_status, customer_id, due_date):
    try:
        # Ejecutar la actualización en Cognito
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "custom:transaction_id",
                 "Value": transaction_id},
                {"Name": "custom:transaction_status",
                 "Value": transaction_status},
                {"Name": "custom:customer_id", "Value": customer_id},
                {"Name": "custom:due_date", "Value": due_date if due_date else ""}
            ]
        )

    except Exception as e:
        print(json.dumps({"event": "update_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
