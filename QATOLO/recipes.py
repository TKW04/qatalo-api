"""Recetas del negocio (solo control de costos; NO afecta inventario/stock).

Rutas (mismo prefijo /{alias}/ que /products, requieren sesión del dueño):
  GET    /recipes                 -> lista ordenada por nombre
  POST   /recipes                 -> crea
  PUT    /recipes/{recipe_id}     -> reemplaza los campos editables
  DELETE /recipes/{recipe_id}

Receta: {recipe_id, name, yield_lb, notes, ingredients: [{name, quantity, unit,
cost_per_recipe, store}], packaging: [{name, quantity_per_unit, unit_cost, store}],
create_date, update_date}

IMPORTANTE: la tabla `qatalo.recipes` NO existe por IaC; hay que crearla a mano
(PK business_id, SK recipe_id):

  aws dynamodb create-table \
    --table-name qatalo.recipes \
    --attribute-definitions AttributeName=business_id,AttributeType=S AttributeName=recipe_id,AttributeType=S \
    --key-schema AttributeName=business_id,KeyType=HASH AttributeName=recipe_id,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --profile BMCargo --region us-east-1

La Lambda necesita además permisos dynamodb:PutItem/GetItem/DeleteItem/Query/UpdateItem sobre esa tabla.
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
recipes_table = dynamodb.Table("qatalo.recipes")
business_table = dynamodb.Table("qatalo.business")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}

# Hora local de RD (UTC-4 fijo), igual que production.py.
RD_TZ = timezone(timedelta(hours=-4), "America/Santo_Domingo")
MAX_NAME = 100
MAX_NOTES = 500
MAX_UNIT = 20
MAX_LINE_TEXT = 100
MAX_INGREDIENTS = 100
MAX_PACKAGING = 50
MAX_NUMBER = Decimal("1000000000")


class RecipeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _now():
    return datetime.now(RD_TZ).strftime("%Y-%m-%d %H:%M:%S")


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


# ----------------- Validación -----------------
def _text(raw, field, required=False, max_len=MAX_LINE_TEXT):
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise RecipeError(f"'{field}' debe ser texto")
    value = raw.strip()
    if required and not value:
        raise RecipeError(f"'{field}' es requerido")
    if len(value) > max_len:
        raise RecipeError(f"'{field}' no puede tener más de {max_len} caracteres")
    return value


def _number(raw, field, allow_zero=False):
    """Número JSON (int/float) finito, > 0 (o >= 0 si allow_zero). Devuelve Decimal."""
    cond = "mayor o igual que 0" if allow_zero else "mayor que 0"
    msg = f"'{field}' debe ser un número {cond}"
    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
        raise RecipeError(msg)
    try:
        d = Decimal(str(raw))
    except Exception:
        raise RecipeError(msg)
    if not d.is_finite():
        raise RecipeError(msg)
    if d < 0 or (d == 0 and not allow_zero):
        raise RecipeError(msg)
    if d > MAX_NUMBER:
        raise RecipeError(f"'{field}' es demasiado grande")
    return d


def _list(raw, field, max_items):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RecipeError(f"'{field}' debe ser una lista")
    if len(raw) > max_items:
        raise RecipeError(f"'{field}' no puede tener más de {max_items} elementos")
    for i, line in enumerate(raw):
        if not isinstance(line, dict):
            raise RecipeError(f"'{field}[{i}]' debe ser un objeto")
    return raw


def _parse_ingredients(raw):
    out = []
    for i, line in enumerate(_list(raw, "ingredients", MAX_INGREDIENTS)):
        p = f"ingredients[{i}]"
        out.append({
            "name": _text(line.get("name"), f"{p}.name", required=True),
            "quantity": _number(line.get("quantity"), f"{p}.quantity"),
            "unit": _text(line.get("unit"), f"{p}.unit", required=True, max_len=MAX_UNIT),
            "cost_per_recipe": _number(line.get("cost_per_recipe"), f"{p}.cost_per_recipe", allow_zero=True),
            "store": _text(line.get("store"), f"{p}.store"),
        })
    return out


def _parse_packaging(raw):
    out = []
    for i, line in enumerate(_list(raw, "packaging", MAX_PACKAGING)):
        p = f"packaging[{i}]"
        out.append({
            "name": _text(line.get("name"), f"{p}.name", required=True),
            "quantity_per_unit": _number(line.get("quantity_per_unit"), f"{p}.quantity_per_unit"),
            "unit_cost": _number(line.get("unit_cost"), f"{p}.unit_cost", allow_zero=True),
            "store": _text(line.get("store"), f"{p}.store"),
        })
    return out


def _parse_body(event):
    try:
        data = json.loads(event.get("body") or "{}")
    except Exception:
        raise RecipeError("Body inválido")
    if not isinstance(data, dict):
        raise RecipeError("Body inválido")
    return {
        "name": _text(data.get("name"), "name", required=True, max_len=MAX_NAME),
        "yield_lb": _number(data.get("yield_lb"), "yield_lb"),
        "notes": _text(data.get("notes"), "notes", max_len=MAX_NOTES),
        "ingredients": _parse_ingredients(data.get("ingredients")),
        "packaging": _parse_packaging(data.get("packaging")),
    }


# ----------------- Mapeo -----------------
def _num_out(v):
    try:
        d = Decimal(str(v))
    except Exception:
        return 0
    if not d.is_finite():
        return 0
    return int(d) if d == d.to_integral_value() else float(d)


def _map_recipe(item):
    return {
        "recipe_id": item.get("recipe_id", ""),
        "name": item.get("name", ""),
        "yield_lb": _num_out(item.get("yield_lb", 0)),
        "notes": item.get("notes", ""),
        "ingredients": [
            {
                "name": l.get("name", ""),
                "quantity": _num_out(l.get("quantity", 0)),
                "unit": l.get("unit", ""),
                "cost_per_recipe": _num_out(l.get("cost_per_recipe", 0)),
                "store": l.get("store", ""),
            }
            for l in (item.get("ingredients") or [])
        ],
        "packaging": [
            {
                "name": l.get("name", ""),
                "quantity_per_unit": _num_out(l.get("quantity_per_unit", 0)),
                "unit_cost": _num_out(l.get("unit_cost", 0)),
                "store": l.get("store", ""),
            }
            for l in (item.get("packaging") or [])
        ],
        "create_date": item.get("create_date", ""),
        "update_date": item.get("update_date", ""),
    }


# ----------------- Router -----------------
def recipes_routes(path, method, event, user_name, user_id, alias):
    try:
        biz = _get_biz(user_id)
        if not biz:
            return _resp(403, {"message": "No autorizado"})
        business_id = biz.get("business_id", "")

        if path == f"/{alias}/recipes" and method == "GET":
            return list_recipes(business_id)
        if path == f"/{alias}/recipes" and method == "POST":
            return create_recipe(event, business_id, user_name or user_id)
        match = re.fullmatch(rf"/{alias}/recipes/([^/]+)", path)
        if match and method == "PUT":
            return update_recipe(event, business_id, match.group(1), user_name or user_id)
        if match and method == "DELETE":
            return delete_recipe(business_id, match.group(1))
        return _resp(404, {"message": "Ruta no encontrada"})
    except RecipeError as e:
        return _resp(e.status, {"message": str(e)})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        print(json.dumps({"event": "recipes_routes", "Error": str(e)}))
        if code == "ResourceNotFoundException":
            return _resp(503, {"message": "Las recetas no están disponibles"})
        return _resp(500, {"message": str(e)})
    except Exception as e:
        print(json.dumps({"event": "recipes_routes", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Handlers -----------------
def list_recipes(business_id):
    items = _query_all(recipes_table, KeyConditionExpression=Key("business_id").eq(business_id))
    recipes = [_map_recipe(i) for i in items if i.get("business_id") == business_id]
    recipes.sort(key=lambda r: (r["name"].lower(), r["recipe_id"]))
    return _resp(200, recipes)


def create_recipe(event, business_id, updated_by):
    fields = _parse_body(event)
    now = _now()
    item = {
        "business_id": business_id,
        "recipe_id": str(uuid.uuid4()),
        **fields,
        "create_date": now,
        "update_date": now,
        "create_user": updated_by or "",
        "update_user": updated_by or "",
    }
    recipes_table.put_item(Item=item)
    return _resp(200, _map_recipe(item))


def _get_own(business_id, recipe_id):
    item = recipes_table.get_item(Key={"business_id": business_id, "recipe_id": recipe_id}).get("Item")
    if not item or item.get("business_id") != business_id:
        raise RecipeError("Receta no encontrada", status=404)
    return item


def update_recipe(event, business_id, recipe_id, updated_by):
    fields = _parse_body(event)
    current = _get_own(business_id, recipe_id)
    item = {
        "business_id": business_id,
        "recipe_id": recipe_id,
        **fields,
        "create_date": current.get("create_date", ""),
        "create_user": current.get("create_user", ""),
        "update_date": _now(),
        "update_user": updated_by or "",
    }
    recipes_table.put_item(Item=item)
    return _resp(200, _map_recipe(item))


def delete_recipe(business_id, recipe_id):
    _get_own(business_id, recipe_id)
    recipes_table.delete_item(Key={"business_id": business_id, "recipe_id": recipe_id})
    return _resp(200, {"message": "Receta eliminada correctamente", "recipe_id": recipe_id})
