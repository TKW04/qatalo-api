import boto3, json, os, uuid, re
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

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


# Hora local de RD (UTC-4 fijo), igual que customers.py: "hoy" para vigencia de ofertas
# debe coincidir con el día local del negocio, no con el día UTC de la Lambda.
RD_TZ = timezone(timedelta(hours=-4), "America/Santo_Domingo")


def _now():
    return datetime.now(RD_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.now(RD_TZ).strftime("%Y-%m-%d")


def _get_biz(user_id):
    items = business_table.scan(FilterExpression=Attr("user_id").eq(user_id)).get(
        "Items", []
    )
    return items[0] if items else None


VALID_DISCOUNT_TYPES = ("percentage", "fixed", "buy_x_get_y")
VALID_APPLIES_TO = ("all", "products", "categories")
VALID_FIXED_MODES = ("order", "per_unit")
VALID_TRIGGERS = ("code", "automatic")
VALID_PRIORITIES = ("alta", "media", "baja")


def _to_int(v, default=0):
    try:
        if v in (None, ""):
            return default
        return int(Decimal(str(v)))
    except Exception:
        return default


class OfferValidationError(ValueError):
    pass


def _num(data, key, default=0):
    """Lee un número no negativo del body; error si no es numérico o es negativo."""
    raw = data.get(key, default)
    if raw in (None, ""):
        raw = default
    try:
        d = Decimal(str(raw))
    except Exception:
        raise OfferValidationError(f"'{key}' debe ser numérico")
    if not d.is_finite() or d < 0:
        raise OfferValidationError(f"'{key}' debe ser un número mayor o igual a 0")
    return d


def _validate_input(data):
    """Valida y normaliza los campos de entrada. Lanza OfferValidationError."""
    dt = data.get("discount_type", "percentage") or "percentage"
    if dt not in VALID_DISCOUNT_TYPES:
        raise OfferValidationError("Tipo de descuento inválido")
    at = data.get("applies_to", "all") or "all"
    if at not in VALID_APPLIES_TO:
        raise OfferValidationError("Alcance (applies_to) inválido")
    trig = data.get("trigger", "code") or "code"
    if trig not in VALID_TRIGGERS:
        raise OfferValidationError("Disparador (trigger) inválido")
    prio = (data.get("priority", "media") or "media").lower()
    if prio not in VALID_PRIORITIES:
        raise OfferValidationError("Prioridad inválida")
    fm = data.get("fixed_mode") or "order"
    if fm not in VALID_FIXED_MODES:
        raise OfferValidationError("fixed_mode debe ser 'order' o 'per_unit'")
    dv = _num(data, "discount_value")
    if dt == "percentage" and dv > 100:
        raise OfferValidationError("El porcentaje no puede ser mayor que 100")
    moa = _num(data, "min_order_amount")
    mq = _num(data, "min_quantity")
    if mq != mq.to_integral_value():
        raise OfferValidationError("min_quantity debe ser un entero")
    bq = _num(data, "buy_quantity")
    pq = _num(data, "paid_quantity")
    if dt == "buy_x_get_y" and not (bq >= 2 and 1 <= pq < bq):
        raise OfferValidationError(
            "Para 'compra X lleva Y': X debe ser ≥ 2 y Y entre 1 y X-1"
        )
    return {
        "discount_type": dt,
        "applies_to": at,
        "trigger": trig,
        "priority": prio,
        "fixed_mode": fm,
        "discount_value": dv,
        "min_order_amount": moa,
        "min_quantity": int(mq),
        "buy_quantity": int(bq),
        "paid_quantity": int(pq),
    }


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
        # v2: ofertas antiguas no tienen estos campos → comportamiento anterior
        "fixed_mode": "per_unit" if item.get("fixed_mode") == "per_unit" else "order",
        "min_quantity": _to_int(item.get("min_quantity"), 0),
        "create_date": item.get("create_date", ""),
        "update_date": item.get("update_date", ""),
    }


