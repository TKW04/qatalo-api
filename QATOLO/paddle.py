import os
import json
import base64
import re
import requests
import boto3
from types import SimpleNamespace
from SendMails.mails import past_due_email
from paddle_billing.Notifications import Secret, Verifier

cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')
FRONT_END_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')


def paddle_routes(event, method, path, alias):

    # obtener productos
    if path == f"/{alias}/paddle/products" and method.upper() == "GET":
        return get_plans(alias=alias)

    match = re.fullmatch(
        rf'/{alias}/paddle/subscription/([^/]+)', path)
    if match:
        subscription_id = match.group(1)
        if method.upper() == "GET":
            return get_subscription(subscription_id=subscription_id, alias=alias)

    # webhook
    if path == f"/{alias}/paddle/webhook" and method.upper() == "POST":
        verify_paddle_signature(event=event, alias=alias)

    return {"statusCode": 404, "body": "Not found"}


def get_plans(alias: str):
    try:
        plans_price_ids = os.environ.get(
            f"PADDLE_PLANS_PRICE_IDS_{alias.upper()}", "")
        PADDLE_API_KEY = os.environ.get(f'PADDLE_API_KEY_{alias.upper()}', '')

        HEADERS = {
            "Authorization": f"Bearer {PADDLE_API_KEY}",
            "Content-Type": "application/json"
        }

        prices = []
        PADDLE_API_BASE = os.environ.get(
            f'PADDLE_API_BASE_{alias.upper()}', 'https://api.paddle.com')
        for price_id in plans_price_ids.split(","):
            prices_resp = requests.get(
                f"{PADDLE_API_BASE}/prices/{price_id}", headers=HEADERS, timeout=10)
            prices_resp.raise_for_status()
            prices_data = prices_resp.json().get("data", {})

            prices.append({
                "price_id": prices_data.get("id"),
                "product_id": prices_data.get("product_id", ""),
                "product_name": prices_data.get("name", ""),
                "currency": prices_data.get("unit_price", {}).get("currency_code"),
                "unit_price": int(prices_data.get("unit_price").get("amount"))/100 if prices_data.get("unit_price") else 0,
                "billing_cycle": prices_data.get("billing_cycle").get("interval") if prices_data.get("billing_cycle") else None,
                "trial_period_interval": prices_data.get("trial_period", {}).get("interval") if prices_data.get("trial_period") else None,
                "trial_period_frequency": prices_data.get("trial_period", {}).get("frequency") if prices_data.get("trial_period") else None,
                "description": prices_data.get("description", "")
            })

        return {
            "statusCode": 200,
            "body": json.dumps(prices, ensure_ascii=False)
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }


def verify_paddle_signature(event, alias: str):

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
        return {
            "statusCode": 400,
            "body": json.dumps({"ok": False, "error": "faltó Paddle-Signature"})
        }

    # 3) Armar objeto tipo request (el SDK necesita .headers y .body)
    request_like = SimpleNamespace(headers=normalized_headers, body=raw_body)

    try:
        PADDLE_WEBHOOK_SECRET = os.environ[f'PADDLE_WEBHOOK_SECRET_{alias.upper()}']
        integrity = verifier.verify(
            request_like, Secret(PADDLE_WEBHOOK_SECRET))
    except Exception as e:
        print(f"Error verificando webhook: {e}")
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "verifier exception"})}

    if not integrity:
        print(f"Firma inválida: {integrity.failure_reason}")
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "invalid signature"})}

    # # 5) Firma válida → payload parseado
    payload = json.loads(raw_body.decode(
        "utf-8") if isinstance(raw_body, bytes) else raw_body)
    data = payload.get('data', {})
    status = data.get('status')

    if status == "active":
        active_event(data, status)
    if status == "canceled":
        canceled_event(data, status)
    if status == "trialing":
        trialing_event(data, status)
    if status == "past_due":
        past_due_event(data, status)
    if status == "paid":
        paid_event(data, "active")
    if status == "paused":
        paused_event(data, status)
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def trialing_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")
    due_date = payload.get("current_billing_period").get("ends_at")
    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=due_date)


