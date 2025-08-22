
import base64
import io
import traceback
import json
import re
import uuid
import boto3
import os

from datetime import datetime
from requests_toolbelt.multipart import decoder
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
business_table = dynamodb.Table("qatalo.business")
s3 = boto3.client('s3')


def business_routes(path, method, event, user_name, user_id):
    if path == '/businesses' and method == 'POST':
        return create_business(event=event, user_name=user_name, user_id=user_id)

    match = re.fullmatch(r'/businesses/([^/]+)', path)
    if match:
        id = match.group(1)
        if method == 'GET':
            return get_business(user_id=id)
        elif method == 'PUT':
            return update_business(event=event, user_name=user_name, business_id=id, user_id=user_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_business(user_id: str):
    """
    Retrieve a user by their ID.
    """
    try:
        response = business_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        if "Items" in response and len(response["Items"]) > 0:
            business = {
                "business_id": response["Items"][0]["business_id"],
                "name": response["Items"][0]["business_name"],
                "description": response["Items"][0]["business_description"],
                "slug": response["Items"][0]["business_slug"],
                "phone": response["Items"][0]["business_phone"],
                "logo_url": response["Items"][0]["business_logo_url"]
            }

            return {
                'statusCode': 200,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps(business, default=str)
            }
        else:
            return None
    except Exception as e:
        print(json.dumps({"event": "get_business", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_business(event, user_name, user_id):
    """
    Create a new user.
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
        business_create = {}

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
                business_create[name] = part.text

        slug = business_create.get('slug', '')

        response = business_table.scan(
            FilterExpression=Attr('business_slug').eq(slug)
        )

        if response.get('Items'):
            return {
                'statusCode': 400,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'message': 'El slug ya existe'})
            }

        business_id = str(uuid.uuid4())

        if file_infos:
            for file_info in file_infos:
                if file_info['field_name'] == 'logo':
                    business_create['logo_url'] = upload_image(
                        file_info=file_info, user_id=user_id, type_file="logo")

        business_table.put_item(
            Item={
                "business_id": business_id,
                "user_id": user_id,
                "business_name": business_create.get('name', ''),
                "business_description": business_create.get('description', ''),
                "business_slug": business_create.get('slug', ''),
                "business_phone": business_create.get('phone', ''),
                "business_logo_url": business_create.get('logo_url', ''),
                "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "create_user": user_name,
                "update_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "update_user": user_name
            }
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Miembro de la comunidad creado correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "create_business", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_business(event, user_name, business_id, user_id):
    """
    Update an existing user.
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
        business_update = {}

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
                business_update[name] = part.text

        slug = business_update.get('slug', '')

        response = business_table.scan(
            FilterExpression=Attr('business_slug').eq(slug)
        )

        if response.get('Items') and response['Items'][0]['business_id'] != business_id:
            return {
                'statusCode': 400,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'message': 'El slug ya existe'})
            }

        if file_infos:
            for file_info in file_infos:
                if file_info['field_name'] == 'logo':
                    business_update['logo_url'] = upload_image(
                        file_info=file_info, user_id=user_id, type_file="logo")

        business_table.update_item(
            Key={'business_id': business_id},
            UpdateExpression="set user_id=:u, business_name=:n, business_description=:d, business_slug=:s, business_phone=:p, business_logo_url=:l, update_date=:ud, update_user=:uu",
            ExpressionAttributeValues={
                ':u': user_id,
                ':n': business_update.get('name', ''),
                ':d': business_update.get('description', ''),
                ':s': business_update.get('slug', ''),
                ':p': business_update.get('phone', ''),
                ':l': business_update.get('logo_url', ''),
                ':ud': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ':uu': user_name
            }
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Negocio actualizado correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "update_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def upload_image(file_info, user_id=None, type_file="logo"):
    try:
        file_name = f"business/{user_id}_{type_file}{file_info['extension']}"
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
