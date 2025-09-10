import base64
import io
import re
import traceback
import boto3
import json
import os
import uuid

from requests_toolbelt.multipart import decoder
from boto3.dynamodb.conditions import Attr
from datetime import datetime
from decimal import Decimal

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
customers_table = dynamodb.Table("qatalo.customers")
business_table = dynamodb.Table("qatalo.business")
s3 = boto3.client('s3')


def customers_routes(path, method, event, user_name, user_id):

    if path == "/customers" and method == 'GET':
        return get_customers_by_user_id(user_id=user_id)
    if path == "/customers" and method == 'POST':
        return create_customer(event=event)

    if path == "/customers/transactions" and method == 'POST':
        return upload_receipt(event=event)
    if path == "/customers/transactions/cancel" and method == 'POST':
        return cancel_transaction(event=event)
    
    if path == "/customers/transactions/cancelAdmin" and method == 'POST':
        return cancel_transaction(event=event)
    if path == "/customers/transactions/approve" and method == 'POST':
        return approve_transaction(event=event)

    customer_id_match = re.fullmatch(r'/customers/([^/]+)', path)
    if customer_id_match:
        customer_id = customer_id_match.group(1)
        if method == 'PUT':
            return update_customer(event=event, user_name=user_name, customer_id=customer_id, user_id=user_id)
        if method == 'DELETE':
            return delete_customer(customer_id)
        if method == 'GET':
            return get_customer_transaction(customer_id=customer_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrado'
    }


def get_customers_by_user_id(user_id: str):
    """
    Retrieve all customers from the database.
    """
    try:
        business_response = business_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        customers = []
        if "Items" in business_response and len(business_response["Items"]) > 0:
            business_id = business_response["Items"][0].get("business_id", "")
            response = customers_table.scan(
                FilterExpression=Attr('business_id').eq(business_id)
            )

            for item in response.get("Items", []):
                customers.append({
                    "customer_id": item.get("customer_id", ""),
                    "business_id": item.get("business_id", ""),
                    "given_name": item.get("given_name", ""),
                    "family_name": item.get("family_name", ""),
                    "transactions": item.get("transactions", []),
                    "email": item.get("email", ""),
                    "phone": item.get("phone", "")
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


def get_customer_transaction(customer_id: str):
    """
    Retrieve a customer transaction by its ID.
    """
    try:
        response = customers_table.get_item(Key={"customer_id": customer_id})
        if "Item" in response:
            item = response["Item"]\

            customer = {
                "customer_id": item.get("customer_id", ""),
                "business_id": item.get("business_id", ""),
                "given_name": item.get("given_name", ""),
                "family_name": item.get("family_name", ""),
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "transactions": item.get("transactions", [])
            }
            if customer and "transactions" in customer:
                return {
                    'statusCode': 200,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps(customer, default=str)
                }
            else:
                return {
                    'statusCode': 404,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({'message': 'Transacción no encontrada'})
                }
        else:
            return {
                'statusCode': 404,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'message': 'Cliente no encontrado'})
            }
    except Exception as e:
        print(json.dumps(
            {"event": "get_customer_transaction", "Error": str(e)}))
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
                        "currency": payment_method.get('currency', ''),
                        "payment_link": payment_method.get('payment_link', '')
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
            "currency": payment_method.get('currency', ''),
            "payment_link": payment_method.get('payment_link', '')
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


