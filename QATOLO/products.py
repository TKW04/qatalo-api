import base64
from decimal import Decimal
import io
import os
import re
import traceback
import boto3
import json
import uuid

from requests_toolbelt.multipart import decoder
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
    if path == "/products/delete/image" and method == 'DELETE':
        return delete_product_image(event=event)

    match = re.fullmatch(r'/products/([^/]+)', path)
    if match:
        product_id = match.group(1)
        if method == 'PUT':
            return update_product(event=event, user_name=user_name, product_id=product_id, user_id=user_id)
        if method == 'GET':
            return get_products_by_business_id(business_id=product_id)
        if method == 'DELETE':
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

            images = []
            imagesUrl = item.get("imagesUrl", [])
            for image in imagesUrl:
                images.append({
                    "image": image
                })

            products.append({
                "product_id": item.get("product_id", ""),
                "business_id": item.get("business_id", ""),
                "name": item.get("product_name", ""),
                "description": item.get("description", ""),
                "price": item.get("price", 0.0),
                "quantity": item.get("quantity", 0),
                "orden": item.get("orden", 0),
                "just_one": item.get("just_one", False),
                "show_quantity": item.get("show_quantity", False),
                "terms": item.get("terms", ""),
                "currency": item.get("currency", ""),
                "imagesUrl": images,
                "category_id": item.get("category_id", ""),
                "is_available": item.get("is_available", False)
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


def get_products_by_business_id(business_id: str):
    """
    Retrieve all products from the database.
    """
    try:
        response = products_table.scan(
            FilterExpression=Attr('business_id').eq(business_id)
        )
        products = []
        for item in response.get("Items", []):

            images = []
            imagesUrl = item.get("imagesUrl", [])
            for image in imagesUrl:
                images.append({
                    "image": image
                })

            products.append({
                "product_id": item.get("product_id", ""),
                "business_id": item.get("business_id", ""),
                "name": item.get("product_name", ""),
                "description": item.get("description", ""),
                "price": item.get("price", 0.00),
                "quantity": item.get("quantity", 0),
                "orden": item.get("orden", 0),
                "just_one": item.get("just_one", False),
                "terms": item.get("terms", ""),
                "show_quantity": item.get("show_quantity", False),
                "currency": item.get("currency", ""),
                "imagesUrl": images,
                "category_id": item.get("category_id", ""),
                "is_available": item.get("is_available", False)
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
            images = []
            for file_info in file_infos:
                images.append(upload_image(
                    file_info=file_info, user_id=user_id, type_file=str(uuid.uuid4()), product_id=product_id))
            product_create['imagesUrl'] = images if images else []

        products_table.put_item(
            Item={
                "product_id": product_id,
                "business_id": product_create.get("business_id", ""),
                "product_name": product_create.get("name", ""),
                "description": product_create.get("description", ""),
                "price": Decimal(product_create.get("price", 0.00)),
                "quantity": product_create.get("quantity", 0),
                "orden": product_create.get("orden", 0),
                "just_one": product_create.get("just_one", False),
                "terms": product_create.get("terms", ""),
                "show_quantity": product_create.get("show_quantity", False),
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

        response = products_table.get_item(Key={"product_id": product_id})
        imagesUrl = []
        if "Item" in response:
            imagesUrl = response["Item"].get("imagesUrl", [])
        images = []
        for image in imagesUrl:
            images.append(image)

        if file_infos:
            for file_info in file_infos:
                images.append(upload_image(
                    file_info=file_info, user_id=user_id, type_file=str(uuid.uuid4()), product_id=product_id))

        products_table.update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET product_name = :product_name, description = :description, price = :price, quantity = :quantity, orden = :orden, just_one = :just_one, show_quantity = :show_quantity, terms = :terms, currency = :currency, imagesUrl = :imagesUrl, category_id = :category_id, is_available = :is_available, user_id = :user_id, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':description': product_update.get('description', ''),
                ':product_name': product_update.get('name', ''),
                ':price': product_update.get('price', 0.0),
                ':quantity': product_update.get('quantity', 0),
                ':orden': product_update.get('orden', 0),
                ':just_one': product_update.get('just_one', False),
                ':show_quantity': product_update.get('show_quantity', False),
                ':terms': product_update.get('terms', ''),
                ':currency': product_update.get('currency', ''),
                ':imagesUrl': images,
                ':category_id': product_update.get('category_id', ''),
                ':is_available': product_update.get('is_available', 'unavailable'),
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


def delete_product_image(event):
    try:
        body = json.loads(event.get('body', '{}'))
        file_url = body.get('file_url', '')
        product_id = body.get('product_id', '')

        response = products_table.get_item(Key={"product_id": product_id})

        if "Item" in response:
            item = response["Item"]
            if "imagesUrl" in item:
                item["imagesUrl"].remove(file_url)
                products_table.update_item(
                    Key={"product_id": product_id},
                    UpdateExpression="SET imagesUrl = :imagesUrl",
                    ExpressionAttributeValues={
                        ':imagesUrl': item["imagesUrl"]
                    },
                    ReturnValues="UPDATED_NEW"
                )

        delete_image(file_url)
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Imagen eliminada correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "delete_product_image", "Error": str(e)}))
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
        response = products_table.get_item(Key={"product_id": product_id})

        if "Item" in response:
            item = response["Item"]
            if "imagesUrl" in item:
                for image_url in item["imagesUrl"]:
                    delete_image(image_url)

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


def delete_image(file_url):
    try:
        file_name = file_url.split(
            'https://qatalo.s3.us-east-1.amazonaws.com/')[1]
        print(file_name)
        s3.delete_object(Bucket=os.getenv('BUCKET_NAME'), Key=file_name)
        return True
    except Exception as e:
        print(json.dumps({
            "event": "delete_image",
            "error": str(e),
            "trace": traceback.format_exc()
        }))
        return False


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
