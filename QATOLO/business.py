from decimal import Decimal
import json
import uuid
import boto3
import os
from datetime import datetime
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
business_table = dynamodb.Table("qatalo.business")
s3 = boto3.client("s3")

CORS = {"Access-Control-Allow-Origin": "*"}


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": CORS,
        "body": json.dumps(body, default=str),
    }


# --- Helper para generar URLs Pre-firmadas ---
def get_presigned_url(
    user_id,
    type_file="logo",
    extension=".png",
    content_type="image/png",
    folder="business",
):
    safe_folder = (folder or "business").strip("/")
    key = f"{safe_folder}/{user_id}_{type_file}{extension}"
    public_url = f"https://{os.getenv('BUCKET_NAME')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{key}"

    upload_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": os.getenv("BUCKET_NAME"),
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=3600,
    )
    return {"uploadUrl": upload_url, "publicUrl": public_url}


# --- Rutas ---
def business_routes(path, method, event, user_name, user_id, alias):
    if path == f"/{alias}/businesses/presign" and method == "POST":
        body = json.loads(event.get("body", "{}"))
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps(
                get_presigned_url(
                    user_id,
                    body.get("type"),
                    body.get("ext"),
                    body.get("mime"),
                    body.get("folder", "business"),
                )
            ),
        }

    if path == f"/{alias}/businesses" and method == "POST":
        return create_business(event=event, user_name=user_name, user_id=user_id)

    if path == f"/{alias}/businesses" and method == "GET":
        return get_business_by_user_id(user_id=user_id)

    if path.startswith(f"/{alias}/businesses/") and method == "PUT":
        business_id = path.split("/")[-1]
        return update_business(event=event, user_id=user_id, business_id=business_id)

    if path.startswith(f"/{alias}/businesses/") and method == "GET":
        slug = path.split("/")[-1]
        return get_business_by_slug(slug)

    # Lógica para PUT/GET por ID/Slug...
    return {"statusCode": 404, "body": "Ruta no encontrada"}


# --- Métodos de Negocio ---


def get_business_by_user_id(user_id: str):
    """
    Retrieve business by user ID.
    """
    try:
        response = business_table.scan(FilterExpression=Attr("user_id").eq(user_id))
        if "Items" in response and len(response["Items"]) > 0:
            item = response["Items"][0]
            # Mapeamos los nuevos campos de diseño
            business = {
                "business_id": item.get("business_id"),
                "name": item.get("business_name"),
                "description": item.get("business_description"),
                "slug": item.get("business_slug"),
                "phone": item.get("business_phone"),
                "logo_url": item.get("business_logo_url"),
                "templateId": item.get("template_id", "default"),
                "themeType": item.get("theme_type", "predefined"),
                "themePalette": item.get("theme_palette") or {},
                "localities": item.get("localities") or [],
                "ga_tracking_id": item.get("ga_tracking_id", ""),
                "meta_pixel_id": item.get("meta_pixel_id", ""),
                "low_stock_threshold": int(item.get("low_stock_threshold", 5) or 5),
                "rnc": item.get("rnc", ""),
                "ncf_enabled": bool(item.get("ncf_enabled", False)),
                "itbis_rate": float(item.get("itbis_rate", 18) or 18),
                "ncf_pool": item.get("ncf_pool", []) or [],
                "delivery_reminder_enabled": bool(
                    item.get("delivery_reminder_enabled", False)
                ),
                # Tipografía y logo (fallback = comportamiento actual)
                "fontHeading": item.get("font_heading", "default"),
                "fontBody": item.get("font_body", "default"),
                "fontScale": item.get("font_scale", "medium"),
                "logoScale": item.get("logo_scale", "medium"),
                # Fuentes subidas por el negocio
                "custom_fonts": item.get("custom_fonts", []) or [],
            }
            return _resp(200, business)
        return {"statusCode": 404, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"message": str(e)})}


def get_business_by_slug(slug: str):
    """Catálogo público por slug (sin autenticación)."""
    try:
        response = business_table.scan(FilterExpression=Attr("business_slug").eq(slug))
        items = response.get("Items", [])
        if not items:
            return _resp(404, {"message": "Catálogo no encontrado"})
        item = items[0]
        business = {
            "business_id": item.get("business_id"),
            "name": item.get("business_name"),
            "description": item.get("business_description"),
            "slug": item.get("business_slug"),
            "phone": item.get("business_phone"),
            "logo_url": item.get("business_logo_url"),
            "templateId": item.get("template_id", "default"),
            "themeType": item.get("theme_type", "predefined"),
            "themePalette": item.get("theme_palette") or {},
            "localities": item.get("localities") or [],
            "status": item.get("status"),
            "ga_tracking_id": item.get("ga_tracking_id", ""),
            "meta_pixel_id": item.get("meta_pixel_id", ""),
            "low_stock_threshold": int(item.get("low_stock_threshold", 5) or 5),
            "rnc": item.get("rnc", ""),
            "ncf_enabled": bool(item.get("ncf_enabled", False)),
            "itbis_rate": float(item.get("itbis_rate", 18) or 18),
            "ncf_pool": item.get("ncf_pool", []) or [],
            # Tipografía y logo (fallback = comportamiento actual)
            "fontHeading": item.get("font_heading", "default"),
            "fontBody": item.get("font_body", "default"),
            "fontScale": item.get("font_scale", "medium"),
            "logoScale": item.get("logo_scale", "medium"),
            # Fuentes subidas por el negocio
            "custom_fonts": item.get("custom_fonts", []) or [],
        }
        return _resp(200, business)
    except Exception as e:
        print(json.dumps({"event": "get_business_by_slug", "Error": str(e)}))
        return _resp(500, {"message": str(e)})


