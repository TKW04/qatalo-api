import json
import re
import boto3
import os

from zoneinfo import ZoneInfo
from datetime import datetime
from SendMails.mails import send_forgot_password_email, welcome_email

cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')
FRONT_END_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')


def users_routes(path, method, event, alias):

    if path == f"/{alias}/users" and method == 'POST':
        return register_user(event=event)
    if path == f"/{alias}/users/change-password" and method == 'POST':
        return reset_password(event=event)

    match_forgot = re.fullmatch(rf'/{alias}/users/forgot-password/([^/]+)', path)
    if match_forgot:
        user_name = match_forgot.group(1)
        if method == 'POST':
            return forgot_password(user_name=user_name)

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
            'body': json.dumps(user_response, default=str)
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
                {"Name": "custom:due_date", "Value": datetime.now(
                    ZoneInfo("America/Santo_Domingo")).strftime("%Y-%m-%d")}

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

        loginLink = f"{FRONT_END_URL}/login"
        return welcome_email(body.get('email'),
                             to_name=f"{body.get('given_name')} {body.get('family_name')}",
                             loginLink=loginLink)

    except Exception as e:
        print(json.dumps({"event": "create_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def forgot_password(user_name):
    try:

        user = cognito.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=user_name
        )

        # Formatear los atributos
        attrs = {attr['Name']: attr['Value']
                 for attr in user['UserAttributes']}
        resetLink = f"{FRONT_END_URL}/reset-password/{attrs.get('sub')}"
        return send_forgot_password_email(to_address=attrs.get('email'),
                                          to_name=f"{attrs.get('given_name')} {attrs.get('family_name')}",
                                          resetLink=resetLink)

    except Exception as e:
        print(json.dumps({"event": "forgot_password", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def reset_password(event):
    try:
        body = json.loads(event.get('body', '{}'))
        user_name = body.get('username')
        new_password = body.get('password')
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=user_name,
            Password=new_password,
            Permanent=True
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Contraseña restablecida correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "reset_password", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
