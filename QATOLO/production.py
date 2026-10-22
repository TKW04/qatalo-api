"""Bitácora de producción y reporte fabricados vs vendidos.

Rutas (mismo prefijo /{alias}/ que /products, requieren sesión del dueño):
  POST   /production                 {product_id, quantity, date "YYYY-MM-DD", note?}
  GET    /production?from=&to=&product_id=
  DELETE /production/{log_id}
  GET    /production/report?from=&to=
  GET    /reports/dashboard?from=&to=   (dashboard de admin: producidos / vendidos / promesas)

Al registrar producción se SUMA `quantity` al stock del producto (nivel producto, sin
variantes); al borrar un registro se resta (sin bajar de 0).

IMPORTANTE: la tabla `qatalo.production_logs` NO existe por IaC; hay que crearla a mano
(PK business_id, SK log_id):

  aws dynamodb create-table \
    --table-name qatalo.production_logs \
    --attribute-definitions AttributeName=business_id,AttributeType=S AttributeName=log_id,AttributeType=S \
    --key-schema AttributeName=business_id,KeyType=HASH AttributeName=log_id,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --profile BMCargo --region us-east-1

La Lambda necesita además permisos dynamodb:PutItem/GetItem/DeleteItem/Query sobre esa tabla.
"""
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
production_table = dynamodb.Table("qatalo.production_logs")
products_table = dynamodb.Table("qatalo.products")
customers_table = dynamodb.Table("qatalo.customers")
business_table = dynamodb.Table("qatalo.business")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}

# Hora local de RD (UTC-4 fijo), igual que customers.py / offers.py.
RD_TZ = timezone(timedelta(hours=-4), "America/Santo_Domingo")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_QUANTITY = 1_000_000
MAX_NOTE = 500


class ProductionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _now():
    return datetime.now(RD_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _to_int(v):
    try:
        return int(Decimal(str(v)))
    except Exception:
        return 0


def _scan_all(table, **kwargs):
    items = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _query_all(table, **kwargs):
    items = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _get_biz(user_id):
    if not user_id:
        return None
    items = _scan_all(business_table, FilterExpression=Attr("user_id").eq(user_id))
    items = [i for i in items if i.get("user_id") == user_id]
    return items[0] if items else None


def _parse_date(value, field):
    value = (value or "").strip() if isinstance(value, str) else value
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ProductionError(f"'{field}' debe tener formato YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ProductionError(f"'{field}' no es una fecha válida")
    return value


def _parse_range(params):
    params = params or {}
    date_from = params.get("from") or ""
    date_to = params.get("to") or ""
    date_from = _parse_date(date_from, "from") if date_from else ""
    date_to = _parse_date(date_to, "to") if date_to else ""
    if date_from and date_to and date_from > date_to:
        raise ProductionError("'from' no puede ser posterior a 'to'")
    return date_from, date_to


def _in_range(day, date_from, date_to):
    # Solo la parte YYYY-MM-DD: "2026-12-31 23:59:59" o isoformat cuentan dentro de to=2026-12-31
    # (to es inclusivo del día completo).
    day = (day or "")[:10] if isinstance(day, str) else ""
    if not day:
        return False
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _parse_quantity(raw):
    if isinstance(raw, bool) or raw is None or raw == "":
        raise ProductionError("'quantity' debe ser un entero mayor que 0")
    try:
        d = Decimal(str(raw).strip())
    except Exception:
        raise ProductionError("'quantity' debe ser un entero mayor que 0")
    if not d.is_finite() or d != d.to_integral_value() or d <= 0:
        raise ProductionError("'quantity' debe ser un entero mayor que 0")
    if d > MAX_QUANTITY:
        raise ProductionError(f"'quantity' no puede ser mayor que {MAX_QUANTITY}")
    return int(d)


def _map_log(item):
    return {
        "log_id": item.get("log_id", ""),
        "business_id": item.get("business_id", ""),
        "product_id": item.get("product_id", ""),
        "product_name": item.get("product_name", ""),
        "quantity": _to_int(item.get("quantity", 0)),
        "date": item.get("date", ""),
        "note": item.get("note", ""),
        "create_date": item.get("create_date", ""),
    }


def _sort_recent_first(logs):
    return sorted(logs, key=lambda l: (l.get("date", ""), l.get("create_date", "")), reverse=True)


def _business_logs(business_id):
    return _query_all(production_table, KeyConditionExpression=Key("business_id").eq(business_id))


# ----------------- Router -----------------
def production_routes(path, method, event, user_name, user_id, alias):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(403, {"message": "No autorizado"})
        business_id = biz.get("business_id", "")
        params = event.get("queryStringParameters") or {}

        if path == f"/{alias}/production/report" and method == "GET":
            return production_report(business_id, params)
        if path == f"/{alias}/production" and method == "GET":
            return list_production(business_id, params)
        if path == f"/{alias}/production" and method == "POST":
            return create_production(event, business_id, user_name or user_id)
        match = re.fullmatch(rf"/{alias}/production/([^/]+)", path)
        if match and method == "DELETE":
            return delete_production(business_id, match.group(1), user_name or user_id)
        return _resp(404, {"message": "Ruta no encontrada"})
    except ProductionError as e:
        return _resp(e.status, {"message": str(e)})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        print(json.dumps({"event": "production_routes", "Error": str(e)}))
        if code == "ResourceNotFoundException":
            return _resp(503, {"message": "La bitácora de producción no está disponible"})
        return _resp(500, {"message": str(e)})
    except Exception as e:
        print(json.dumps({"event": "production_routes", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Handlers -----------------
def create_production(event, business_id, updated_by):
    try:
        data = json.loads(event.get("body") or "{}")
    except Exception:
        raise ProductionError("Body inválido")
    if not isinstance(data, dict):
        raise ProductionError("Body inválido")

    product_id = data.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        raise ProductionError("'product_id' es requerido")
    product_id = product_id.strip()
    quantity = _parse_quantity(data.get("quantity"))
    date = _parse_date(data.get("date"), "date")
    note = data.get("note") or ""
    if not isinstance(note, str):
        raise ProductionError("'note' debe ser texto")
    note = note.strip()[:MAX_NOTE]

    product = products_table.get_item(Key={"product_id": product_id}).get("Item")
    if not product or product.get("business_id") != business_id:
        raise ProductionError("Producto no encontrado", status=404)

    log = {
        "business_id": business_id,
        "log_id": str(uuid.uuid4()),
        "product_id": product_id,
        "product_name": product.get("product_name", ""),
        "quantity": quantity,
        "date": date,
        "note": note,
        "create_date": _now(),
        "create_user": updated_by or "",
    }
    production_table.put_item(Item=log)

    # Suma atómica; la condición garantiza que el producto sigue siendo del negocio.
    try:
        products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression="ADD quantity :q SET is_available = :av, update_date = :ud, update_user = :uu",
            ConditionExpression="attribute_exists(product_id) AND business_id = :b",
            ExpressionAttributeValues={
                ":q": quantity,
                ":av": "available",
                ":ud": _now(),
                ":uu": updated_by or "",
                ":b": business_id,
            },
        )
    except ClientError as e:
        # Revertir el log para no dejar producción sin stock asociado.
        production_table.delete_item(Key={"business_id": business_id, "log_id": log["log_id"]})
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ProductionError("Producto no encontrado", status=404)
        raise

    return _resp(200, _map_log(log))


def list_production(business_id, params):
    date_from, date_to = _parse_range(params)
    product_id = (params or {}).get("product_id") or ""
    logs = []
    for item in _business_logs(business_id):
        if product_id and item.get("product_id") != product_id:
            continue
        if (date_from or date_to) and not _in_range(item.get("date", ""), date_from, date_to):
            continue
        logs.append(_map_log(item))
    return _resp(200, _sort_recent_first(logs))


def delete_production(business_id, log_id, updated_by):
    log = production_table.get_item(Key={"business_id": business_id, "log_id": log_id}).get("Item")
    if not log:
        raise ProductionError("Registro no encontrado", status=404)

    production_table.delete_item(Key={"business_id": business_id, "log_id": log_id})
    _subtract_stock(log.get("product_id", ""), _to_int(log.get("quantity", 0)), business_id, updated_by)
    return _resp(200, {"message": "Registro eliminado correctamente", "log_id": log_id})


def _subtract_stock(product_id, quantity, business_id, updated_by):
    """Resta `quantity` del stock sin bajar de 0. Si el producto ya no existe, no hace nada."""
    if not product_id or quantity <= 0:
        return
    now = _now()
    try:
        res = products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression="ADD quantity :neg SET update_date = :ud, update_user = :uu",
            ConditionExpression="attribute_exists(product_id) AND business_id = :b AND quantity >= :q",
            ExpressionAttributeValues={
                ":neg": -quantity,
                ":q": quantity,
                ":ud": now,
                ":uu": updated_by or "",
                ":b": business_id,
            },
            ReturnValues="UPDATED_NEW",
        )
        new_qty = _to_int((res or {}).get("Attributes", {}).get("quantity", 1))
        if new_qty > 0:
            return
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    # Stock insuficiente (o quedó en 0): fijar a 0 y marcar no disponible.
    try:
        products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET quantity = :z, is_available = :av, update_date = :ud, update_user = :uu",
            ConditionExpression="attribute_exists(product_id) AND business_id = :b",
            ExpressionAttributeValues={
                ":z": 0,
                ":av": "unavailable",
                ":ud": now,
                ":uu": updated_by or "",
                ":b": business_id,
            },
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def production_report(business_id, params):
    date_from, date_to = _parse_range(params)
    rows = {}

    def row(product_id, name):
        r = rows.get(product_id)
        if r is None:
            r = rows[product_id] = {"product_id": product_id, "product_name": name or "",
                                    "produced": 0, "sold": 0, "active": False}
        elif not r["product_name"] and name:
            r["product_name"] = name
        return r

    for log in _business_logs(business_id):
        if not _in_range(log.get("date", ""), date_from, date_to):
            continue
        pid = log.get("product_id", "")
        if not pid:
            continue
        row(pid, log.get("product_name", ""))["produced"] += _to_int(log.get("quantity", 0))

    customers = _scan_all(customers_table, FilterExpression=Attr("business_id").eq(business_id))
    for customer in customers:
        for tx in customer.get("transactions", []) or []:
            # Las transacciones canceladas no cuentan como vendidas (misma convención que customers.py).
            if tx.get("status") == "Cancelada":
                continue
            if not _in_range(tx.get("create_date", ""), date_from, date_to):
                continue
            pid = tx.get("product_id", "")
            if not pid:
                continue
            row(pid, tx.get("product_name", ""))["sold"] += _to_int(tx.get("quantity", 0))

    # Nombre actual del producto cuando existe (los registros guardan el nombre histórico).
    # "active" = is_available == "available" (Disponible); un producto eliminado cuenta como inactivo.
    if rows:
        products = _scan_all(products_table, FilterExpression=Attr("business_id").eq(business_id))
        for p in products:
            r = rows.get(p.get("product_id", ""))
            if r is None:
                continue
            if p.get("product_name"):
                r["product_name"] = p["product_name"]
            r["active"] = p.get("is_available") == "available"

    items = []
    totals = {"produced": 0, "sold": 0, "difference": 0}
    for r in rows.values():
        r["difference"] = r["produced"] - r["sold"]
        totals["produced"] += r["produced"]
        totals["sold"] += r["sold"]
        items.append(r)
    totals["difference"] = totals["produced"] - totals["sold"]
    items.sort(key=lambda r: (r["product_name"].lower(), r["product_id"]))
    return _resp(200, {"items": items, "totals": totals})


# ----------------- Dashboard de admin (GET /reports/dashboard?from=&to=) -----------------
# Trabaja sobre LÍNEAS de pedido (transacciones), no pedidos. No altera /production/report.
SOLD_STATUSES = ("Aprobada", "Entregada", "Pago Completado")
EXCLUDED_PROMISE_STATUSES = SOLD_STATUSES + ("Cancelada",)


def reports_routes(path, method, event, user_name, user_id, alias):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(403, {"message": "No autorizado"})
        params = event.get("queryStringParameters") or {}
        if path == f"/{alias}/reports/dashboard" and method == "GET":
            return dashboard_report(biz.get("business_id", ""), params)
        return _resp(404, {"message": "Ruta no encontrada"})
    except ProductionError as e:
        return _resp(e.status, {"message": str(e)})
    except Exception as e:
        print(json.dumps({"event": "reports_routes", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def _to_dec(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else Decimal(0)
    except Exception:
        return Decimal(0)


def _num(d):
    d = Decimal(d).quantize(Decimal("0.01"))
    return int(d) if d == d.to_integral_value() else float(d)


def _money(amounts):
    return {cur: _num(v) for cur, v in amounts.items()}


def _line_currency(tx):
    return tx.get("currency") or (tx.get("payment_method", {}) or {}).get("currency", "") or ""


def dashboard_report(business_id, params):
    date_from, date_to = _parse_range(params)

    produced = {}
    for log in _business_logs(business_id):
        if not _in_range(log.get("date", ""), date_from, date_to) and (date_from or date_to):
            continue
        pid = log.get("product_id", "")
        qty = _to_int(log.get("quantity", 0))
        if not pid or qty <= 0:
            continue
        r = produced.setdefault(pid, {"product_id": pid, "product_name": log.get("product_name", "") or "",
                                      "quantity": 0})
        if not r["product_name"] and log.get("product_name"):
            r["product_name"] = log["product_name"]
        r["quantity"] += qty

    sold, promises = {}, {}

    def add(groups, tx, customer, day, with_status):
        qty = _to_int(tx.get("quantity", 0))
        if qty <= 0:
            return
        pid = tx.get("product_id", "")
        label = tx.get("variant_label", "") or ""
        price = _to_dec(tx.get("price", 0))
        cur = _line_currency(tx)
        status = tx.get("status", "") or ""
        g = groups.get((pid, label))
        if g is None:
            g = groups[(pid, label)] = {"product_id": pid, "product_name": tx.get("product_name", "") or "",
                                        "variant_label": label, "quantity": 0, "amount": {}, "lines": []}
            if with_status:
                g["by_status"] = {}
        g["quantity"] += qty
        g["amount"][cur] = g["amount"].get(cur, Decimal(0)) + price * qty
        if with_status:
            g["by_status"][status] = g["by_status"].get(status, 0) + qty
        name = f"{customer.get('given_name', '') or ''} {customer.get('family_name', '') or ''}".strip()
        g["lines"].append({
            "transaction_id": tx.get("transaction_id", ""),
            "order_group": tx.get("order_group", "") or "",
            "customer_name": name,
            "quantity": qty,
            "price": _num(price),
            "currency": cur,
            "status": status,
            "date": day,
        })

    customers = _scan_all(customers_table, FilterExpression=Attr("business_id").eq(business_id))
    for customer in customers:
        if customer.get("business_id") != business_id:
            continue
        for tx in customer.get("transactions", []) or []:
            status = tx.get("status", "")
            if status in SOLD_STATUSES:
                # Respaldo para datos antiguos sin approved_date.
                day = tx.get("approved_date") or tx.get("create_date", "")
                target, with_status = sold, False
            elif status != "Cancelada":
                day = tx.get("create_date", "")
                target, with_status = promises, True
            else:
                continue
            if (date_from or date_to) and not _in_range(day, date_from, date_to):
                continue
            add(target, tx, customer, day or "", with_status)

    def finish(groups):
        out = []
        for g in groups.values():
            g["amount"] = _money(g["amount"])
            g["lines"].sort(key=lambda l: l["date"], reverse=True)
            out.append(g)
        out.sort(key=lambda g: (-g["quantity"], g["product_name"].lower(), g["variant_label"]))
        return out

    def total_amount(groups):
        acc = {}
        for g in groups.values():
            for cur, v in g["amount"].items():
                acc[cur] = acc.get(cur, Decimal(0)) + v
        return _money(acc)

    totals = {
        "produced_units": sum(r["quantity"] for r in produced.values()),
        "sold_units": sum(g["quantity"] for g in sold.values()),
        "sold_amount": total_amount(sold),
        "promises_units": sum(g["quantity"] for g in promises.values()),
        "promises_amount": total_amount(promises),
    }
    produced_list = sorted(produced.values(), key=lambda r: (-r["quantity"], r["product_name"].lower()))
    return _resp(200, {
        "range": {"from": date_from or None, "to": date_to or None},
        "totals": totals,
        "produced": produced_list,
        "sold": finish(sold),
        "promises": finish(promises),
    })
