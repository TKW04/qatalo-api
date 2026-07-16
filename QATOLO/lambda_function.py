import base64
import json
import os

import boto3

# from contact_team import contact_team_routes
from contact_team import contact_team_routes
from customers import customers_routes
from payment_methods import payment_methods_routes
from products import products_routes
from business import business_routes
from users import users_routes
from paddle import paddle_routes
from categories import categories_routes
from offers import offers_routes
from delivery_reminder import run_delivery_reminders
from suggestions import suggestions_routes
from root import root_routes

cognito = boto3.client("cognito-idp")
USER_POOL_ID = os.environ.get("USER_POOL_ID")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}

# Estados de Paddle que permiten operar (crear/editar)
ACTIVE_SUB_STATUSES = {"active", "trialing"}

# Rutas de escritura que NO deben bloquearse aunque la suscripción esté inactiva.
# (gestión de la propia suscripción, cambio de contraseña, alta del negocio, etc.)
# Se comparan por substring contra el path.
WRITE_ALLOW_WHEN_INACTIVE = (
    "/paddle",       # todo el flujo de pago/suscripción
    "/users",        # login, forgot/change password, etc.
    "/team",         # contacto
    "/suggestions",  # feedback (opcional; quítalo si prefieres bloquearlo)
)


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _fresh_subscription_status(user_id):
    """Lee custom:transaction_status FRESCO desde Cognito (no del token)."""
    if not user_id:
        return ""
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
        attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
        return attrs.get("custom:transaction_status", "") or ""
    except Exception as e:
        print(json.dumps({"event": "_fresh_subscription_status", "user_id": user_id, "Error": str(e)}))
        return ""


def _is_write(method):
    return method in ("POST", "PUT", "DELETE", "PATCH")


def _needs_subscription(path, method):
    """True si esta petición es una escritura admin que requiere suscripción activa."""
    if not _is_write(method):
        return False  # las lecturas (GET) nunca se bloquean
    # Rutas exentas (pago, users, etc.)
    for allow in WRITE_ALLOW_WHEN_INACTIVE:
        if allow in path:
            return False
    # Rutas públicas del catálogo/cliente final (sin sesión de dueño) → no bloquear.
    # El cliente que hace un pedido no tiene suscripción; solo bloqueamos al DUEÑO.
    # Estas son escrituras que hace el comprador, no el dueño:
    public_customer_writes = (
        "/customers/cart",
        "/customers/orders",           # add/cancel/receipt/cart por token del cliente
        "/customers/access",           # OTP
        "/customers/transactions/cancel",  # cancelación pública (no cancelAdmin)
    )
    for pub in public_customer_writes:
        if pub in path and "cancelAdmin" not in path:
            return False
    # create_customer público (POST /customers exacto) y noTransaction → pedido del cliente
    if path.endswith("/customers") and method == "POST":
        return False
    if path.endswith("/customers/noTransaction") and method == "POST":
        return False
    if path.endswith("/customers/transactions") and method == "POST":
        return False  # subida de comprobante del cliente
    return True


def lambda_handler(event, context):
    try:
        if event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event":
            return run_delivery_reminders()
        headers = event.get('headers', {})
        arn = context.invoked_function_arn
        alias = arn.split(":")[-1]
        auth_header = headers.get(
            'Authorization') or headers.get('authorization')
        token = auth_header
        decoded = ""
        user_name = ""
        user_id = ""
        if token is not None:
            try:
                decoded = decode_jwt_payload(token)
                user_name = decoded.get("email")
                user_id = decoded.get("sub")
            except:
                pass
        path = event.get('rawPath', '')
        method = event.get('requestContext', {}).get(
            'http', {}).get('method', '')

        # --- Endpoint de estado de suscripción (fresco desde Cognito) ---
        if "/subscription/status" in path and method == "GET":
            status = _fresh_subscription_status(user_id)
            return _resp(200, {
                "transaction_status": status,
                "active": status in ACTIVE_SUB_STATUSES,
            })

        # --- Guard de suscripción: bloquea escrituras del dueño si no está activa ---
        if _needs_subscription(path, method):
            status = _fresh_subscription_status(user_id)
            if status not in ACTIVE_SUB_STATUSES:
                return _resp(402, {
                    "error": "subscription_inactive",
                    "transaction_status": status,
                    "message": "Tu suscripción no está activa. Reactívala para continuar.",
                })

        if "/root/" in path:
            return root_routes(path=path, method=method, event=event, alias=alias)
        if "paddle" in path:
            return paddle_routes(event, method=method, path=path, alias=alias)
        if "users" in path:
            return users_routes(path=path, method=method, event=event, alias=alias)
        if "businesses" in path:
            return business_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "categories" in path:
            return categories_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "customers" in path:
            return customers_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "products" in path:
            return products_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "payment_methods" in path:
            return payment_methods_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)

        if "team" in path:
            return contact_team_routes(path=path, method=method, event=event, alias=alias)
        if "offers" in path:
            return offers_routes(path=path, method=method, event=event, user_id=user_id, alias=alias)
        if "suggestions" in path:
            return suggestions_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)

    except Exception as e:
        print(f"Error processing request: {str(e)}")
        return {
            'statusCode': 500,
            'body': str(e)
        }


def decode_jwt_payload(token):
    try:
        payload = token.split('.')[1]
        padded = payload + '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        return json.loads(decoded_bytes)
    except Exception as e:
        print(f"Error decoding JWT payload: {str(e)}")
        return {}