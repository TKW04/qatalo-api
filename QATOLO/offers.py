import boto3, json, os, uuid, re
from decimal import Decimal
from datetime import datetime
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
offers_table = dynamodb.Table("qatalo.offers")
business_table = dynamodb.Table("qatalo.business")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}


def _resp(s, b):
    return {"statusCode": s, "headers": CORS, "body": json.dumps(b, default=str)}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _get_biz(user_id):
    items = business_table.scan(FilterExpression=Attr("user_id").eq(user_id)).get(
        "Items", []
    )
    return items[0] if items else None


def _map(item):
    return {
        "offer_id": item.get("offer_id", ""),
        "business_id": item.get("business_id", ""),
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "is_active": bool(item.get("is_active", True)),
        "trigger": item.get("trigger", "code"),
        "code": (item.get("code", "") or "").upper(),
        "discount_type": item.get("discount_type", "percentage"),
        "discount_value": float(item.get("discount_value", 0) or 0),
        "applies_to": item.get("applies_to", "all"),
        "product_ids": item.get("product_ids", []) or [],
        "category_ids": item.get("category_ids", []) or [],
        "min_order_amount": float(item.get("min_order_amount", 0) or 0),
        "max_uses": int(item["max_uses"]) if item.get("max_uses") is not None else None,
        "uses_count": int(item.get("uses_count", 0)),
        "valid_from": item.get("valid_from", ""),
        "valid_until": item.get("valid_until", ""),
        "buy_quantity": int(item.get("buy_quantity", 0) or 0),
        "paid_quantity": int(item.get("paid_quantity", 0) or 0),
        "priority": item.get("priority", "media"),
        "create_date": item.get("create_date", ""),
        "update_date": item.get("update_date", ""),
    }


# ───────── Router ─────────
def offers_routes(path, method, event, user_id, alias):
    # Public
    m = re.fullmatch(rf"/{alias}/offers/public/([^/]+)", path)
    if m and method == "GET":
        return get_active_offers(m.group(1))
    # Admin
    if path == f"/{alias}/offers" and method == "GET":
        return get_offers(user_id)
    if path == f"/{alias}/offers" and method == "POST":
        return create_offer(event, user_id)
    m = re.fullmatch(rf"/{alias}/offers/([^/]+)", path)
    if m:
        oid = m.group(1)
        if method == "PUT":
            return update_offer(event, oid, user_id)
        if method == "DELETE":
            return delete_offer(oid, user_id)
    return _resp(404, {"message": "Ruta no encontrada"})