def upload_receipt(event):
    try:
        # Paso 1: Decodificar el body de base64
        body_bytes = base64.b64decode(event["body"])
        # Paso 2: Obtener content-type (con boundary)
        content_type = event["headers"].get(
            "content-type") or event["headers"].get("Content-Type")
        # Paso 3: Parsear multipart
        multipart_data = decoder.MultipartDecoder(body_bytes, content_type)
        file_infos = []  # Para múltiples archivos
        customer_update = {}

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
                customer_update[name] = part.text

            if file_infos:
                images = upload_image(file_info=file_infos[0], customer_id=customer_update.get(
                    "customer_id", ""), transaction_id=customer_update.get("transaction_id", ""))
                if images:
                    response = customers_table.get_item(
                        Key={"customer_id": customer_update.get("customer_id", "")})
                    if "Item" in response:
                        item = response["Item"]

                        customer = {
                            "customer_id": item.get("customer_id", ""),
                            "business_id": item.get("business_id", ""),
                            "given_name": item.get("given_name", ""),
                            "family_name": item.get("family_name", ""),
                            "email": item.get("email", ""),
                            "phone": item.get("phone", ""),
                            "transactions": item.get("transactions", [])
                        }
                        transaction = next((tran for tran in customer.get("transactions", []) if tran.get(
                            "transaction_id", "") == customer_update.get("transaction_id", "")), None)
                        if transaction:
                            transaction["receipt_url"] = images
                            transaction["status"] = "Pendiente de validación"
                            customers_table.update_item(
                                Key={"customer_id": customer.get(
                                    "customer_id", "")},
                                UpdateExpression="SET transactions = :transactions, update_date = :update_date, update_user = :update_user",
                                ExpressionAttributeValues={
                                    ':transactions': customer.get("transactions", []),
                                    ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    ':update_user': customer.get("email", "")
                                },
                                ReturnValues="UPDATED_NEW"
                            )
                            return {
                                'statusCode': 200,
                                'headers': {'Access-Control-Allow-Origin': '*'},
                                'body': json.dumps({'message': 'Recibo subido y transacción actualizada correctamente', "receipt_url": images})
                            }

    except Exception as e:
        print(json.dumps({"event": "upload_receipt",
                          "Error": str(e), "trace": traceback.format_exc()}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def cancel_transaction(event):
    try:
        body = json.loads(event.get('body', '{}'))
        customer_id = body.get('customer_id', '')
        transaction_id = body.get('transaction_id', '')

        response = customers_table.get_item(Key={"customer_id": customer_id})
        if "Item" in response:
            item = response["Item"]

            customer = {
                "customer_id": item.get("customer_id", ""),
                "business_id": item.get("business_id", ""),
                "given_name": item.get("given_name", ""),
                "family_name": item.get("family_name", ""),
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "transactions": item.get("transactions", [])
            }
            transaction = next((tran for tran in customer.get("transactions", []) if tran.get(
                "transaction_id", "") == transaction_id), None)
            if transaction:
                transaction["status"] = "Cancelada"
                customers_table.update_item(
                    Key={"customer_id": customer.get("customer_id", "")},
                    UpdateExpression="SET transactions = :transactions, update_date = :update_date, update_user = :update_user",
                    ExpressionAttributeValues={
                        ':transactions': customer.get("transactions", []),
                        ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ':update_user': customer.get("email", "")
                    },
                    ReturnValues="UPDATED_NEW"
                )
                return {
                    'statusCode': 200,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({'message': 'Transacción cancelada correctamente'})
                }
            else:
                return {
                    'statusCode': 404,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({'message': 'Transacción no encontrada'})
                }
        else:
            return {
                'statusCode': 404,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'message': 'Cliente no encontrado'})
            }
    except Exception as e:
        print(json.dumps({"event": "cancel_transaction", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }

def approve_transaction(event):
    try:
        body = json.loads(event.get('body', '{}'))
        customer_id = body.get('customer_id', '')
        transaction_id = body.get('transaction_id', '')

        response = customers_table.get_item(Key={"customer_id": customer_id})
        if "Item" in response:
            item = response["Item"]

            customer = {
                "customer_id": item.get("customer_id", ""),
                "business_id": item.get("business_id", ""),
                "given_name": item.get("given_name", ""),
                "family_name": item.get("family_name", ""),
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "transactions": item.get("transactions", [])
            }
            transaction = next((tran for tran in customer.get("transactions", []) if tran.get(
                "transaction_id", "") == transaction_id), None)
            if transaction:
                transaction["status"] = "Aprobada"
                customers_table.update_item(
                    Key={"customer_id": customer.get("customer_id", "")},
                    UpdateExpression="SET transactions = :transactions, update_date = :update_date, update_user = :update_user",
                    ExpressionAttributeValues={
                        ':transactions': customer.get("transactions", []),
                        ':update_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ':update_user': customer.get("email", "")
                    },
                    ReturnValues="UPDATED_NEW"
                )
                return {
                    'statusCode': 200,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({'message': 'Transacción aprobada correctamente'})
                }
            else:
                return {
                    'statusCode': 404,
                    'headers': {'Access-Control-Allow-Origin': '*'},
                    'body': json.dumps({'message': 'Transacción no encontrada'})
                }
        else:
            return {
                'statusCode': 404,
                'headers': {'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'message': 'Cliente no encontrado'})
            }
    except Exception as e:
        print(json.dumps({"event": "approve_transaction", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }

def upload_image(file_info, customer_id=None, transaction_id=None):
    try:
        file_name = f"customers/{customer_id}_receipt_{transaction_id}{file_info['extension']}"
        file_url = f"https://{os.getenv('BUCKET_NAME')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{file_name}"
        with io.BytesIO(file_info["content"]) as fileobj:
            s3.upload_fileobj(
                Fileobj=fileobj,
                Bucket=os.getenv('BUCKET_NAME'),
                Key=file_name,
                ExtraArgs={"ContentType": file_info['content_type']}
            )

        del file_info["content"]
        return file_url
    except Exception as e:
        print(json.dumps({
            "event": "upload_image",
            "error": str(e),
            "trace": traceback.format_exc()
        }))
        return None
