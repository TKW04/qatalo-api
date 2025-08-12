import base64
import json

from paddle import paddle_routes


def lambda_handler(event, context):
    try:

        headers = event.get('headers', {})
        auth_header = headers.get(
            'Authorization') or headers.get('authorization')
        token = auth_header
        decoded = ""
        user_name = ""
        if token is not None:
            decoded = decode_jwt_payload(token)
            user_name = decoded.get("email")
        path = event.get('rawPath', '')
        method = event.get('requestContext', {}).get(
            'http', {}).get('method', '')
        if "paddle" in path:
            return paddle_routes(event)

    except Exception as e:
        print(f"Error processing request: {str(e)}")
        return {
            'statusCode': 500,
            'body': str(e)
        }


def decode_jwt_payload(token):
    payload = token.split('.')[1]
    padded = payload + '=' * (-len(payload) % 4)
    decoded_bytes = base64.urlsafe_b64decode(padded)
    return json.loads(decoded_bytes)