def canceled_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")

    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=None)


def active_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")
    # due_date = payload.get("billing_period").get("ends_at")
    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=None)


def paused_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")
    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=None)


def past_due_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")
    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=None)

    user = cognito.admin_get_user(
        UserPoolId=USER_POOL_ID,
        Username=user_id
    )

    # Formatear los atributos
    attrs = {attr['Name']: attr['Value']
             for attr in user['UserAttributes']}
    admin_url = f"{FRONT_END_URL}/login"

    return past_due_email(to_address=attrs.get('email'),
                          to_name=f"{attrs.get('given_name')} {attrs.get('family_name')}",
                          admin_url=admin_url)


def paid_event(payload, status):
    transaction_id = payload.get("id")
    customer_id = payload.get("customer_id")
    user_id = payload.get("custom_data", {}).get("appUserId")
    billing_period = payload.get("billing_period", {})
    due_date = billing_period.get("ends_at") if billing_period else None

    update_user(user_id=user_id, transaction_id=transaction_id,
                transaction_status=status, customer_id=customer_id, due_date=due_date)


def update_user(user_id, transaction_id, transaction_status, customer_id, due_date):
    try:
        if user_id != "d4d85418-c0d1-7094-162f-4fa2ed7c20bb":
            # Ejecutar la actualización en Cognito
            userAttributes = [
                {"Name": "custom:transaction_id",
                    "Value": transaction_id},
                {"Name": "custom:transaction_status",
                "Value": transaction_status},
            {"Name": "custom:customer_id", "Value": customer_id}
            ]
            if due_date is not None:
                userAttributes.append(
                    {"Name": "custom:due_date", "Value": due_date if due_date else ""})

            cognito.admin_update_user_attributes(
                UserPoolId=USER_POOL_ID,
                Username=user_id,
                UserAttributes=userAttributes
            )

    except Exception as e:
        print(json.dumps({"event": "update_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def get_subscription(subscription_id: str, alias: str) -> dict:
    """
    Obtiene los detalles de una suscripción en Paddle Billing.
    """
    try:
        PADDLE_API_BASE = os.environ.get(
            f'PADDLE_API_BASE_{alias.upper()}', 'https://api.paddle.com')
        PADDLE_API_KEY = os.environ.get(
            f'PADDLE_API_KEY_{alias.upper()}', '')

        url = f"{PADDLE_API_BASE}/subscriptions/{subscription_id}"
        headers = {
            "Authorization": f"Bearer {PADDLE_API_KEY}",
            "Content-Type": "application/json"
        }

        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json().get("data", {})
            management_urls = data.get("management_urls", {})
            item = data.get("items", [])[0] if data.get("items", []) else {}
            price = item.get("price", {})
            trials_dates = item.get("trial_dates", {})
            unit_price_overrides = price.get("unit_price_overrides", [])[
                0] if price.get("unit_price_overrides", []) else []
            unit_price = unit_price_overrides.get(
                "unit_price", {}) if unit_price_overrides.get("unit_price", {}) else {}

            result = {
                "subscription_id": data.get("id", ""),
                "customer_id": data.get("customer_id", ""),
                "status": data.get("status", ""),
                "next_bill_date": data.get("next_billed_at", ""),
                "update_url": management_urls.get("update_payment_method", ""),
                "trial_starts_at": trials_dates.get("starts_at", ""),
                "trial_ends_at": trials_dates.get("ends_at", ""),
                "trial_period": price.get("trial_period", {}),
                "billing_cycle": price.get("billing_cycle", {}),
                "price":  int(unit_price.get("amount", 0))/100 if unit_price else 0,
                "currency": unit_price.get("currency_code", "") if unit_price else "",
            }
            return {
                'statusCode': 200,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps(result, default=str)
            }
        else:
            return {
                'statusCode': response.status_code,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': response.text
            }
    except Exception as e:
        print(json.dumps({"event": "get_subscription", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