# ───────── Admin CRUD ─────────
def get_offers(user_id):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(200, [])
        items = offers_table.scan(
            FilterExpression=Attr("business_id").eq(biz["business_id"])
        ).get("Items", [])
        return _resp(
            200,
            sorted(
                [_map(i) for i in items], key=lambda x: x["create_date"], reverse=True
            ),
        )
    except Exception as e:
        print(json.dumps({"event": "get_offers", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def _build_item(data, biz_id, offer_id=None):
    code = (data.get("code", "") or "").strip().upper()
    mu = (
        int(data["max_uses"])
        if data.get("max_uses") not in (None, "", 0, "0")
        else None
    )
    item = {
        "offer_id": offer_id or str(uuid.uuid4()),
        "business_id": biz_id,
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "is_active": bool(data.get("is_active", True)),
        "trigger": data.get("trigger", "code"),
        "code": code,
        "discount_type": data.get("discount_type", "percentage"),
        "discount_value": Decimal(str(data.get("discount_value", 0) or 0)),
        "applies_to": data.get("applies_to", "all"),
        "product_ids": data.get("product_ids", []) or [],
        "category_ids": data.get("category_ids", []) or [],
        "min_order_amount": Decimal(str(data.get("min_order_amount", 0) or 0)),
        "max_uses": mu,
        "valid_from": data.get("valid_from", ""),
        "valid_until": data.get("valid_until", ""),
        "buy_quantity": int(data.get("buy_quantity", 0) or 0),
        "paid_quantity": int(data.get("paid_quantity", 0) or 0),
        "priority": (data.get("priority", "media") or "media").lower(),
        "update_date": _now(),
    }
    return item


def create_offer(event, user_id):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(404, {"message": "Negocio no encontrado"})
        data = json.loads(event.get("body", "{}"))
        item = _build_item(data, biz["business_id"])
        item["uses_count"] = 0
        item["create_date"] = _now()
        offers_table.put_item(Item=item)
        return _resp(
            200,
            {"message": "Oferta creada correctamente", "offer_id": item["offer_id"]},
        )
    except Exception as e:
        print(json.dumps({"event": "create_offer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def update_offer(event, offer_id, user_id):
    try:
        existing = offers_table.get_item(Key={"offer_id": offer_id}).get("Item")
        if not existing:
            return _resp(404, {"message": "Oferta no encontrada"})
        biz = _get_biz(user_id)
        if not biz or existing.get("business_id") != biz["business_id"]:
            return _resp(403, {"message": "No autorizado"})
        data = json.loads(event.get("body", "{}"))
        item = _build_item(data, biz["business_id"], offer_id)
        code = item["code"]
        mu = item["max_uses"]
        offers_table.update_item(
            Key={"offer_id": offer_id},
            UpdateExpression=(
                "SET #nm=:n, description=:d, is_active=:a, #trig=:tr, code=:c, "
                "discount_type=:dt, discount_value=:dv, applies_to=:at, "
                "product_ids=:pi, category_ids=:ci, min_order_amount=:moa, "
                "max_uses=:mu, valid_from=:vf, valid_until=:vu, update_date=:ud, "
                "buy_quantity=:bq, paid_quantity=:pq, priority=:prio"
            ),
            ExpressionAttributeNames={"#nm": "name", "#trig": "trigger"},
            ExpressionAttributeValues={
                ":n": item["name"],
                ":d": item["description"],
                ":a": item["is_active"],
                ":tr": item["trigger"],
                ":c": code,
                ":dt": item["discount_type"],
                ":dv": item["discount_value"],
                ":at": item["applies_to"],
                ":pi": item["product_ids"],
                ":ci": item["category_ids"],
                ":moa": item["min_order_amount"],
                ":mu": mu,
                ":vf": item["valid_from"],
                ":vu": item["valid_until"],
                ":bq": item["buy_quantity"],
                ":pq": item["paid_quantity"],
                ":prio": item["priority"],
                ":ud": _now(),
            },
        )
        return _resp(200, {"message": "Oferta actualizada correctamente"})
    except Exception as e:
        print(json.dumps({"event": "update_offer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def delete_offer(offer_id, user_id):
    try:
        existing = offers_table.get_item(Key={"offer_id": offer_id}).get("Item")
        if not existing:
            return _resp(404, {"message": "Oferta no encontrada"})
        biz = _get_biz(user_id)
        if not biz or existing.get("business_id") != biz["business_id"]:
            return _resp(403, {"message": "No autorizado"})
        offers_table.delete_item(Key={"offer_id": offer_id})
        return _resp(200, {"message": "Oferta eliminada correctamente"})
    except Exception as e:
        print(json.dumps({"event": "delete_offer", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ───────── Public ─────────
def get_active_offers(business_id):
    """Devuelve ofertas activas y validas para el catalogo publico."""
    try:
        today = _today()
        items = offers_table.scan(
            FilterExpression=Attr("business_id").eq(business_id)
            & Attr("is_active").eq(True)
        ).get("Items", [])
        valid = []
        for item in items:
            o = _map(item)
            if o["valid_from"] and o["valid_from"] > today:
                continue
            if o["valid_until"] and o["valid_until"] < today:
                continue
            if o["max_uses"] is not None and o["uses_count"] >= o["max_uses"]:
                continue
            valid.append(o)
        return _resp(200, valid)
    except Exception as e:
        print(json.dumps({"event": "get_active_offers", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ───────── Utility llamada desde customers.py ─────────
def increment_offer_uses(offer_id):
    """Incrementa uses_count al confirmar un pedido."""
    try:
        if not offer_id:
            return
        offers_table.update_item(
            Key={"offer_id": offer_id},
            UpdateExpression="SET uses_count = if_not_exists(uses_count,:z) + :one",
            ExpressionAttributeValues={":z": 0, ":one": 1},
        )
    except Exception as e:
        print(json.dumps({"event": "increment_offer_uses", "Error": str(e)}))