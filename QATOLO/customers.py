from decimal import Decimal
import re
import boto3
import json
import os
import uuid
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
customers_table = dynamodb.Table("qatalo.customers")


def customers_routes(path, method, event, user_name, user_id):

    if path == "/customers" and method == 'GET':
        return get_customers_by_user_id(user_id=user_id)
    if path == "/customers" and method == 'POST':
        return create_customer(event=event)

    match = re.fullmatch(r'/customers/([^/]+)', path)
    if match:
        customer_id = match.group(1)
        if method == 'PUT':
            return update_customer(event=event, user_name=user_name, customer_id=customer_id, user_id=user_id)
        # if method == 'GET':
        #     return get_customers_by_business_id(business_id=customer_id)
        if method == 'DELETE':
            return delete_customer(customer_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrado'
    }


def get_customers_by_user_id(user_id: str):
    """
    Retrieve all customers from the database.
    """
    try:
        response = customers_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        customers = []
        for item in response.get("Items", []):
            customers.append({
                "customer_id": item.get("customer_id", ""),
                "business_id": item.get("business_id", ""),
                "given_name": item.get("given_name", ""),
                "family_name": item.get("family_name", ""),
                "transactions": item.get("transactions", []),
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "user_id": item.get("user_id", "")
            })
        return {
            'statusCode': 200,  # No uses 204
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': '*'
            },
            'body': json.dumps(customers, default=str)
        }
    except Exception as e:
        print(json.dumps({"event": "get_customers", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_customer(event):
    """
    Create a new customer.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        business_id = body.get('business_id', '')
        response = customers_table.scan(
            FilterExpression=Attr('business_id').eq(business_id)
        )
        transactions = []
        if response.get("Items", []):
            items = response.get("Items", [])
            if any(item.get("email", "") == body.get('email', '') for item in items):
                customer_id = next(item.get("customer_id", "") for item in items if item.get(
                    "email", "") == body.get('email', ''))
                transactions = next(item.get("transactions", []) for item in items if item.get(
                    "customer_id", "") == customer_id)

                transaction = body.get('transaction', {})
                payment_method = transaction.get('payment_method', {})
                transactions.append({
                    "transaction_id": str(uuid.uuid4()),
                    "product_id": transaction.get('product_id', ''),
                    "product_name": transaction.get('product_name', ''),
                    "quantity": transaction.get('quantity', 1),
                    "price": Decimal(transaction.get("price", 0.00)),
                    "status": "PENDING",
                    "payment_method": {
                        "payment_method_id": payment_method.get('payment_method_id', ''),
                        "payment_type": payment_method.get('payment_type', ''),
                        "currency": payment_method.get('currency', '')
                    },
                    "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "create_user": body.get('email', '')
                })
                customers_table.update_item(
                    Key={"customer_id": customer_id},
                    UpdateExpression="SET given_name = :given_name, family_name = :family_name, email = :email, phone = :phone,  transactions = :transactions, update_date = :update_date, update_user = :update_user",
                    ExpressionAttributeValues={
                        ':given_name': body.get('given_name', ''),
                        ':family_name': body.get('family_name', ''),
                        ':email': body.get('email', ''),
                        ':phone': body.get('phone', ''),
                        ':transactions': transactions,
                        ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ':update_user': body.get('email', '')
                    },
                    ReturnValues="UPDATED_NEW")
            else:
                return create_customer_transaction(body=body)
        else:
            return create_customer_transaction(body=body)
    except Exception as e:
        print(json.dumps({"event": "create_customer", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_customer_transaction(body):
    transactions = []
    transaction = body.get('transaction', {})
    payment_method = transaction.get('payment_method', {})
    transactions.append({
        "transaction_id": str(uuid.uuid4()),
        "product_id": transaction.get('product_id', ''),
        "product_name": transaction.get('product_name', ''),
        "quantity": transaction.get('quantity', 1),
        "price": Decimal(transaction.get("price", 0.00)),
        "status": "PENDING",
        "payment_method": {
            "payment_method_id": payment_method.get('payment_method_id', ''),
            "payment_type": payment_method.get('payment_type', ''),
            "currency": payment_method.get('currency', '')
        },
        "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "create_user": body.get('email', '')
    })
    customers_table.put_item(
        Item={
            "customer_id": str(uuid.uuid4()),
            "business_id": body.get('business_id', ''),
            "given_name": body.get('given_name', ''),
            "family_name": body.get('family_name', ''),
            "email": body.get('email', ''),
            "phone": body.get('phone', ''),
            "transactions": transactions,
            "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "create_user": body.get('email', ''),
            "update_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "update_user": body.get('email', '')
        }
    )
    return {
        'statusCode': 200,
        'headers': {'Access-Control-Allow-Origin': '*'},
        'body': json.dumps({'message': 'Cliente creado correctamente'})
    }


def update_customer(event, user_name, customer_id, user_id):
    """
    Update an existing customer.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        customers_table.update_item(
            Key={"customer_id": customer_id},
            UpdateExpression="SET given_name = :given_name, family_name = :family_name, email = :email, phone = :phone, user_id = :user_id, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':given_name': body.get('given_name', ''),
                ':family_name': body.get('family_name', ''),
                ':email': body.get('email', ''),
                ':phone': body.get('phone', ''),
                ':user_id': user_id,
                ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ':update_user': user_name
            },
            ReturnValues="UPDATED_NEW"
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Cliente actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "create_member", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def delete_customer(customer_id: str):
    """
    Delete a customer by its ID.
    """
    try:
        customers_table.delete_item(Key={"customer_id": customer_id})
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Cliente eliminado correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "delete_customer", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
