import json
import os
# pyrefly: ignore [missing-import]
from mailersend import emails
# pyrefly: ignore [missing-import]
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

def past_due_email(to_address, to_name, admin_url):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("past_due.html")
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
                "html": template.render(admin_url=admin_url, user_name=to_name)
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

def order_create_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_request.html")
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
                                        business_logo_url=order_details.get(
                                            'business_logo_url', ''),
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

def new_order_create_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("new_order_owner.html")
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
                "subject": "Pedido recibido - Qatalo",
                "html": template.render(business_name=order_details.get('business_name', 'Qatalo'),
                                        business_logo_url=order_details.get(
                                            'business_logo_url', ''),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        product_name=order_details['product_name'],
                                        quantity=order_details['quantity'],
                                        total_amount=order_details['total_amount'],
                                        currency=order_details['currency'],
                                        upload_link=order_details['upload_link'],
                                        business_email=order_details['business_email'],
                                        business_phone=order_details['business_phone'],
                                        customer_name=order_details['customer_name'],)
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

def order_cancel_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_cancel.html")
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
                "html": template.render(business_logo_url=order_details.get('business_logo_url', ''),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        cancellation_date=order_details['cancellation_date'],
                                        product_name=order_details['product_name'],
                                        cancellation_reason=order_details['cancellation_reason'],
                                        business_website_url=order_details['business_website_url'],
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

def order_receipt_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_pending_validation.html")
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
                                        business_logo_url=order_details.get(
                                            'business_logo_url', ''),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        product_name=order_details['product_name'],
                                        quantity=order_details['quantity'],
                                        total_amount=order_details['total_amount'],
                                        currency=order_details['currency'],
                                        business_website_url=order_details['business_website_url'],
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

def order_verified_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_verified.html")
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
                                        business_logo_url=order_details.get(
                                            'business_logo_url', ''),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        product_name=order_details['product_name'],
                                        quantity=order_details['quantity'],
                                        total_amount=order_details['total_amount'],
                                        currency=order_details['currency'],
                                        business_website_url=order_details['business_website_url'],
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

def order_delivered_email(to_address, to_name, order_details):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_delivered.html")
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
                "subject": "Entrega de Orden - Qatalo",
                "html": template.render(business_name=order_details.get('business_name', 'Qatalo'),
                                        business_logo_url=order_details.get(
                                            'business_logo_url', ''),
                                        transaction_id=order_details['transaction_id'],
                                        order_date=order_details['order_date'],
                                        product_name=order_details['product_name'],
                                        quantity=order_details['quantity'],
                                        total_amount=order_details['total_amount'],
                                        currency=order_details['currency'],
                                        business_website_url=order_details['business_website_url'],
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

def contact_team_email(to_address, to_name, login_link, message):

    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("contact_team.html")
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
                "subject": "Gracias por contactarnos - Qatalo",
                "html": template.render(login_link=login_link, name=to_name)
            })
        template = env.get_template("to_team.html")
        response = mail.send(
            {
                "from": {
                    "email": "info@qatalo.online",
                    "name": "Qatalo Support"
                },
                "to": [
                    {
                        "email": "info@qatalo.online",
                        "name": "Qatalo Support"
                    }
                ],
                "subject": f"{to_name}, nos envió un mensaje",
                "html": template.render(name=to_name, message=message)
            })
        
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }

    except Exception as e:
        print(json.dumps({"event": "contact_team_email", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }

def order_access_code_email(to_address, to_name, code, business_name):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("access_code.html")
        response = mail.send(
            {
                "from": {"email": "info@qatalo.online", "name": "Qatalo Support"},
                "to": [{"email": to_address, "name": to_name}],
                "subject": f"Tu código de acceso - {business_name}",
                "html": template.render(name=to_name, code=code, business_name=business_name),
            }
        )
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_access_code_email", "Error": str(e)}))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": str(e)}),
        }

                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  