# ───────── Router ─────────
def offers_routes(path, method, event, user_id, alias):
    # Public
    m = re.fullmatch(rf"/{alias}/offers/public/([^/]+)/validate-code", path)
    if m and method == "POST":
        return validate_offer_code(event, m.group(1))
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
    v = _validate_input(data)
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
        "trigger": v["trigger"],
        "code": code,
        "discount_type": v["discount_type"],
        "discount_value": v["discount_value"],
        "fixed_mode": v["fixed_mode"],
        "applies_to": v["applies_to"],
        "product_ids": data.get("product_ids", []) or [],
        "category_ids": data.get("category_ids", []) or [],
        "min_order_amount": v["min_order_amount"],
        "min_quantity": v["min_quantity"],
        "max_uses": mu,
        "valid_from": data.get("valid_from", ""),
        "valid_until": data.get("valid_until", ""),
        "buy_quantity": v["buy_quantity"],
        "paid_quantity": v["paid_quantity"],
        "priority": v["priority"],
        "update_date": _now(),
    }
    return item


def create_offer(event, user_id):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(404, {"message": "Negocio no encontrado"})
        data = json.loads(event.get("body", "{}"))
        try:
            item = _build_item(data, biz["business_id"])
        except OfferValidationError as ve:
            return _resp(400, {"message": str(ve)})
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
        try:
            item = _build_item(data, biz["business_id"], offer_id)
        except OfferValidationError as ve:
            return _resp(400, {"message": str(ve)})
        code = item["code"]
        mu = item["max_uses"]
        offers_table.update_item(
            Key={"offer_id": offer_id},
            UpdateExpression=(
                "SET #nm=:n, description=:d, is_active=:a, #trig=:tr, code=:c, "
                "discount_type=:dt, discount_value=:dv, applies_to=:at, "
                "product_ids=:pi, category_ids=:ci, min_order_amount=:moa, "
                "max_uses=:mu, valid_from=:vf, valid_until=:vu, update_date=:ud, "
                "buy_quantity=:bq, paid_quantity=:pq, priority=:prio, "
                "fixed_mode=:fm, min_quantity=:mq"
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
                ":fm": item["fixed_mode"],
                ":mq": item["min_quantity"],
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
def _is_current(o, today):
    """o = oferta mapeada (_map). True si está dentro de fechas y con usos disponibles."""
    if o["valid_from"] and o["valid_from"] > today:
        return False
    if o["valid_until"] and o["valid_until"] < today:
        return False
    if o["max_uses"] is not None and o["max_uses"] > 0 and o["uses_count"] >= o["max_uses"]:
        return False
    return True


# Campos que el catálogo necesita para calcular el descuento (sin code/usos/fechas internas).
_PUBLIC_FIELDS = (
    "offer_id", "name", "description", "trigger", "discount_type", "discount_value",
    "fixed_mode", "applies_to", "product_ids", "category_ids", "min_order_amount",
    "min_quantity", "buy_quantity", "paid_quantity", "priority", "valid_until",
)


def _public_view(o):
    return {k: o[k] for k in _PUBLIC_FIELDS}


def get_active_offers(business_id):
    """Ofertas AUTOMÁTICAS activas y vigentes para el catálogo público.
    Las ofertas por código (trigger 'code', default de las antiguas) no se exponen:
    se validan una a una con POST /offers/public/{business}/validate-code."""
    try:
        today = _today()
        items = offers_table.scan(
            FilterExpression=Attr("business_id").eq(business_id)
            & Attr("is_active").eq(True)
        ).get("Items", [])
        valid = []
        for item in items:
            o = _map(item)
            if o["trigger"] != "automatic":
                continue
            if not _is_current(o, today):
                continue
            valid.append(_public_view(o))
        return _resp(200, valid)
    except Exception as e:
        print(json.dumps({"event": "get_active_offers", "Error": str(e)}))
        return _resp(500, {"message": "No se pudieron cargar las ofertas"})


MAX_CODE_LEN = 64
_CODE_INVALID = {"valid": False, "message": "Código no válido"}


def _find_code_offer(business_id, code):
    kwargs = {"FilterExpression": Attr("business_id").eq(business_id) & Attr("code").eq(code)}
    while True:
        page = offers_table.scan(**kwargs)
        for it in page.get("Items", []):
            if it.get("business_id") == business_id and (it.get("code") or "").upper() == code:
                return it
        if "LastEvaluatedKey" not in page:
            return None
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def validate_offer_code(event, business_id):
    """POST /offers/public/{business}/validate-code  body: {code, items?}
    Devuelve {valid: true, offer: {...campos de cálculo...}} si el código existe, es del
    negocio, es de tipo 'code', está activo y vigente. En cualquier otro caso la MISMA
    respuesta genérica (no revela si el código existe, está vencido o agotado).
    `items` se acepta por compatibilidad pero no se usa: si la oferta aplica al carrito
    lo decide el motor del cliente y, al pagar, el servidor (customers.py)."""
    try:
        try:
            data = json.loads(event.get("body") or "{}")
        except Exception:
            data = {}
        code = data.get("code") if isinstance(data, dict) else ""
        code = (code if isinstance(code, str) else "").strip().upper()
        if not business_id or not code or len(code) > MAX_CODE_LEN:
            return _resp(200, _CODE_INVALID)
        raw = _find_code_offer(business_id, code)
        if not raw:
            return _resp(200, _CODE_INVALID)
        o = _map(raw)
        if o["trigger"] != "code" or not o["is_active"] or not _is_current(o, _today()):
            return _resp(200, _CODE_INVALID)
        offer = _public_view(o)
        offer["code"] = code  # eco del código que el cliente ya escribió (lo reenvía al pagar)
        return _resp(200, {"valid": True, "offer": offer})
    except Exception as e:
        print(json.dumps({"event": "validate_offer_code", "Error": str(e)}))
        return _resp(200, _CODE_INVALID)


# ───────── Usos (max_uses) — llamado desde customers.py ─────────
# Condición atómica: la oferta existe y (no tiene tope | tope 0/NULL | quedan usos).
# Las ofertas sin tope se guardan con max_uses = None (tipo NULL en DynamoDB).
_RESERVE_CONDITION = (
    "attribute_exists(offer_id) AND ("
    "attribute_not_exists(max_uses) OR attribute_type(max_uses, :null_t) "
    "OR max_uses = :zero OR uses_count < max_uses "
    "OR (attribute_not_exists(uses_count) AND max_uses > :zero))"
)


def reserve_offer_use(offer_id):
    """Reserva un uso de forma atómica (UpdateItem condicional, ADD uses_count 1).
    True si se reservó; False si la oferta está agotada/no existe o hubo error
    (en ese caso la orden debe crearse SIN descuento)."""
    if not offer_id:
        return False
    try:
        offers_table.update_item(
            Key={"offer_id": offer_id},
            UpdateExpression="ADD uses_count :one",
            ConditionExpression=_RESERVE_CONDITION,
            ExpressionAttributeValues={":one": 1, ":zero": 0, ":null_t": "NULL"},
        )
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        print(json.dumps({"event": "reserve_offer_use", "level": "WARNING",
                          "kind": "oferta_sin_usos_disponibles" if code == "ConditionalCheckFailedException" else "error",
                          "offer_id": offer_id, "Error": str(e)}))
        return False
    except Exception as e:
        print(json.dumps({"event": "reserve_offer_use", "level": "WARNING", "kind": "error",
                          "offer_id": offer_id, "Error": str(e)}))
        return False


def release_offer_use(offer_id):
    """Devuelve un uso reservado (si la escritura de la orden falló). Nunca baja de 0."""
    if not offer_id:
        return
    try:
        offers_table.update_item(
            Key={"offer_id": offer_id},
            UpdateExpression="ADD uses_count :minus",
            ConditionExpression="attribute_exists(offer_id) AND uses_count > :zero",
            ExpressionAttributeValues={":minus": -1, ":zero": 0},
        )
    except Exception as e:
        print(json.dumps({"event": "release_offer_use", "level": "WARNING",
                          "offer_id": offer_id, "Error": str(e)}))


def increment_offer_uses(offer_id):
    """Compatibilidad: usar reserve_offer_use (atómico, respeta max_uses)."""
    reserve_offer_use(offer_id)
