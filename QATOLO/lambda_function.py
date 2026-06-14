import base64
import json

# from contact_team import contact_team_routes
from contact_team import contact_team_routes
from customers import customers_routes
from payment_methods import payment_methods_routes
from products import products_routes
from business import business_routes
from users import users_routes
from paddle import paddle_routes
from categories import categories_routes
from offers import offers_routes 


def lambda_handler(event, context):
    try:
        headers = event.get('headers', {})
        arn = context.invoked_function_arn
        alias = arn.split(":")[-1]
        auth_header = headers.get(
            'Authorization') or headers.get('authorization')
        token = auth_header
        decoded = ""
        user_name = ""
        user_id = ""
        if token is not None:
            try:
                decoded = decode_jwt_payload(token)
                user_name = decoded.get("email")
                user_id = decoded.get("sub")
            except:
                pass
        path = event.get('rawPath', '')
        method = event.get('requestContext', {}).get(
            'http', {}).get('method', '')
        if "paddle" in path:
            return paddle_routes(event, method=method, path=path, alias=alias)
        if "users" in path:
            return users_routes(path=path, method=method, event=event, alias=alias)
        if "businesses" in path:
            return business_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "categories" in path:
            return categories_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "products" in path:
            return products_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "payment_methods" in path:
            return payment_methods_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "customers" in path:
            return customers_routes(path=path, method=method, event=event, user_name=user_name, user_id=user_id, alias=alias)
        if "team" in path:
            return contact_team_routes(path=path, method=method, event=event, alias=alias)
        if "offers" in path:
            return offers_routes(path=path, method=method, event=event, user_id=user_id, alias=alias)
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
