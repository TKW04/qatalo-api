
from decimal import Decimal
import io
import os
import re
import traceback
import boto3
import json
import uuid
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION"))
products_table = dynamodb.Table("qatalo.products")
s3 = boto3.client("s3")


def products_routes(path, method, event, user_name, user_id, alias):
    try:
        if path == f"/{alias}/products" and method == "GET":
            return get_products_by_user_id(user_id=user_id)
        if path == f"/{alias}/products" and method == "POST":
            return create_product(event=event, user_name=user_name, user_id=user_id)
        if path == f"/{alias}/products/delete/image" and method == "DELETE":
            return delete_product_image(event=event)

        if "/products/dropdown/" in path:
            match_dropdown = re.fullmatch(rf"/{alias}/products/dropdown/([^/]+)", path)
            if match_dropdown:
                business_id = match_dropdown.group(1)
                if method == "GET":
                    return get_product_dropdown(business_id=business_id)

        if "/products/n8n" in path and method == "GET":
            
            match = re.fullmatch(rf"/{alias}/products/n8n/([^/]+)", path)
            if match:
                business_id = match.group(1)

                return get_products_by_business_id(business_id=business_id)

        match = re.fullmatch(rf"/{alias}/products/([^/]+)", path)
        if match:
            product_id = match.group(1)
            if method == "PUT":
                return update_product(
                    event=event,
                    user_name=user_name,
                    product_id=product_id,
                    user_id=user_id,
                )
            if method == "GET":
                return get_products_by_business_id(business_id=product_id)
            if method == "DELETE":
                return delete_product(product_id)

        return {"statusCode": 404, "body": "Ruta no encontrada"}
    except Exception as e:
        print(json.dumps({"event": "products_routes", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


def get_products_by_user_id(user_id: str):
    """
    Retrieve all products from the database.
    """
    try:
        response = products_table.scan(FilterExpression=Attr("user_id").eq(user_id))
        products = []
        for item in response.get("Items", []):

            images = []
            imagesUrl = item.get("imagesUrl", [])
            for image in imagesUrl:
                images.append({"image": image})

            products.append(
                {
                    "product_id": item.get("product_id", ""),
                    "business_id": item.get("business_id", ""),
                    "name": item.get("product_name", ""),
                    "description": item.get("description", ""),
                    "price": Decimal(item.get("price", 0.0)),
                    "quantity": int(item.get("quantity", 0)),
                    "orden": int(item.get("orden", 0)),
                    "just_one": bool(
                        item.get("just_one", False)
                        if item.get("just_one", False) in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "show_quantity": bool(
                        item.get("show_quantity", False)
                        if item.get("show_quantity", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "terms": item.get("terms", ""),
                    "min_age_allow": bool(
                        item.get("min_age_allow", False)
                        if item.get("min_age_allow", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "min_age": int(item.get("min_age", 0)),
                    "currency": item.get("currency", ""),
                    "imagesUrl": images,
                    "category_id": item.get("category_id", ""),
                    "is_available": item.get("is_available", "unavailable"),
                    "required_delivery_day": bool(
                        item.get("required_delivery_day", False)
                        if item.get("required_delivery_day", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "delivery_start_day": item.get("delivery_start_day", ""),
                    "localities": item.get("localities", []) or [],
                    "is_customizable": bool(
                        item.get("is_customizable", False)
                        if item.get("is_customizable", False) in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "variants": item.get("variants", []) or [],
                    "locality_config": item.get("locality_config", []) or [],
                }
            )
        return {
            "statusCode": 200,  # No uses 204
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(products, default=str),
        }
    except Exception as e:
        print(json.dumps({"event": "get_products", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


def get_products_by_business_id(business_id: str):
    """
    Retrieve all products from the database.
    """
    try:
        response = products_table.scan(
            FilterExpression=Attr("business_id").eq(business_id)
        )
        products = []
        for item in response.get("Items", []):

            images = []
            imagesUrl = item.get("imagesUrl", [])
            for image in imagesUrl:
                images.append({"image": image})

            products.append(
                {
                    "product_id": item.get("product_id", ""),
                    "business_id": item.get("business_id", ""),
                    "name": item.get("product_name", ""),
                    "description": item.get("description", ""),
                    "price": Decimal(item.get("price", 0.0)),
                    "quantity": int(item.get("quantity", 0)),
                    "orden": int(item.get("orden", 0)),
                    "just_one": bool(
                        item.get("just_one", False)
                        if item.get("just_one", False) in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "show_quantity": bool(
                        item.get("show_quantity", False)
                        if item.get("show_quantity", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "terms": item.get("terms", ""),
                    "min_age_allow": bool(
                        item.get("min_age_allow", False)
                        if item.get("min_age_allow", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "min_age": int(item.get("min_age", 0)),
                    "currency": item.get("currency", ""),
                    "imagesUrl": images,
                    "category_id": item.get("category_id", ""),
                    "is_available": item.get("is_available", "unavailable"),
                    "required_delivery_day": bool(
                        item.get("required_delivery_day", False)
                        if item.get("required_delivery_day", False)
                        in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "delivery_start_day": item.get("delivery_start_day", ""),
                    "localities": item.get("localities", []) or [],
                    "place": item.get("place", ""),
                    "is_customizable": bool(
                        item.get("is_customizable", False)
                        if item.get("is_customizable", False) in [True, "true", "True", 1, "1"]
                        else False
                    ),
                    "variants": item.get("variants", []) or [],
                    "locality_config": item.get("locality_config", []) or [],
                }
            )
        return {
            "statusCode": 200,  # No uses 204
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(products, default=str),
        }
    except Exception as e:
        print(json.dumps({"event": "get_products", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


def _resp(status, body):
    return {"statusCode": status, "headers": {"Access-Control-Allow-Origin": "*"}, "body": json.dumps(body, default=str)}

def create_product(event, user_name, user_id):
    try:
        data = json.loads(event.get("body", "{}"))
        product_id = str(uuid.uuid4())
        products_table.put_item(Item={
            "product_id": product_id,
            "business_id": data.get("business_id", "").strip(),
            "product_name": data.get("name", "").strip(),
            "description": data.get("description", "").strip(),
            "price": Decimal(str(data.get("price", 0) or 0)),
            "quantity": int(data.get("quantity", 0) or 0),
            "orden": int(data.get("orden", 0) or 0),
            "just_one": bool(data.get("just_one", False)),
            "terms": data.get("terms", "").strip(),
            "show_quantity": bool(data.get("show_quantity", False)),
            "currency": data.get("currency", ""),
            "imagesUrl": data.get("imagesUrl", []),
            "category_id": data.get("category_id", ""),
            "is_available": data.get("is_available", "unavailable"),
            "min_age_allow": bool(data.get("min_age_allow", False)),
            "min_age": int(data.get("min_age", 0) or 0),
            "required_delivery_day": bool(data.get("required_delivery_day", False)),
            "delivery_start_day": data.get("delivery_start_day", ""),
            "localities": data.get("localities", []) or [],
            "is_customizable": bool(data.get("is_customizable", False)),        # ← nuevo
            "variants": data.get("variants", []) or [],      
            "locality_config": data.get("locality_config", []) or [],                   # ← nuevo
            "user_id": user_id,
            "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "create_user": user_name,
            "update_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "update_user": user_name,   
        })
        return _resp(200, {"message": "Producto creado correctamente"})
    except Exception as e:
        print(json.dumps({"event": "create_product", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def update_product(event, user_name, product_id, user_id):
    try:
        data = json.loads(event.get("body", "{}"))
        products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression=(
                "SET product_name=:n, description=:d, price=:p, quantity=:q, orden=:o, "
                "just_one=:j, show_quantity=:sq, terms=:t, min_age_allow=:ma, min_age=:mage, "
                "required_delivery_day=:rdd, delivery_start_day=:dsd, currency=:c, "
                "imagesUrl=:img, category_id=:cat, is_available=:av, localities=:loc, "
                "is_customizable=:ic, #var=:var, locality_config=:lc, "
                "user_id=:uid, update_date=:ud, update_user=:uu"
            ),
            ExpressionAttributeNames={"#var": "variants"},                       # ← nuevo (variants es reservada)
            ExpressionAttributeValues={
                ":n": data.get("name", "").strip(),
                ":d": data.get("description", "").strip(),
                ":p": Decimal(str(data.get("price", 0) or 0)),
                ":q": int(data.get("quantity", 0) or 0),
                ":o": int(data.get("orden", 0) or 0),
                ":j": bool(data.get("just_one", False)),
                ":sq": bool(data.get("show_quantity", False)),
                ":t": data.get("terms", "").strip(),
                ":ma": bool(data.get("min_age_allow", False)),
                ":mage": int(data.get("min_age", 0) or 0),
                ":rdd": bool(data.get("required_delivery_day", False)),
                ":dsd": data.get("delivery_start_day", ""),
                ":c": data.get("currency", ""),
                ":img": data.get("imagesUrl", []),
                ":cat": data.get("category_id", ""),
                ":av": data.get("is_available", "unavailable"),
                ":loc": data.get("localities", []) or [],
                ":ic": bool(data.get("is_customizable", False)),                 # ← nuevo
                ":var": data.get("variants", []) or [],                          # ← nuevo
                ":lc": data.get("locality_config", []) or [],
                ":uid": user_id,
                ":ud": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ":uu": user_name,
            },
            ReturnValues="UPDATED_NEW",
        )
        return _resp(200, {"message": "Producto actualizado correctamente"})
    except Exception as e:
        print(json.dumps({"event": "update_product", "Error": str(e)}))
        return _resp(500, {"message": str(e)})

def delete_product_image(event):
    try:
        body = json.loads(event.get("body", "{}"))
        file_url = body.get("file_url", "")
        product_id = body.get("product_id", "")

        response = products_table.get_item(Key={"product_id": product_id})

        if "Item" in response:
            item = response["Item"]
            if "imagesUrl" in item:
                item["imagesUrl"].remove(file_url)
                products_table.update_item(
                    Key={"product_id": product_id},
                    UpdateExpression="SET imagesUrl = :imagesUrl",
                    ExpressionAttributeValues={":imagesUrl": item["imagesUrl"]},
                    ReturnValues="UPDATED_NEW",
                )

        delete_image(file_url)
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": "Imagen eliminada correctamente"}),
        }
    except Exception as e:
        print(json.dumps({"event": "delete_product_image", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


def get_product_dropdown(business_id: str):
    """
    Retrieve products for dropdown.
    """
    try:
        response = products_table.scan(
            FilterExpression=Attr("business_id").eq(business_id)
        )
        products = []
        for item in response.get("Items", []):
            products.append(
                {
                    "code": item.get("product_id", ""),
                    "name": item.get("product_name", ""),
                    "price": Decimal(item.get("price", 0.0)),
                }
            )
        sorted_products = sorted(products, key=lambda x: x["name"])
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(sorted_products, default=str),
        }
    except Exception as e:
        print(json.dumps({"event": "get_product_dropdown", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


# Helpers


def delete_product(product_id: str):
    """
    Delete a product by its ID.
    """
    try:
        response = products_table.get_item(Key={"product_id": product_id})

        if "Item" in response:
            item = response["Item"]
            if "imagesUrl" in item:
                for image_url in item["imagesUrl"]:
                    delete_image(image_url)

        products_table.delete_item(Key={"product_id": product_id})
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": "Categoría eliminada correctamente"}),
        }
    except Exception as e:
        print(json.dumps({"event": "delete_product", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }


def delete_image(file_url):
    try:
        file_name = file_url.split("https://qatalo.s3.us-east-1.amazonaws.com/")[1]
        s3.delete_object(Bucket=os.getenv("BUCKET_NAME"), Key=file_name)
        return True
    except Exception as e:
        print(
            json.dumps(
                {
                    "event": "delete_image",
                    "error": str(e),
                    "trace": traceback.format_exc(),
                }
            )
        )
        return False


def upload_image(file_info, user_id=None, type_file="image", product_id=None):
    try:
        file_name = (
            f"business/products/{product_id}_{type_file}{file_info['extension']}"
        )
        file_url = f"https://{os.getenv('BUCKET_NAME')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{file_name}"
        with io.BytesIO(file_info["content"]) as fileobj:
            s3.upload_fileobj(
                Fileobj=fileobj,
                Bucket=os.getenv("BUCKET_NAME"),
                Key=file_name,
                ExtraArgs={"ContentType": file_info["content_type"]},
            )

            # para no incluir binario en la respuesta
        del file_info["content"]
        return file_url
    except Exception as e:
        print(
            json.dumps(
                {
                    "event": "upload_image",
                    "error": str(e),
                    "trace": traceback.format_exc(),
                }
            )
        )
        return None
