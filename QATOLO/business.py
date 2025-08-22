
import io
import traceback
import json
import re
import uuid
import boto3
import os

from datetime import datetime
from requests_toolbelt.multipart import decoder

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
business_table = dynamodb.Table("qatalo.business")
s3 = boto3.client('s3')


def business_routes(path, method, event, user_name):

    match = re.fullmatch(r'/business/([^/]+)', path)
    if match:
        id = match.group(1)
        if method == 'GET':
            return get_business(user_id=id)
        elif method == 'PUT':
            return update_member(event=event, user_name=user_name, business_id=id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_business(user_id: str):
    """
    Retrieve a user by their ID.
    """
    try:
        response = business_table.query(
            KeyConditionExpression="name = :v_name",
            ExpressionAttributeValues={
                ":user_id": {"S": user_id}
            }
        )
        if "Items" in response and len(response["Items"]) > 0:
            business = {
                "business_id": response["Items"][0]["business_id"],
                "business_name": response["Items"][0]["business_name"],
                "business_description": response["Items"][0]["business_description"],
                "business_slug": response["Items"][0]["business_slug"],
                "business_phone": response["Items"][0]["business_phone"],
                "business_logo_url": response["Items"][0]["business_logo_url"]
            }

            return {
                'statusCode': 200,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps(business.__dict__, default=str)
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


def create_member(event, user_name):
    """
    Create a new member.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        business_table.put_item(
            Item={
                "business_id": str(uuid.uuid4()),
                "first_name": body.get('first_name', ''),
                "last_name": body.get('last_name', ''),
                "email": body.get('email', ''),
                "address": body.get('address', ''),
                "phone": body.get('phone', ''),
                "birth_date": body.get('birth_date', ''),
                "emergency_contact": body.get('emergency_contact', ''),
                "emergency_contact_phone": body.get('emergency_contact_phone', ''),
                "positions": body.get('positions', ''),
                "is_active": body.get('is_active', True),
                "creation_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "creation_user": user_name,
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
        print(json.dumps({"event": "create_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_member(event, user_name, business_id):
    """
    Update an existing user.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        business_table.update_item(
            Key={"business_id": business_id},
            UpdateExpression="SET first_name = :first_name, last_name = :last_name, email = :email, address = :address, phone = :phone, birth_date = :birth_date, emergency_contact = :emergency_contact, emergency_contact_phone = :emergency_contact_phone, positions = :p, is_active = :is_active, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':first_name': body.get('first_name', ''),
                ':last_name': body.get('last_name', ''),
                ':email': body.get('email', ''),
                ':address': body.get('address', ''),
                ':phone': body.get('phone', ''),
                ':birth_date': body.get('birth_date', ''),
                ':emergency_contact': body.get('emergency_contact', ''),
                ':emergency_contact_phone': body.get('emergency_contact_phone', ''),
                ':p': body.get('positions', ''),
                ':is_active': body.get('is_active', True),
                ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ':update_user': user_name
            },
            ReturnValues="UPDATED_NEW"
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Miembro de la comunidad actualizado correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "update_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def upload_image(file_info, user_id=None, type_file="photo"):
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