def create_business(event, user_name, user_id):
    try:
        data = json.loads(event["body"])
        if business_table.scan(
            FilterExpression=Attr("business_slug").eq(data.get("slug"))
        ).get("Items"):
            return _resp(400, {"message": "El slug ya existe"})
        item = {
            "business_id": str(uuid.uuid4()),
            "user_id": user_id,
            "business_name": data.get("name"),
            "business_description": data.get("description"),
            "business_slug": data.get("slug"),
            "business_phone": data.get("phone"),
            "business_logo_url": data.get("logo_url"),
            "template_id": data.get("templateId", "default"),
            "theme_type": data.get("themeType", "predefined"),
            "theme_palette": data.get("themePalette"),
            "localities": data.get("localities") or [],
            "ga_tracking_id": data.get("ga_tracking_id", ""),
            "meta_pixel_id": data.get("meta_pixel_id", ""),
            "create_date": datetime.now().isoformat(),
            "update_date": datetime.now().isoformat(),
            "low_stock_threshold": int(data.get("low_stock_threshold", 5) or 5),
            "delivery_reminder_enabled": bool(
                data.get("delivery_reminder_enabled", False)
            ),
            "rnc": data.get("rnc", ""),
            "ncf_enabled": bool(data.get("ncf_enabled", False)),
            "itbis_rate": Decimal(str(data.get("itbis_rate", 18) or 18)),
            "ncf_pool": data.get("ncf_pool", []) or [],
            # Tipografía y logo
            "font_heading": data.get("fontHeading", "default"),
            "font_body": data.get("fontBody", "default"),
            "font_scale": data.get("fontScale", "medium"),
            "logo_scale": data.get("logoScale", "medium"),
            # Fuentes subidas por el negocio
            "custom_fonts": data.get("custom_fonts", []) or [],
        }
        business_table.put_item(Item=item)
        return _resp(
            200, {"message": "Negocio creado", "business_id": item["business_id"]}
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})


def update_business(event, user_id, business_id):
    try:
        data = json.loads(event["body"])
        existing = business_table.get_item(Key={"business_id": business_id}).get("Item")
        if not existing or existing.get("user_id") != user_id:
            return _resp(404, {"message": "Negocio no encontrado"})
        business_table.update_item(
            Key={"business_id": business_id},
            UpdateExpression=(
                "SET business_name=:n, business_description=:d, business_slug=:s, "
                "business_phone=:p, business_logo_url=:l, template_id=:t, "
                "theme_type=:tt, theme_palette=:tp, localities=:loc, update_date=:u, "
                "ga_tracking_id=:ga, meta_pixel_id=:mp, low_stock_threshold=:lst, "
                "delivery_reminder_enabled=:dre, "
                "rnc=:rnc, ncf_enabled=:nce, itbis_rate=:itr, ncf_pool=:ncp, "
                "font_heading=:fh, font_body=:fb, font_scale=:fs, logo_scale=:ls, "
                "custom_fonts=:cf"
            ),
            ExpressionAttributeValues={
                ":n": data.get("name"),
                ":d": data.get("description"),
                ":s": data.get("slug"),
                ":p": data.get("phone"),
                ":l": data.get("logo_url"),
                ":t": data.get("templateId", "default"),
                ":tt": data.get("themeType", "predefined"),
                ":tp": data.get("themePalette"),
                ":loc": data.get("localities") or [],
                ":u": datetime.now().isoformat(),
                ":ga": data.get("ga_tracking_id", ""),
                ":mp": data.get("meta_pixel_id", ""),
                ":lst": int(data.get("low_stock_threshold", 5) or 5),
                ":dre": bool(data.get("delivery_reminder_enabled", False)),
                ":rnc": data.get("rnc", ""),
                ":nce": bool(data.get("ncf_enabled", False)),
                ":itr": Decimal(str(data.get("itbis_rate", 18) or 18)),
                ":ncp": data.get("ncf_pool", []) or [],
                ":fh": data.get("font_heading", "default"),
                ":fb": data.get("font_body", "default"),
                ":fs": data.get("font_scale", "medium"),
                ":ls": data.get("logo_scale", "medium"),
                ":cf": data.get("custom_fonts", []) or [],
            },
        )
        return _resp(200, {"message": "Negocio actualizado"})
    except Exception as e:
        print("Error en update_business:", e)
        return _resp(500, {"error": str(e)})