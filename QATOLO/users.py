import json
import re
import boto3
import os


cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')


def users_routes(path, method, event):

    # if path == "/users" and method == 'GET':
    #     return list_users()
    if path == "/users" and method == 'POST':
        return register_user(event=event)

    match = re.fullmatch(r'/users/([^/]+)', path)
    if match:
        username = match.group(1)
        if method == 'GET':
            return get_user(username)
        elif method == 'PUT':
            return update_user(username, event)

    # match_inactive = re.fullmatch(r'/users/inactive/([^/]+)', path)
    # if match_inactive:
    #     username = match_inactive.group(1)
    #     if method == 'PUT':
    #         return inactive_user(username)

    # match_active = re.fullmatch(r'/users/active/([^/]+)', path)
    # if match_active:
    #     username = match_active.group(1)
    #     if method == 'PUT':
    #         return active_user(username)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


# def list_users():
#     try:
#         response = cognito.list_users(UserPoolId=USER_POOL_ID)
#         users = []

#         for user in response['Users']:
#             user_response = Users(id=None, email=None, given_name=None,
#                                   family_name=None, role=None, status=user['Enabled'])
#             for attr in user['Attributes']:
#                 name = attr['Name']
#                 value = attr['Value']

#                 if name == 'sub':
#                     user_response.id = value
#                 elif name == 'email':
#                     user_response.email = value
#                 elif name == 'given_name':
#                     user_response.given_name = value
#                 elif name == 'family_name':
#                     user_response.family_name = value
#                 elif name == 'custom:role':
#                     user_response.role = value
#             # if user_response.role != "ROOT":
#             users.append(user_response.__dict__)
#         return {
#             'statusCode': 200,  # No uses 204
#             'headers': {
#                 'Access-Control-Allow-Origin': '*',
#                 'Access-Control-Allow-Headers': '*',
#                 'Access-Control-Allow-Methods': '*'
#             },
#             'body': json.dumps(users, default=str)
#         }
#     except Exception as e:
#         print(json.dumps({"event": "list_users", "Error": str(e)}))
#         return {
#             'statusCode': 500,
#             'headers': {'Access-Control-Allow-Origin': '*'}
#         }


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
                         "phone_number": attrs.get('phone_number'),
                         "price_id": attrs.get('custom:price_id'),
                         "transaction_id": attrs.get('custom:transaction_id'),
                         "transaction_status": attrs.get('custom:transaction_status'),
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
                {"Name": "email", "Value": body.get('email'), },
                {"Name": "email_verified", "Value": 'true'},
                {"Name": "given_name", "Value": body.get('given_name')},
                {"Name": "family_name", "Value": body.get('family_name')},
                {"Name": "phone_number", "Value": body.get('phone_number')}
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


def active_user(username):
    try:

        # Ejecutar la actualización en Cognito
        cognito.admin_enable_user(
            UserPoolId=USER_POOL_ID,
            Username=username
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "active_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def inactive_user(username):
    try:

        # Ejecutar la actualización en Cognito
        cognito.admin_disable_user(
            UserPoolId=USER_POOL_ID,
            Username=username
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "inactive_user", "Error": str(e)}))
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
