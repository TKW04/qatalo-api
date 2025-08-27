import base64
import json

from products import products_routes
from business import business_routes
from users import users_routes
from paddle import paddle_routes
from categories import categories_routes


def lambda_handler(event, context):
    try:
        headers = event.get('headers', {})
        auth_header = headers.get(
            'Authorization') or headers.get('authorization')
        token = auth_header
        decoded = ""
        user_name = ""
        user_id = ""
        if token is not None:
            decoded = decode_jwt_payload(token)
            user_name = decoded.get("email")
            user_id = decoded.get("sub")

        path = event.get('rawPath', '')
        method = event.get('requestContext', {}).get(
            'http', {}).get('method', '')
        if "paddle" in path:
            return paddle_routes(event, user_name=user_name, method=method, path=path)
        if "users" in path:
            return users_routes(path=path, method=method, event=event)
        if "businesses" in path:
            return business_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id)
        if "categories" in path:
            return categories_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id)
        if "products" in path:
            return products_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id)

    except Exception as e:
        print(f"Error processing request: {str(e)}")
        return {
            'statusCode': 500,
            'body': str(e)
        }


def decode_jwt_payload(token):
    try:
        payload = token.split('.')[1]
        padded = payload + '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        return json.loads(decoded_bytes)
    except Exception as e:
        print(f"Error decoding JWT payload: {str(e)}")
        return {}
