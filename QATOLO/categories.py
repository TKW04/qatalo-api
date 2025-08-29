import re
import boto3
import json
import os
import uuid
from boto3.dynamodb.conditions import Attr
from datetime import datetime

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION'))
categories_table = dynamodb.Table("qatalo.categories")


def categories_routes(path, method, event, user_name, user_id):

    if path == "/categories" and method == 'GET':
        return get_categories_by_user_id(user_id=user_id)
    if path == "/categories" and method == 'POST':
        return create_category(event=event, user_name=user_name, user_id=user_id)

    match = re.fullmatch(r'/categories/([^/]+)', path)
    if match:
        category_id = match.group(1)
        if method == 'PUT':
            return update_category(event=event, user_name=user_name, category_id=category_id, user_id=user_id)
        if method == 'DELETE':
            return delete_category(category_id)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_categories_by_user_id(user_id: str):
    """
    Retrieve all categories from the database.
    """
    try:
        response = categories_table.scan(
            FilterExpression=Attr('user_id').eq(user_id)
        )
        categories =[]
        for item in response.get("Items", []):
            categories.append({
                "category_id": item.get("category_id", ""),
                "business_id": item.get("business_id", ""),
                "slug": item.get("category_slug", ""),
                "name": item.get("category_name", ""),
                "user_id": item.get("user_id", "")
            })
        return {
            'statusCode': 200,  # No uses 204
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': '*'
            },
            'body': json.dumps(categories, default=str)
        }
    except Exception as e:
        print(json.dumps({"event": "get_categories", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_category(event, user_name, user_id):
    """
    Create a new category.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        categories_table.put_item(
            Item={
                "category_id": str(uuid.uuid4()),
                "business_id": body.get('business_id', ''),
                "category_slug": body.get('slug', ''),
                "category_name": body.get('name', ''),
                "user_id": user_id,
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
        print(json.dumps({"event": "create_category", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def update_category(event, user_name, category_id, user_id):
    """
    Update an existing category.
    """
    try:
        body = json.loads(event.get('body', '{}'))
        categories_table.update_item(
            Key={"category_id": category_id},
            UpdateExpression="SET category_slug = :category_slug, category_name = :category_name, user_id = :user_id, update_date = :update_date, update_user = :update_user",
            ExpressionAttributeValues={
                ':category_slug': body.get('slug', ''),
                ':category_name': body.get('name', ''),
                ':user_id': user_id,
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


def delete_category(category_id: str):
    """
    Delete a category by its ID.
    """
    try:
        categories_table.delete_item(Key={"category_id": category_id})
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Categoría eliminada correctamente'})
        }
    except Exception as e:
        print(json.dumps({"event": "delete_category", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
