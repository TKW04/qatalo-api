import requests
import base64
import io
import re
import traceback
import hmac
import hashlib
import time
import random

import boto3
import json
import os
import uuid

# pyrefly: ignore [missing-import]
from requests_toolbelt.multipart import decoder
from boto3.dynamodb.conditions import Attr
from datetime import datetime
from decimal import Decimal

from SendMails.mails import (
    new_order_create_email,
    order_cancel_email,
    order_create_email,
    order_delivered_email,
    order_receipt_email,
    order_verified_email,
    order_access_code_email
)


dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
cognito = boto3.client("cognito-idp")
CUSTOMER_JWT_SECRET = os.environ.get("CUSTOMER_JWT_SECRET", "")


customers_table = dynamodb.Table("qatalo.customers")
business_table = dynamodb.Table("qatalo.business")
payment_methods_table = dynamodb.Table("qatalo.payment_methods")
products_table = dynamodb.Table("qatalo.products")
USER_POOL_ID = os.environ.get("USER_POOL_ID")
s3 = boto3.client("s3")
FRONT_END_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}
DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ----------------- Helpers -----------------
def _resp(status, body=None):
    out = {"statusCode": status, "headers": CORS}
    if body is not None:
        out["body"] = json.dumps(body, default=str)
    return out


def _now():
    return datetime.now().strftime(DATE_FMT)


def _get_customer(customer_id):
    return customers_table.get_item(Key={"customer_id": customer_id}).get("Item")


def _get_business(business_id):
    return business_table.get_item(Key={"business_id": business_id}).get("Item")


def _find_transaction(transactions, transaction_id):
    return next((t for t in transactions if t.get("transaction_id", "") == transaction_id), None)


def _save_transactions(customer_id, transactions, email):
    customers_table.update_item(
        Key={"customer_id": customer_id},
        UpdateExpression="SET transactions = :t, update_date = :ud, update_user = :uu",
        ExpressionAttributeValues={":t": transactions, ":ud": _now(), ":uu": email},
        ReturnValues="UPDATED_NEW",
    )


def _check_owner(customer, user_id):
    """None si OK; response de error si no es dueño. user_id None = endpoint público."""
    if user_id is None:
        return None
    business = _get_business(customer.get("business_id", ""))
    if not business or business.get("user_id") != user_id:
        return _resp(403, {"message": "No autorizado"})
    return None


def _owner_email(business):
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=business.get("user_id", ""))
        return {a["Name"]: a["Value"] for a in user["UserAttributes"]}.get("email", "")
    except Exception:
        return ""


def _catalog_url(business):
    return f"{FRONT_END_URL}/catalog/{business.get('business_slug', '')}"


def _adjust_stock(product_id, delta, updated_by):
    """delta negativo descuenta, positivo restaura. Ignora si el producto no existe."""
    if not product_id or delta == 0:
        return
    product = products_table.get_item(Key={"product_id": product_id}).get("Item")
    if not product:
        return
    new_qty = int(product.get("quantity", 0)) + delta
    if new_qty < 0:
        new_qty = 0
    products_table.update_item(
        Key={"product_id": product_id},
        UpdateExpression="SET quantity = :q, update_date = :ud, update_user = :uu",
        ExpressionAttributeValues={":q": new_qty, ":ud": _now(), ":uu": updated_by},
    )


def _notify_n8n(customer_phone, transaction_id, status):
    try:
        requests.post(
            url="https://n8n.qatalo.online/webhook/transactionStatus",
            headers={"Content-Type": "application/json"},
            json={"customer_id": customer_phone, "transaction_id": transaction_id, "transaction_status": status},
            timeout=5,
        )
    except Exception as e:
        print(json.dumps({"event": "notify_n8n", "Error": str(e)}))


def _order_details(business, transaction, owner_email, customer_name="", **extra):
    qty = int(transaction.get("quantity", 1) or 1)
    price = Decimal(str(transaction.get("price", 0) or 0))
    details = {
        "business_name": business.get("business_name", "Qatalo"),
        "business_logo_url": business.get("business_logo_url", ""),
        "transaction_id": transaction.get("transaction_id", ""),
        "order_date": transaction.get("create_date", _now()),
        "product_name": transaction.get("product_name", ""),
        "quantity": qty,
        "total_amount": str(price * qty),
        "currency": (transaction.get("payment_method", {}) or {}).get("currency", ""),
        "business_email": owner_email or "Qatalo",
        "business_phone": business.get("business_phone", "Qatalo"),
        "customer_name": customer_name,
    }
    details.update(extra)
    return details


