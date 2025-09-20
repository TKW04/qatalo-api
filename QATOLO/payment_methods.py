import re
import boto3
import json
import os
import uuid
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
payment_methods_table = dynamodb.Table("qatalo.payment_methods")


def payment_methods_routes(path, method, event, user_name, user_id):

    if path == "/payment_methods" and method == 'GET':
        return get_payment_methods_by_user_id(user_id=user_id)
    if path == "/payment_methods" and method == 'POST':
        return create_payment_method(event=event, user_name=user_name, user_id=user_id)

    match = re.fullmatch(r'/payment_methods/([^/]+)', path)
    if match:
        payment_method_id = match.group(1)
        if method == 'PUT':
            return update_payment_method(event=event, user_name=user_name, payment_method_id=payment_method_id, user_id=user_id)
        if method == 'DELETE':
            return delete_payment_method(payment_method_id)
        if method == 'GET':
            return get_payment_methods_by_business_id(business_id=payment_method_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_payment_methods_by_user_id(user_id: str):
    """
    Retrieve all payment_methods from the database.
    """
    try:
        response = payment_methods_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        payment_methods = []
        for item in response.get("Items", []):
            payment_methods.append({
                "payment_method_id": item.get("payment_method_id", ""),
                "payment_method_name": item.get("payment_method_name", ""),
                "user_id": item.get("user_id", ""),
                "business_id": item.get("business_id", ""),
                "payment_type": item.get("payment_type", ""),
                "account_number": item.get("account_number", ""),
                "account_type": item.get("account_type", ""),
                "bank_name": item.get("bank_name", ""),
                "routing_number": item.get("routing_number", ""),
                "owner_name": item.get("owner_name", ""),
                "owner_document": item.get("owner_document", ""),
                "owner_email": item.get("owner_email", ""),
                "swift": item.get("swift", ""),
                "standard_account": item.get("standard_account", ""),
                "payment_link": item.get("payment_link", ""),
                "currency": item.get("currency", "")

            })
        return {
            'statusCode': 200,  # No uses 204
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': '*'
            },
            'body': json.dumps(payment_methods, default=str)
        }
    except Exception as e:
        print(json.dumps({"event": "get_payment_methods", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def get_payment_methods_by_business_id(business_id: str):
    """
    Retrieve all payment_methods from the database.
    """
    try:
        response = payment_methods_table.scan(
            FilterExpression=Attr('business_id').eq(business_id)
        )
        payment_methods = []
        for item in response.get("Items", []):
            payment_methods.append({
                "payment_method_id": item.get("payment_method_id", ""),
                "payment_method_name": item.get("payment_method_name", ""),
                "user_id": item.get("user_id", ""),
                "business_id": item.get("business_id", ""),
                "payment_type": item.get("payment_type", ""),
                "account_number": item.get("account_number", ""),
                "account_type": item.get("account_type", ""),
                "bank_name": item.get("bank_name", ""),
                "routing_number": item.get("routing_number", ""),
                "owner_name": item.get("owner_name", ""),
                "owner_document": item.get("owner_document", ""),
                "owner_email": item.get("owner_email", ""),
                "swift": item.get("swift", ""),
                "standard_account": item.get("standard_account", ""),
                "payment_link": item.get("payment_link", ""),
                "currency": item.get("currency", "")

            })
        return {
            'statusCode': 200,  # No uses 204
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': '*'
            },
            'body': json.dumps(payment_methods, default=str)
        }
    except Exception as e:
        print(json.dumps({"event": "get_payment_methods", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_payment_method(event, user_name, user_id):
    """
    Create a new payment_method.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        payment_methods_table.put_item(
            Item={
                "payment_method_id": str(uuid.uuid4()),
                "payment_method_name": body.get('payment_method_name', ''),
                "business_id": body.get('business_id', ''),
                "user_id": user_id,
                "payment_type": body.get('payment_type', ''),
                "account_number": body.get('account_number', ''),
                "account_type": body.get('account_type', ''),
                "bank_name": body.get('bank_name', ''),
                "routing_number": body.get('routing_number', ''),
                "owner_name": body.get('owner_name', ''),
                "owner_document": body.get('owner_document', ''),
                "owner_email": body.get('owner_email', ''),
                "swift": body.get('swift', ''),
                "standard_account": body.get('standard_account', ''),
                "payment_link": body.get('payment_link', ''),
                "currency": body.get('currency', ''),
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
        print(json.dumps({"event": "create_payment_method", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_payment_method(event, user_name, payment_method_id, user_id):
    """
    Update an existing payment_method.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        payment_methods_table.update_item(
            Key={"payment_method_id": payment_method_id},
            UpdateExpression="SET business_id = :business_id, payment_method_name = :payment_method_name, payment_type = :payment_type, account_number = :account_number, account_type = :account_type, bank_name = :bank_name, routing_number = :routing_number, owner_name = :owner_name, owner_document = :owner_document, owner_email = :owner_email, swift = :swift, standard_account = :standard_account, payment_link = :payment_link, currency = :currency, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':business_id': body.get('business_id', ''),
                ':payment_method_name': body.get('payment_method_name', ''),
                ':payment_type': body.get('payment_type', ''),
                ':account_number': body.get('account_number', ''),
                ':account_type': body.get('account_type', ''),
                ':bank_name': body.get('bank_name', ''),
                ':routing_number': body.get('routing_number', ''),
                ':owner_name': body.get('owner_name', ''),
                ':owner_document': body.get('owner_document', ''),
                ':owner_email': body.get('owner_email', ''),
                ':swift': body.get('swift', ''),
                ':payment_link': body.get('payment_link', ''),
                ':currency': body.get('currency', ''),
                ':standard_account': body.get('standard_account', ''),
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


def delete_payment_method(payment_method_id: str):
    """
    Delete a payment_method by its ID.
    """
    try:
        payment_methods_table.delete_item(
            Key={"payment_method_id": payment_method_id})
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Categoría eliminada correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "delete_payment_method", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
