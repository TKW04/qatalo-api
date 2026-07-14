import os
import json
import uuid
import traceback

import boto3
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
suggestions_table = dynamodb.Table("qatalo.suggestions")
business_table = dynamodb.Table("qatalo.business")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Tipos y estados válidos
VALID_TYPES = {"feature", "improvement", "bug", "other"}
DEFAULT_STATUS = "nueva"  # nueva | en_revision | planeada | en_progreso | completada | descartada


# ----------------- Helpers -----------------
def _resp(status, body=None):
    out = {"statusCode": status, "headers": CORS}
    if body is not None:
        out["body"] = json.dumps(body, default=str)
    return out


def _now():
    return datetime.now().strftime(DATE_FMT)


def _get_business_by_user(user_id):
    """El negocio del dueño autenticado (para etiquetar la sugerencia)."""
    if not user_id:
        return {}
    items = business_table.scan(
        FilterExpression=Attr("user_id").eq(user_id)
    ).get("Items", [])
    return items[0] if items else {}


def _map_suggestion(item, include_admin=False):
    data = {
        "suggestion_id": item.get("suggestion_id", ""),
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "type": item.get("type", "other"),
        "status": item.get("status", DEFAULT_STATUS),
        "create_date": item.get("create_date", ""),
        "update_date": item.get("update_date", ""),
    }
    if include_admin:
        # Campos que solo ve el panel Root (paso 4)
        data["business_id"] = item.get("business_id", "")
        data["business_name"] = item.get("business_name", "")
        data["email"] = item.get("email", "")
        data["user_id"] = item.get("user_id", "")
        data["admin_notes"] = item.get("admin_notes", "")
    return data


# ----------------- Router -----------------
def suggestions_routes(path, method, event, user_name, user_id, alias):
    try:
        if path == f"/{alias}/suggestions" and method == "POST":
            return create_suggestion(event=event, user_name=user_name, user_id=user_id)
        if path == f"/{alias}/suggestions" and method == "GET":
            return get_my_suggestions(user_id=user_id)

        # NOTA: el listado global y el cambio de estado (panel Root) se agregan en el paso 4.

        return _resp(404, {"message": "Ruta no encontrada"})
    except Exception as e:
        print(json.dumps({"event": "suggestions_routes", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


# ----------------- Crear -----------------
def create_suggestion(event, user_name, user_id):
    """Un dueño autenticado envía una sugerencia de mejora / feature."""
    try:
        if not user_id:
            return _resp(401, {"message": "No autorizado"})

        body = json.loads(event.get("body", "{}"))
        title = (body.get("title", "") or "").strip()
        description = (body.get("description", "") or "").strip()
        stype = (body.get("type", "other") or "other").strip().lower()

        if not title:
            return _resp(400, {"message": "El título es requerido"})
        if not description:
            return _resp(400, {"message": "La descripción es requerida"})
        if stype not in VALID_TYPES:
            stype = "other"

        # Límites defensivos
        title = title[:120]
        description = description[:2000]

        business = _get_business_by_user(user_id)
        suggestion_id = str(uuid.uuid4())
        now = _now()

        suggestions_table.put_item(
            Item={
                "suggestion_id": suggestion_id,
                "user_id": user_id,
                "business_id": business.get("business_id", ""),
                "business_name": business.get("business_name", "")
                or business.get("name", ""),
                "email": user_name or "",
                "title": title,
                "description": description,
                "type": stype,
                "status": DEFAULT_STATUS,
                "admin_notes": "",
                "create_date": now,
                "create_user": user_name or "",
                "update_date": now,
                "update_user": user_name or "",
            }
        )

        return _resp(
            200,
            {"message": "¡Gracias! Tu sugerencia fue enviada.", "suggestion_id": suggestion_id},
        )
    except Exception as e:
        print(
            json.dumps(
                {
                    "event": "create_suggestion",
                    "Error": str(e),
                    "trace": traceback.format_exc(),
                }
            )
        )
        return _resp(500, {"message": str(e)})


# ----------------- Lectura del propio dueño -----------------
def get_my_suggestions(user_id):
    """Sugerencias enviadas por el dueño autenticado (para que vea su historial y estatus)."""
    try:
        if not user_id:
            return _resp(401, {"message": "No autorizado"})

        items = suggestions_table.scan(
            FilterExpression=Attr("user_id").eq(user_id)
        ).get("Items", [])

        mapped = sorted(
            [_map_suggestion(i) for i in items],
            key=lambda x: x.get("create_date", ""),
            reverse=True,
        )
        return _resp(200, mapped)
    except Exception as e:
        print(json.dumps({"event": "get_my_suggestions", "Error": str(e)}))
        return _resp(500, {"message": str(e)})