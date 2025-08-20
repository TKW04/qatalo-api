import json
import re
from zoneinfo import ZoneInfo
import boto3
import os
from datetime import datetime

cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')


def users_routes(path, method, event):
    print(f"Processing users route: {path} with method: {method}")
    if path == "/users" and method == 'POST':
        return register_user(event=event)

    match = re.fullmatch(r'/users/([^/]+)', path)
    if match:
        username = match.group(1)
        if method == 'GET':
            return get_user(username)
        elif method == 'PUT':
            return update_user(username, event)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_user(username):
    try:
        user = cognito.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=username
        )

        # Formatear los atributos
        attrs = {attr['Name']: attr['Value']
                 for attr in user['UserAttributes']}

        user_response = {"id": attrs.get('sub'),
                         "email": attrs.get('email'),
                         "given_name": attrs.get('given_name'),
                         "family_name": attrs.get('family_name'),
                         "price_id": attrs.get('custom:price_id'),
                         "transaction_id": attrs.get('custom:transaction_id'),
                         "transaction_status": attrs.get('custom:transaction_status'),
                         "customer_id": attrs.get('custom:customer_id'),
                         "due_date": attrs.get('custom:due_date'),
                         "status": user['Enabled']}

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps(user_response.__dict__, default=str)
        }

    except cognito.exceptions.UserNotFoundException as ex:
        print(json.dumps({"event": "get_user", "Error": str(ex)}))
        return {
            'statusCode': 404,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario no encontrado'})
        }
    except Exception as e:
        print(json.dumps({"event": "get_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }


def update_user(username, event):
    try:
        # Parsear el body recibido (debe ser JSON)
        body = json.loads(event.get('body', '{}'))
        # Ejecutar la actualización en Cognito
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[
                {"Name": "given_name",
                 "Value": body['given_name']},
                {"Name": "family_name",
                 "Value": body.get('family_name')},
                {"Name": "phone_number", "Value": body.get('phone_number')}
            ]
        )
        if 'password' in body and body['password'] != '':
            nueva_clave = body['password']
            cognito.admin_set_user_password(
                UserPoolId=USER_POOL_ID,
                Username=username,
                Password=nueva_clave,
                Permanent=True
            )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "update_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def register_user(event):
    try:

        body = json.loads(event.get('body', '{}'))
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email'),
            UserAttributes=[
                {"Name": "email", "Value": body.get('email')},
                {"Name": "email_verified", "Value": 'true'},
                {"Name": "given_name", "Value": body.get('given_name')},
                {"Name": "family_name", "Value": body.get('family_name')},
                {"Name": "custom:customer_id", "Value": ""},
                {"Name": "custom:price_id", "Value": "0"},
                {"Name": "custom:transaction_id", "Value": "0"},
                {"Name": "custom:transaction_status", "Value": "pending"},
                {"Name": "custom:due_date", "Value": datetime.now(ZoneInfo("America/Santo_Domingo")).strftime("%Y-%m-%d")}

            ],
            MessageAction="SUPPRESS"  # No enviar email

        )
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email'),
            Password=body.get('password'),
            Permanent=True
        )
        cognito.admin_enable_user(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email')
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "create_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def change_password(username, event):
    try:
        body = json.loads(event.get('body', '{}'))
        nueva_clave = body['password']
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=username,
            Password=nueva_clave,
            Permanent=True
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Contraseña actualizada correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "change_password", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
