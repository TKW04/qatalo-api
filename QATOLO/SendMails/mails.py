import json
import os
from mailersend import emails
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))
MAIL_API_TOKEN = os.getenv('MAIL_API_TOKEN')


def send_forgot_password_email(to_address, to_name, resetLink):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("forgot_password.html")
        response = mail.send(
            {
                "from": {
                    "email": "info@qatalo.online",
                    "name": "Qatalo Support"
                },
                "to": [
                    {
                        "email": to_address,
                        "name": to_name
                    }
                ],
                "subject": "Restablecer contraseña",
                "html": template.render(resetLink=resetLink, name=to_name)
            }
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }

    except Exception as e:
        print(json.dumps(
            {"event": "send_forgot_password_email", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def welcome_email(to_address, to_name, loginLink):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("welcome.html")
        response = mail.send(
            {
                "from": {
                    "email": "info@qatalo.online",
                    "name": "Qatalo Support"
                },
                "to": [
                    {
                        "email": to_address,
                        "name": to_name
                    }
                ],
                "subject": "¡Bienvenido a Qatalo!",
                "html": template.render(login_link=loginLink, name=to_name)
            }
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }

    except Exception as e:
        print(json.dumps({"event": "welcome_email", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def create_order_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("orden_request.html")
        response = mail.send(
            {
                "from": {
                    "email": "info@qatalo.online",
                    "name": "Qatalo Support"
                },
                "to": [
                    {
                        "email": to_address,
                        "name": to_name
                    }
                ],
                "subject": "Confirmación de Orden - Qatalo",
                "html": template.render(business_name=order_details.get('business_name', 'Qatalo'),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        product_name=order_details['product_name'],
                                        quantity=order_details['quantity'],
                                        total_amount=order_details['total_amount'],
                                        currency=order_details['currency'],
                                        upload_link=order_details['upload_link'],
                                        business_email=order_details['business_email'],
                                        business_phone=order_details['business_phone'])
            }
        )
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }

    except Exception as e:
        print(json.dumps({"event": "create_orden_email", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
