import base64
import io
import os
import re
import traceback
import boto3
import json
import uuid

from json import decoder
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
products_table = dynamodb.Table("qatalo.products")
s3 = boto3.client('s3')


def products_routes(path, method, event, user_name, user_id):

    if path == "/products" and method == 'GET':
        return get_products_by_user_id(user_id=user_id)
    if path == "/products" and method == 'POST':
        return create_product(event=event, user_name=user_name, user_id=user_id)

    match = re.fullmatch(r'/products/([^/]+)', path)
    if match:
        product_id = match.group(1)
        if method == 'GET':
            return get_product(product_id)
        elif method == 'PUT':
            return update_product(event=event, user_name=user_name, product_id=product_id, user_id=user_id)
        elif method == 'DELETE':
            return delete_product(product_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_products_by_user_id(user_id: str):
    """
    Retrieve all products from the database.
    """
    try:
        response = products_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        products = []
        for item in response.get("Items", []):
            products.append({
                "product_id": item.get("product_id", ""),
                "business_id": item.get("business_id", ""),
                "name": item.get("product_name", ""),
                "description": item.get("product_description", ""),
                "price": item.get("product_price", 0.0),
                "currency": item.get("product_currency", ""),
                "imagesUrl": item.get("product_image_urls", []),
                "category_id": item.get("product_category_id", ""),
                "is_available": item.get("product_available", False),
                "order": item.get("product_order", 0)
            })
        return {
            'statusCode': 200,  # No uses 204
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': '*'
            },
            'body': json.dumps(products, default=str)
        }
    except Exception as e:
        print(json.dumps({"event": "get_products", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def get_product(product_id: str):
    """
    Retrieve a product by its ID.
    """
    try:
        response = products_table.get_item(Key={"product_id": product_id})
        if "Item" in response:
            product = {
                "product_id": response["Item"].get("product_id", ""),
                "description": response["Item"].get("product_description", ""),
                "price": response["Item"].get("product_price", 0.0),
                "currency": response["Item"].get("product_currency", ""),
                "imagesUrl": response["Item"].get("product_image_urls", []),
                "category_id": response["Item"].get("product_category_id", ""),
                "is_available": response["Item"].get("product_available", False),
                "user_id": response["Item"].get("user_id", "")
            }

            return {
                'statusCode': 200,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps(product, default=str)
            }
        else:
            return None
    except Exception as e:
        print(json.dumps({"event": "get_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_product(event, user_name, user_id):
    """
    Create a new product.
    """
    try:

        # Paso 1: Decodificar el body de base64
        body_bytes = base64.b64decode(event["body"])
        # Paso 2: Obtener content-type (con boundary)
        content_type = event["headers"].get(
            "content-type") or event["headers"].get("Content-Type")
        # Paso 3: Parsear multipart
        multipart_data = decoder.MultipartDecoder(body_bytes, content_type)
        file_infos = []  # Para múltiples archivos
        product_create = {}

        for part in multipart_data.parts:
            headers = part.headers[b'Content-Disposition'].decode()
            name = headers.split('name="')[1].split('"')[0]

            if "filename=" in headers:
                original_filename = headers.split(
                    'filename="')[1].split('"')[0]
                if original_filename and len(part.content) > 0:
                    extension = os.path.splitext(original_filename)[1].lower()
                    part_content_type = part.headers.get(
                        b"Content-Type", b"application/octet-stream").decode()
                    file_infos.append({
                        "field_name": name,
                        "original_filename": original_filename,
                        "content": part.content,
                        "extension": extension,
                        "content_type": part_content_type
                    })
            else:
                product_create[name] = part.text

        product_id = str(uuid.uuid4())

        if file_infos:
            count = 0
            images = []
            for file_info in file_infos:
                count += 1
                images.append(upload_image(
                    file_info=file_info, user_id=user_id, type_file=f"image{count}", product_id=product_id))
            product_create['imagesUrl'] = images[0] if images else []
        products_table.put_item(
            Item={
                "product_id": product_id,
                "product_name": product_create.get("name", ""),
                "description": product_create.get("description", ""),
                "price": product_create.get("price", 0.0),
                "currency": product_create.get("currency", ""),
                "imagesUrl": product_create.get("imagesUrl", []),
                "category_id": product_create.get("category_id", ""),
                "is_available": product_create.get("is_available", "unavailable"),
                "user_id": user_id,
                "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "create_user": user_name,
                "update_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "update_user": user_name
            }
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Categoría creada correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "create_product", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_product(event, user_name, product_id, user_id):
    """
    Update an existing product.
    """
    try:
        # Paso 1: Decodificar el body de base64
        body_bytes = base64.b64decode(event["body"])
        # Paso 2: Obtener content-type (con boundary)
        content_type = event["headers"].get(
            "content-type") or event["headers"].get("Content-Type")
        # Paso 3: Parsear multipart
        multipart_data = decoder.MultipartDecoder(body_bytes, content_type)
        file_infos = []  # Para múltiples archivos
        product_update = {}

        for part in multipart_data.parts:
            headers = part.headers[b'Content-Disposition'].decode()
            name = headers.split('name="')[1].split('"')[0]

            if "filename=" in headers:
                original_filename = headers.split(
                    'filename="')[1].split('"')[0]
                if original_filename and len(part.content) > 0:
                    extension = os.path.splitext(original_filename)[1].lower()
                    part_content_type = part.headers.get(
                        b"Content-Type", b"application/octet-stream").decode()
                    file_infos.append({
                        "field_name": name,
                        "original_filename": original_filename,
                        "content": part.content,
                        "extension": extension,
                        "content_type": part_content_type
                    })
            else:
                product_update[name] = part.text

        products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET product_name = :product_name, description = :description, price = :price, currency = :currency, imagesUrl = :imagesUrl, category_id = :category_id, user_id = :user_id, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':description': product_update.get('description', ''),
                ':product_name': product_update.get('name', ''),
                ':price': product_update.get('price', 0.0),
                ':currency': product_update.get('currency', ''),
                ':imagesUrl': product_update.get('imagesUrl', []),
                ':category_id': product_update.get('category_id', ''),
                ':user_id': user_id,
                ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ':update_user': user_name
            },
            ReturnValues="UPDATED_NEW"
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Categoría actualizada correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "create_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def delete_product(product_id: str):
    """
    Delete a product by its ID.
    """
    try:
        products_table.delete_item(Key={"product_id": product_id})
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Categoría eliminada correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "delete_product", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def upload_image(file_info, user_id=None, type_file="image", product_id=None):
    try:
        file_name = f"business/products/{product_id}_{type_file}{file_info['extension']}"
        file_url = f"https://{os.getenv('BUCKET_NAME')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{file_name}"
        with io.BytesIO(file_info["content"]) as fileobj:
            s3.upload_fileobj(
                Fileobj=fileobj,
                Bucket=os.getenv('BUCKET_NAME'),
                Key=file_name,
                ExtraArgs={"ContentType": file_info['content_type']}
            )

            # para no incluir binario en la respuesta
        del file_info["content"]
        return file_url
    except Exception as e:
        print(json.dumps({
            "event": "upload_image",
            "error": str(e),
            "trace": traceback.format_exc()
        }))
        return None