def _map_customer(item):
    return {
        "customer_id": item.get("customer_id", ""),
        "business_id": item.get("business_id", ""),
        "given_name": item.get("given_name", ""),
        "family_name": item.get("family_name", ""),
        "full_name": f"{item.get('given_name', '')} {item.get('family_name', '')}",
        "email": item.get("email", ""),
        "phone": item.get("phone", ""),
        "age": int(item.get("age", 0)),
        "delivery_day": item.get("delivery_day", ""),
        "transactions": item.get("transactions", []),
    }

def _ext_from_content_type(ct):
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get((ct or "").lower(), ".jpg")

def _customer_magic_link(business, customer_id, business_id, email, ttl=604800):
    """Magic link de 7 días que abre 'Mis órdenes' ya autenticado."""
    token = _make_customer_token(
        {"customer_id": customer_id, "business_id": business_id, "email": email, "purpose": "magic"},
        ttl=ttl,
    )
    slug = (business or {}).get("business_slug", "")
    return f"{FRONT_END_URL}/catalog/{slug}#orders-token={token}"

# ---------- Token de sesión del cliente final (HMAC, sin libs) ----------
def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_customer_token(payload, ttl=3600):
    data = {**payload, "exp": int(time.time()) + ttl}
    p = _b64u(json.dumps(data, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(CUSTOMER_JWT_SECRET.encode(), p.encode(), hashlib.sha256).digest())
    return f"{p}.{sig}"


def _verify_customer_token(token):
    try:
        p, sig = token.split(".")
        expected = _b64u(hmac.new(CUSTOMER_JWT_SECRET.encode(), p.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64u_dec(p))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _customer_token_from_event(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = headers.get("authorization", "") or headers.get("x-customer-token", "")
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    return raw.strip()


# ---------- OTP ----------
def _gen_code():
    return f"{random.randint(0, 999999):06d}"


def _hash_code(code):
    return hashlib.sha256(str(code).encode()).hexdigest()


def _find_customer_by_email(business_id, email):
    email = (email or "").strip().lower()
    items = customers_table.scan(FilterExpression=Attr("business_id").eq(business_id)).get("Items", [])
    return next((i for i in items if i.get("email", "").strip().lower() == email), None)


def _public_customer(item, business):
    data = _map_customer(item)
    data["business_name"] = (business or {}).get("business_name", "")
    data["business_logo_url"] = (business or {}).get("business_logo_url", "")
    data["business_phone"] = (business or {}).get("business_phone", "")
    return data

# ----------------- Router -----------------
def customers_routes(path, method, event, user_name, user_id, alias):
    # --- Acceso del cliente final (OTP + token) ---
    if path == f"/{alias}/customers/access/request" and method == "POST":
        return request_access_code(event)
    if path == f"/{alias}/customers/access/verify" and method == "POST":
        return verify_access_code(event)
    if path == f"/{alias}/customers/orders" and method == "GET":
        return get_orders_by_token(event)
    if path == f"/{alias}/customers/orders/add" and method == "POST":
        return add_transaction_by_token(event)
    if path == f"/{alias}/customers/orders/cancel" and method == "POST":
        return cancel_order_by_token(event)
    if path == f"/{alias}/customers/orders/receipt/presign" and method == "POST":
        return presign_receipt(event)
    if path == f"/{alias}/customers/orders/receipt" and method == "POST":
        return save_receipt_by_token(event)

    # Públicos (catálogo / cliente)
    if path == f"/{alias}/customers" and method == "POST":
        return create_customer(event=event)
    if path == f"/{alias}/customers/noTransaction" and method == "POST":
        return create_customer_without_transactions(event=event)
    if path == f"/{alias}/customers/transactions" and method == "POST":
        return upload_receipt(event=event)
    if path == f"/{alias}/customers/transactions/cancel" and method == "POST":
        return cancel_transaction(event=event, user_id=None)
    # n8n
    if path == f"/{alias}/customers/n8n" and method == "POST":
        return create_customer_without_transactions(event=event)
    if path == f"/{alias}/customers/transactions/add/n8n" and method == "POST":
        return add_transaction_n8n(event=event)
    if path == f"/{alias}/customers/transactions/upload/n8n" and method == "POST":
        return upload_receipt(event=event)
    if "/customers/transactions/get/n8n" in path and method == "GET":
        m = re.fullmatch(rf"/{alias}/customers/transactions/get/n8n/([^/]+)", path)
        if m:
            cid, tid = m.group(1).split("|")[0], m.group(1).split("|")[1]
            return get_customer_transaction_n8n(transaction_id=tid, customer_id=cid)

    # Admin (requieren dueño)
    if path == f"/{alias}/customers" and method == "GET":
        return get_customers_by_user_id(user_id=user_id)
    if path == f"/{alias}/customers/transactions/update" and method == "PUT":
        return update_transaction(event=event, user_id=user_id)
    if path == f"/{alias}/customers/transactions/add" and method == "POST":
        return add_transaction(event=event, user_id=user_id)
    if path == f"/{alias}/customers/transactions/delete" and method == "DELETE":
        return delete_transaction(event=event, user_id=user_id)
    if path == f"/{alias}/customers/transactions/approve" and method == "POST":
        return approve_transaction(event=event, user_id=user_id)
    if path == f"/{alias}/customers/transactions/cancelAdmin" and method == "POST":
        return cancel_transaction(event=event, user_id=user_id)
    if path == f"/{alias}/customers/transactions/delivered" and method == "POST":
        return delivered_transaction(event=event, user_id=user_id)

    m = re.fullmatch(rf"/{alias}/customers/phone/([^/]+)", path)
    if m:
        phone, business_id = m.group(1).split("|")[0], m.group(1).split("|")[1]
        return get_customer_by_phone(phone=phone, business_id=business_id)

    m = re.fullmatch(rf"/{alias}/customers/([^/]+)", path)
    if m:
        customer_id = m.group(1)
        if method == "PUT":
            return update_customer(event=event, user_name=user_name, customer_id=customer_id, user_id=user_id)
        if method == "DELETE":
            return delete_customer(customer_id=customer_id, user_id=user_id)
        if method == "GET":
            return get_customer_transaction(customer_id=customer_id)

    return _resp(404, {"message": "Ruta no encontrada"})


# ----------------- Lectura -----------------
def get_customers_by_user_id(user_id):
    try:
        biz = business_table.scan(FilterExpression=Attr("user_id").eq(user_id)).get("Items", [])
        customers = []
        if biz:
            business_id = biz[0].get("business_id", "")
            items = customers_table.scan(FilterExpression=Attr("business_id").eq(business_id)).get("Items", [])
            customers = sorted([_map_customer(i) for i in items], key=lambda x: x["full_name"])
        return _resp(200, customers)
    except Exception as e:
        print(json.dumps({"event": "get_customers", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def get_customer_by_phone(phone, business_id):
    try:
        items = customers_table.scan(FilterExpression=Attr("business_id").eq(business_id)).get("Items", [])
        p = phone.strip().replace(" ", "").replace("-", "").replace("+1", "").replace("(", "").replace(")", "")
        if p.startswith("1"):
            p = p[1:]
        customer = None
        for item in items:
            if p in item.get("phone", ""):
                customer = _map_customer(item)
                break
        return _resp(200, customer)
    except Exception as e:
        print(json.dumps({"event": "get_customer_by_phone", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def get_customer_transaction(customer_id):
    try:
        item = _get_customer(customer_id)
        if not item:
            return _resp(404, {"message": "Cliente no encontrado"})
        business = _get_business(item.get("business_id", "")) or {}
        customer = _map_customer(item)
        customer["business_logo_url"] = business.get("business_logo_url", "")
        return _resp(200, customer)
    except Exception as e:
        print(json.dumps({"event": "get_customer_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Clientes -----------------
def create_customer(event):
    """Orden desde el catálogo público (crea cliente si no existe + transacción + emails)."""
    try:
        body = json.loads(event.get("body", "{}"))
        business_id = body.get("business_id", "")
        email = body.get("email", "").strip()
        items = customers_table.scan(FilterExpression=Attr("business_id").eq(business_id)).get("Items", [])
        existing = next((i for i in items if i.get("email", "") == email), None)
        if not existing:
            return create_customer_transaction(body=body)

        customer_id = existing.get("customer_id", "")
        transactions = existing.get("transactions", [])
        transaction = body.get("transaction", {})
        if transaction and transaction.get("product_id", "") != "":
            pm = transaction.get("payment_method", {})
            payment_method = payment_methods_table.get_item(
                Key={"payment_method_id": pm.get("payment_method_id", "")}
            ).get("Item", {})
            transactions.append({
                "transaction_id": str(uuid.uuid4()),
                "product_id": transaction.get("product_id", ""),
                "product_name": transaction.get("product_name", ""),
                "quantity": transaction.get("quantity", 1),
                "price": Decimal(str(transaction.get("price", 0) or 0)),
                "status": "Pendiente de pago",
                "accept_terms": transaction.get("accept_terms", False),
                "payment_method": payment_method,
                "delivery_day": transaction.get("delivery_day", ""),
                "create_date": _now(),
                "create_user": email,
            })
        customers_table.update_item(
            Key={"customer_id": customer_id},
            UpdateExpression="SET given_name=:g, family_name=:f, email=:e, phone=:p, age=:a, transactions=:t, update_date=:ud, update_user=:uu",
            ExpressionAttributeValues={
                ":g": body.get("given_name", ""), ":f": body.get("family_name", ""),
                ":e": email, ":p": body.get("phone", ""), ":a": int(body.get("age", 0) or 0),
                ":t": transactions, ":ud": _now(), ":uu": email,
            },
        )
        try:
            business = _get_business(business_id) or {}
            owner_email = _owner_email(business)
            new_tx = transactions[-1] if transactions else {}
            magic = _customer_magic_link(business, customer_id, business_id, email)
            customer_details = _order_details(
                business, new_tx, owner_email,
                customer_name=f"{body.get('given_name')} {body.get('family_name', '')}",
                upload_link=magic,
            )
            owner_details = {**customer_details, "upload_link": f"{FRONT_END_URL}/admin"}
            new_order_create_email(owner_email or "Qatalo", to_name=business.get("business_name", "Qatalo"), order_details=owner_details)
            return order_create_email(email, to_name=customer_details["customer_name"], order_details=customer_details)
        except Exception as notify_err:
            print(json.dumps({"event": "create_customer.notify", "Error": str(notify_err)}))
        return _resp(200, {"message": "Orden creada", "customer_id": customer_id})
    except Exception as e:
        print(json.dumps({"event": "create_customer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def create_customer_transaction(body):
    try:
        business_id = body.get("business_id", "")
        transaction = body.get("transaction", {})
        pm = transaction.get("payment_method", {})
        payment_method = payment_methods_table.get_item(
            Key={"payment_method_id": pm.get("payment_method_id", "")}
        ).get("Item", {})
        customer_id = str(uuid.uuid4())
        tx = {
            "transaction_id": str(uuid.uuid4()),
            "product_id": transaction.get("product_id", ""),
            "product_name": transaction.get("product_name", ""),
            "quantity": transaction.get("quantity", 1),
            "price": Decimal(str(transaction.get("price", 0) or 0)),
            "status": "Pendiente de pago",
            "accept_terms": transaction.get("accept_terms", False),
            "payment_method": payment_method,
            "delivery_day": transaction.get("delivery_day", ""),
            "locality": transaction.get("locality", ""),
            "create_date": _now(),
            "create_user": body.get("email", ""),
        }
        customers_table.put_item(Item={
            "customer_id": customer_id, "business_id": business_id,
            "given_name": body.get("given_name", ""), "family_name": body.get("family_name", ""),
            "email": body.get("email", ""), "phone": body.get("phone", ""),
            "age": int(body.get("age", 0) or 0), "transactions": [tx],
            "create_date": _now(), "create_user": body.get("email", ""),
            "update_date": _now(), "update_user": body.get("email", ""),
        })
        try:
            business = _get_business(business_id) or {}
            owner_email = _owner_email(business)
            magic = _customer_magic_link(business, customer_id, business_id, body.get("email", ""))
            customer_details = _order_details(
                business, tx, owner_email,
                customer_name=f"{body.get('given_name')} {body.get('family_name', '')}",
                upload_link=magic,
            )
            owner_details = {**customer_details, "upload_link": f"{FRONT_END_URL}/admin"}
            new_order_create_email(owner_email or "Qatalo", to_name=business.get("business_name", "Qatalo"), order_details=owner_details)
            return order_create_email(body.get("email"), to_name=customer_details["customer_name"], order_details=customer_details)
        except Exception as notify_err:
            print(json.dumps({"event": "create_customer_transaction.notify", "Error": str(notify_err)}))
        return _resp(200, {"message": "Orden creada", "customer_id": customer_id})
    except Exception as e:
        print(json.dumps({"event": "create_customer_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def create_customer_without_transactions(event):
    try:
        body = json.loads(event.get("body", "{}"))
        business_id = body.get("business_id", "")
        email = body.get("email", "").strip()
        items = customers_table.scan(FilterExpression=Attr("business_id").eq(business_id)).get("Items", [])
        if any(i.get("email", "") == email for i in items):
            return _resp(409, {"message": "Cliente ya existe"})
        customer_id = str(uuid.uuid4())
        customers_table.put_item(Item={
            "customer_id": customer_id, "business_id": business_id,
            "given_name": body.get("given_name", ""), "family_name": body.get("family_name", ""),
            "email": email, "phone": body.get("phone", ""), "age": int(body.get("age", 0)),
            "transactions": [], "create_date": _now(), "create_user": email,
            "update_date": _now(), "update_user": email,
        })
        return _resp(200, {"message": "Cliente creado exitosamente", "customer_id": customer_id})
    except Exception as e:
        print(json.dumps({"event": "create_customer_without_transactions", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def update_customer(event, user_name, customer_id, user_id):
    try:
        customer = _get_customer(customer_id)
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        body = json.loads(event.get("body", "{}"))
        customers_table.update_item(
            Key={"customer_id": customer_id},
            UpdateExpression="SET given_name=:g, family_name=:f, email=:e, phone=:p, user_id=:u, update_date=:ud, update_user=:uu",
            ExpressionAttributeValues={
                ":g": body.get("given_name", ""), ":f": body.get("family_name", ""),
                ":e": body.get("email", ""), ":p": body.get("phone", ""),
                ":u": user_id, ":ud": _now(), ":uu": user_name,
            },
            ReturnValues="UPDATED_NEW",
        )
        return _resp(200, {"message": "Cliente actualizado correctamente"})
    except Exception as e:
        print(json.dumps({"event": "update_customer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def delete_customer(customer_id, user_id):
    try:
        customer = _get_customer(customer_id)
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        customers_table.delete_item(Key={"customer_id": customer_id})
        return _resp(200, {"message": "Cliente eliminado correctamente"})
    except Exception as e:
        print(json.dumps({"event": "delete_customer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Transacciones -----------------
def approve_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        if tx.get("status") == "Aprobada":
            return _resp(200, {"message": "La transacción ya estaba aprobada"})
        tx["status"] = "Aprobada"
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        _adjust_stock(tx.get("product_id", ""), -int(tx.get("quantity", 1) or 1), customer.get("email", ""))
        business = _get_business(customer.get("business_id", "")) or {}
        owner_email = _owner_email(business)
        _notify_n8n(customer.get("phone", ""), tx.get("transaction_id", ""), "Aprobada")
        return order_verified_email(
            customer.get("email"),
            to_name=f"{customer.get('given_name')} {customer.get('family_name')}",
            order_details=_order_details(business, tx, owner_email, business_website_url=_catalog_url(business)),
        )
    except Exception as e:
        print(json.dumps({"event": "approve_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def delivered_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        tx["status"] = "Entregada"  # el stock ya se descontó al aprobar
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        business = _get_business(customer.get("business_id", "")) or {}
        owner_email = _owner_email(business)
        return order_delivered_email(
            customer.get("email"),
            to_name=f"{customer.get('given_name')} {customer.get('family_name')}",
            order_details=_order_details(business, tx, owner_email, business_website_url=_catalog_url(business)),
        )
    except Exception as e:
        print(json.dumps({"event": "delivered_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def cancel_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        prev_status = tx.get("status")
        tx["status"] = "Cancelada"
        tx["cancellation_reason"] = body.get("cancellation_reason", "No especificada")
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        if prev_status in ("Aprobada", "Entregada"):  # restaurar stock que se había descontado
            _adjust_stock(tx.get("product_id", ""), int(tx.get("quantity", 1) or 1), customer.get("email", ""))
        business = _get_business(customer.get("business_id", "")) or {}
        owner_email = _owner_email(business)
        return order_cancel_email(
            customer.get("email"),
            to_name=f"{customer.get('given_name')} {customer.get('family_name')}",
            order_details=_order_details(
                business, tx, owner_email,
                cancellation_date=_now(),
                cancellation_reason=tx["cancellation_reason"],
                business_website_url=_catalog_url(business),
            ),
        )
    except Exception as e:
        print(json.dumps({"event": "cancel_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def update_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        tx["delivery_day"] = body.get("delivery_day", tx.get("delivery_day", ""))
        tx["price"] = Decimal(str(body.get("price", tx.get("price", 0)) or 0))
        tx["quantity"] = body.get("quantity", tx.get("quantity", 1))
        tx["product_id"] = body.get("product_id", tx.get("product_id", ""))
        tx["product_name"] = body.get("product_name", tx.get("product_name", ""))
        tx["payment_method"] = body.get("payment_method", tx.get("payment_method", {}))
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        return _resp(200, {"message": "Transacción actualizada correctamente"})
    except Exception as e:
        print(json.dumps({"event": "update_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def add_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = customer.get("transactions", [])
        transactions.append({
            "transaction_id": str(uuid.uuid4()),
            "product_id": body.get("product_id", ""),
            "product_name": body.get("product_name", ""),
            "quantity": body.get("quantity", 1),
            "price": Decimal(str(body.get("price", 0) or 0)),
            "status": "Pendiente de pago",
            "accept_terms": True,
            "payment_method": body.get("payment_method", {}),
            "delivery_day": body.get("delivery_day", ""),
            "locality": body.get("locality", ""),
            "create_date": _now(),
            "create_user": customer.get("email", ""),
        })
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        return _resp(200, {"message": "Transacción creada correctamente"})
    except Exception as e:
        print(json.dumps({"event": "add_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def delete_transaction(event, user_id=None):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        err = _check_owner(customer, user_id)
        if err:
            return err
        transactions = [t for t in customer.get("transactions", []) if t.get("transaction_id", "") != body.get("transaction_id", "")]
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        return _resp(200, {"message": "Transacción eliminada correctamente"})
    except Exception as e:
        print(json.dumps({"event": "delete_transaction", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Recibo -----------------
def upload_receipt(event):
    try:
        body_bytes = base64.b64decode(event["body"])
        content_type = event["headers"].get("content-type") or event["headers"].get("Content-Type")
        multipart_data = decoder.MultipartDecoder(body_bytes, content_type)
        file_infos, fields = [], {}
        for part in multipart_data.parts:
            headers = part.headers[b"Content-Disposition"].decode()
            name = headers.split('name="')[1].split('"')[0]
            if "filename=" in headers:
                original = headers.split('filename="')[1].split('"')[0]
                if original and len(part.content) > 0:
                    file_infos.append({
                        "content": part.content,
                        "extension": os.path.splitext(original)[1].lower(),
                        "content_type": part.headers.get(b"Content-Type", b"application/octet-stream").decode(),
                    })
            else:
                fields[name] = part.text

        if not file_infos:
            return _resp(400, {"message": "No se recibió ningún archivo"})

        customer_id = fields.get("customer_id", "")
        transaction_id = fields.get("transaction_id", "")
        receipt_url = upload_image(file_infos[0], customer_id=customer_id, transaction_id=transaction_id)
        if not receipt_url:
            return _resp(500, {"message": "No se pudo subir el recibo"})

        customer = _get_customer(customer_id)
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, transaction_id)
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        tx["receipt_url"] = receipt_url
        tx["status"] = "Pendiente de validación"
        tx["amount_paid"] = fields.get("amount_paid", "")
        tx["destiny_account"] = fields.get("destiny_account", "")
        tx["transfer_date"] = fields.get("transfer_date", "")
        tx["reference"] = fields.get("reference", "")
        _save_transactions(customer_id, transactions, customer.get("email", ""))

        business = _get_business(customer.get("business_id", "")) or {}
        owner_email = _owner_email(business)
        return order_receipt_email(
            customer.get("email"),
            to_name=f"{customer.get('given_name')} {customer.get('family_name')}",
            order_details=_order_details(business, tx, owner_email, business_website_url=_catalog_url(business)),
        )
    except Exception as e:
        print(json.dumps({"event": "upload_receipt", "Error": str(e), "trace": traceback.format_exc()}))
        return _resp(500, {"message": str(e)})

def upload_image(file_info, customer_id=None, transaction_id=None):
    try:
        file_name = f"customers/{customer_id}_receipt_{transaction_id}{file_info['extension']}"
        file_url = f"https://{os.getenv('BUCKET_NAME')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{file_name}"
        with io.BytesIO(file_info["content"]) as fileobj:
            s3.upload_fileobj(Fileobj=fileobj, Bucket=os.getenv("BUCKET_NAME"), Key=file_name,
                              ExtraArgs={"ContentType": file_info["content_type"]})
        return file_url
    except Exception as e:
        print(json.dumps({"event": "upload_image", "error": str(e), "trace": traceback.format_exc()}))
        return None

# ----------------- n8n -----------------
def add_transaction_n8n(event):
    try:
        body = json.loads(event.get("body", "{}"))
        customer = _get_customer(body.get("customer_id", ""))
        if not customer:
            return _resp(404, {"message": "Cliente no encontrado"})
        transactions = customer.get("transactions", [])
        transactions.append({
            "transaction_id": str(uuid.uuid4()),
            "product_id": body.get("product_id", ""),
            "product_name": body.get("product_name", ""),
            "quantity": body.get("quantity", 1),
            "price": Decimal(str(body.get("price", 0) or 0)),
            "status": "Pendiente de pago",
            "accept_terms": True,
            "payment_method": body.get("payment_method", {}),
            "delivery_day": body.get("delivery_day", ""),
            "locality": body.get("locality", ""),
            "create_date": _now(),
            "create_user": customer.get("email", ""),
        })
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        return _resp(200, _map_customer(_get_customer(customer["customer_id"])))
    except Exception as e:
        print(json.dumps({"event": "add_transaction_n8n", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def get_customer_transaction_n8n(transaction_id, customer_id):
    try:
        item = _get_customer(customer_id)
        if not item:
            return _resp(404, {"message": "Cliente no encontrado"})
        tx = _find_transaction(item.get("transactions", []), transaction_id)
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})
        return _resp(200, tx)
    except Exception as e:
        print(json.dumps({"event": "get_customer_transaction_n8n", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

# ----------------- Acceso del cliente final -----------------
def request_access_code(event):
    try:
        body = json.loads(event.get("body", "{}"))
        business_id = body.get("business_id", "")
        email = body.get("email", "").strip().lower()
        if not business_id or not email:
            return _resp(400, {"message": "Datos incompletos"})

        customer = _find_customer_by_email(business_id, email)
        if customer:
            code = _gen_code()
            customers_table.update_item(
                Key={"customer_id": customer["customer_id"]},
                UpdateExpression="SET access_code_hash=:h, access_code_expires=:e, access_code_attempts=:a",
                ExpressionAttributeValues={":h": _hash_code(code), ":e": int(time.time()) + 600, ":a": 0},
            )
            business = _get_business(business_id) or {}
            to_name = f"{customer.get('given_name', '')} {customer.get('family_name', '')}".strip() or email
            try:
                order_access_code_email(
                    email,
                    to_name=to_name,
                    code=code,
                    business_name=business.get("business_name", "Qatalo"),
                )
            except Exception as mail_err:
                print(json.dumps({"event": "request_access_code.email", "Error": str(mail_err)}))

        # Respuesta genérica: no revela si el correo existe (anti-enumeración)
        return _resp(200, {"message": "Si el correo tiene órdenes registradas, te enviamos un código de acceso."})
    except Exception as e:
        print(json.dumps({"event": "request_access_code", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def verify_access_code(event):
    try:
        body = json.loads(event.get("body", "{}"))
        business_id = body.get("business_id", "")
        email = body.get("email", "").strip().lower()
        code = str(body.get("code", "")).strip()

        customer = _find_customer_by_email(business_id, email)
        invalid = _resp(401, {"message": "Código inválido o expirado"})
        if not customer or not customer.get("access_code_hash"):
            return invalid
        if int(customer.get("access_code_expires", 0)) < int(time.time()):
            return invalid
        if int(customer.get("access_code_attempts", 0)) >= 5:
            return _resp(429, {"message": "Demasiados intentos. Solicita un código nuevo."})

        if _hash_code(code) != customer.get("access_code_hash"):
            customers_table.update_item(
                Key={"customer_id": customer["customer_id"]},
                UpdateExpression="SET access_code_attempts = if_not_exists(access_code_attempts, :z) + :one",
                ExpressionAttributeValues={":z": 0, ":one": 1},
            )
            return invalid

        # Éxito: limpiar el código
        customers_table.update_item(
            Key={"customer_id": customer["customer_id"]},
            UpdateExpression="REMOVE access_code_hash, access_code_expires, access_code_attempts",
        )
        token = _make_customer_token({
            "customer_id": customer["customer_id"],
            "business_id": business_id,
            "email": email,
        })
        business = _get_business(business_id) or {}
        return _resp(200, {"token": token, "customer": _public_customer(customer, business)})
    except Exception as e:
        print(json.dumps({"event": "verify_access_code", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def _auth_customer(event):
    """Devuelve (customer, business, None) o (None, None, error_response)."""
    payload = _verify_customer_token(_customer_token_from_event(event))
    if not payload:
        return None, None, _resp(401, {"message": "Sesión inválida o expirada"})
    customer = _get_customer(payload.get("customer_id", ""))
    if not customer or customer.get("business_id") != payload.get("business_id"):
        return None, None, _resp(404, {"message": "Cliente no encontrado"})
    business = _get_business(customer.get("business_id", "")) or {}
    return customer, business, None


def get_orders_by_token(event):
    try:
        customer, business, err = _auth_customer(event)
        if err:
            return err
        return _resp(200, _public_customer(customer, business))
    except Exception as e:
        print(json.dumps({"event": "get_orders_by_token", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def add_transaction_by_token(event):
    try:
        customer, business, err = _auth_customer(event)
        if err:
            return err
        body = json.loads(event.get("body", "{}"))
        transaction = body.get("transaction", {})
        if not transaction.get("product_id"):
            return _resp(400, {"message": "Falta el producto"})

        pm = transaction.get("payment_method", {})
        payment_method = payment_methods_table.get_item(
            Key={"payment_method_id": pm.get("payment_method_id", "")}
        ).get("Item", {})

        transactions = customer.get("transactions", [])
        tx = {
            "transaction_id": str(uuid.uuid4()),
            "product_id": transaction.get("product_id", ""),
            "product_name": transaction.get("product_name", ""),
            "quantity": transaction.get("quantity", 1),
            "price": Decimal(str(transaction.get("price", 0) or 0)),
            "status": "Pendiente de pago",
            "accept_terms": transaction.get("accept_terms", True),
            "payment_method": payment_method,
            "delivery_day": transaction.get("delivery_day", ""),
            "locality": transaction.get("locality", ""),
            "create_date": _now(),
            "create_user": customer.get("email", ""),
        }
        transactions.append(tx)
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))

        try:
            owner_email = _owner_email(business)
            magic = _customer_magic_link(business, customer["customer_id"], customer["business_id"], customer.get("email", ""))
            customer_details = _order_details(
                business, tx, owner_email,
                customer_name=f"{customer.get('given_name', '')} {customer.get('family_name', '')}",
                upload_link=magic,
            )
            owner_details = {**customer_details, "upload_link": f"{FRONT_END_URL}/admin"}
            new_order_create_email(owner_email or "Qatalo", to_name=business.get("business_name", "Qatalo"), order_details=owner_details)
            order_create_email(customer.get("email"), to_name=customer_details["customer_name"], order_details=customer_details)
        except Exception as mail_err:
            print(json.dumps({"event": "add_transaction_by_token.email", "Error": str(mail_err)}))

        return _resp(200, {"message": "Orden creada", "transaction_id": tx["transaction_id"]})
    except Exception as e:
        print(json.dumps({"event": "add_transaction_by_token", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def cancel_order_by_token(event):
    try:
        customer, business, err = _auth_customer(event)
        if err:
            return err
        body = json.loads(event.get("body", "{}"))
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})

        prev_status = tx.get("status")
        tx["status"] = "Cancelada"
        tx["cancellation_reason"] = body.get("cancellation_reason", "Cancelada por el cliente")
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))
        if prev_status in ("Aprobada", "Entregada"):
            _adjust_stock(tx.get("product_id", ""), int(tx.get("quantity", 1) or 1), customer.get("email", ""))

        try:
            owner_email = _owner_email(business)
            order_cancel_email(
                customer.get("email"),
                to_name=f"{customer.get('given_name', '')} {customer.get('family_name', '')}",
                order_details=_order_details(
                    business, tx, owner_email,
                    cancellation_date=_now(),
                    cancellation_reason=tx["cancellation_reason"],
                    business_website_url=_catalog_url(business),
                ),
            )
        except Exception as mail_err:
            print(json.dumps({"event": "cancel_order_by_token.email", "Error": str(mail_err)}))

        return _resp(200, {"message": "Orden cancelada"})
    except Exception as e:
        print(json.dumps({"event": "cancel_order_by_token", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def presign_receipt(event):
    try:
        customer, business, err = _auth_customer(event)
        if err:
            return err
        body = json.loads(event.get("body", "{}"))
        transaction_id = body.get("transaction_id", "")
        content_type = body.get("content_type", "image/jpeg")
        if not transaction_id:
            return _resp(400, {"message": "Falta la transacción"})
        if not _find_transaction(customer.get("transactions", []), transaction_id):
            return _resp(404, {"message": "Transacción no encontrada"})

        bucket = os.getenv("BUCKET_NAME")
        key = f"customers/{customer['customer_id']}_receipt_{transaction_id}{_ext_from_content_type(content_type)}"
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=300,
        )
        file_url = f"https://{bucket}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{key}"
        return _resp(200, {"upload_url": upload_url, "file_url": file_url, "key": key})
    except Exception as e:
        print(json.dumps({"event": "presign_receipt", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def save_receipt_by_token(event):
    try:
        customer, business, err = _auth_customer(event)
        if err:
            return err
        body = json.loads(event.get("body", "{}"))
        transactions = customer.get("transactions", [])
        tx = _find_transaction(transactions, body.get("transaction_id", ""))
        if not tx:
            return _resp(404, {"message": "Transacción no encontrada"})

        tx["receipt_url"] = body.get("receipt_url", "")
        tx["status"] = "Pendiente de validación"
        tx["amount_paid"] = body.get("amount_paid", "")
        tx["destiny_account"] = body.get("destiny_account", "")
        tx["transfer_date"] = body.get("transfer_date", "")
        tx["reference"] = body.get("reference", "")
        _save_transactions(customer["customer_id"], transactions, customer.get("email", ""))

        try:
            owner_email = _owner_email(business)
            order_receipt_email(
                customer.get("email"),
                to_name=f"{customer.get('given_name', '')} {customer.get('family_name', '')}",
                order_details=_order_details(business, tx, owner_email, business_website_url=_catalog_url(business)),
            )
        except Exception as mail_err:
            print(json.dumps({"event": "save_receipt_by_token.email", "Error": str(mail_err)}))

        return _resp(200, {"message": "Comprobante recibido"})
    except Exception as e:
        print(json.dumps({"event": "save_receipt_by_token", "Error": str(e)}))
        return _resp(500, {"message": str(e)})