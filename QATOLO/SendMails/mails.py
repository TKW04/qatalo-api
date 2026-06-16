from .email_color_helper import build_email_color_vars
import json
import os
# pyrefly: ignore [missing-import]
from mailersend import emails
# pyrefly: ignore [missing-import]
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))
MAIL_API_TOKEN = os.getenv('MAIL_API_TOKEN')

# ── Remitentes ────────────────────────────────────────────────────────────────
QATALO_FROM = {"email": "info@qatalo.online", "name": "Qatalo Support"}

def _biz_from(business: dict) -> dict:
    """Remitente personalizado con el nombre del negocio del cliente."""
    name = business.get('name') or business.get('business_name') or 'Qatalo'
    return {"email": "info@qatalo.online", "name": name}

def _biz_name(business: dict) -> str:
    return business.get('name') or business.get('business_name') or 'Qatalo'

def _logo_url(business: dict) -> str:
    return business.get('logoUrl') or business.get('business_logo_url') or ''


# ── Emails de sistema (remitente = Qatalo) ───────────────────────────────────

def send_forgot_password_email(to_address, to_name, resetLink):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("forgot_password.html")
        mail.send({
            "from": QATALO_FROM,
            "to": [{"email": to_address, "name": to_name}],
            "subject": "Restablecer contraseña",
            "html": template.render(resetLink=resetLink, name=to_name)
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "send_forgot_password_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def past_due_email(to_address, to_name, admin_url):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("past_due.html")
        mail.send({
            "from": QATALO_FROM,
            "to": [{"email": to_address, "name": to_name}],
            "subject": "Tu suscripción venció - Qatalo",
            "html": template.render(admin_url=admin_url, user_name=to_name)
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "past_due_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def welcome_email(to_address, to_name, loginLink):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("welcome.html")
        mail.send({
            "from": QATALO_FROM,
            "to": [{"email": to_address, "name": to_name}],
            "subject": "¡Bienvenido a Qatalo!",
            "html": template.render(login_link=loginLink, name=to_name)
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "welcome_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def contact_team_email(to_address, to_name, login_link, message):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("contact_team.html")
        mail.send({
            "from": QATALO_FROM,
            "to": [{"email": to_address, "name": to_name}],
            "subject": "Gracias por contactarnos - Qatalo",
            "html": template.render(login_link=login_link, name=to_name)
        })
        template = env.get_template("to_team.html")
        mail.send({
            "from": QATALO_FROM,
            "to": [{"email": "isaac.hiraldo@icloud.com", "name": "Qatalo Support"}],
            "subject": f"{to_name} nos envió un mensaje",
            "html": template.render(name=to_name, message=message)
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "contact_team_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def order_access_code_email(to_address, to_name, code, business_name):
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("access_code.html")
        mail.send({
            "from": {"email": "info@qatalo.online", "name": business_name},
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Tu código de acceso - {business_name}",
            "html": template.render(name=to_name, code=code, business_name=business_name),
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_access_code_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}

def order_create_email(to_address, to_name, order_details, business):
    """
    Enviado al CLIENTE cuando crea una nueva orden.
    Template: order_request.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_request.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Confirmación de orden - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                product_name=order_details['product_name'],
                quantity=order_details['quantity'],
                total_amount=order_details['total_amount'],
                currency=order_details['currency'],
                upload_link=order_details['upload_link'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_create_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def new_order_create_email(to_address, to_name, order_details, business):
    """
    Enviado al DUEÑO DEL NEGOCIO cuando llega un nuevo pedido.
    Template: new_order_owner.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("new_order_owner.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Nuevo pedido recibido - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                product_name=order_details['product_name'],
                quantity=order_details['quantity'],
                total_amount=order_details['total_amount'],
                currency=order_details['currency'],
                upload_link=order_details['upload_link'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
                customer_name=order_details['customer_name'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "new_order_create_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def order_cancel_email(to_address, to_name, order_details, business):
    """
    Enviado al CLIENTE cuando su orden es cancelada.
    Template: order_cancel.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_cancel.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Orden cancelada - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                cancellation_date=order_details['cancellation_date'],
                product_name=order_details['product_name'],
                cancellation_reason=order_details['cancellation_reason'],
                business_website_url=order_details['business_website_url'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_cancel_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def order_receipt_email(to_address, to_name, order_details, business):
    """
    Enviado al CLIENTE cuando sube su comprobante de pago.
    Template: order_pending_validation.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_pending_validation.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Comprobante recibido - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                product_name=order_details['product_name'],
                quantity=order_details['quantity'],
                total_amount=order_details['total_amount'],
                currency=order_details['currency'],
                business_website_url=order_details['business_website_url'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_receipt_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def order_verified_email(to_address, to_name, order_details, business):
    """
    Enviado al CLIENTE cuando el dueño valida el pago.
    Template: order_verified.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_verified.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Pago verificado - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                product_name=order_details['product_name'],
                quantity=order_details['quantity'],
                total_amount=order_details['total_amount'],
                currency=order_details['currency'],
                business_website_url=order_details['business_website_url'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_verified_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}


def order_delivered_email(to_address, to_name, order_details, business):
    """
    Enviado al CLIENTE cuando el dueño marca la orden como entregada.
    Template: order_delivered.html
    """
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("order_delivered.html")
        color_vars = build_email_color_vars(business)
        name = _biz_name(business)
        mail.send({
            "from": _biz_from(business),
            "to": [{"email": to_address, "name": to_name}],
            "subject": f"Orden entregada - {name}",
            "html": template.render(
                **color_vars,
                business_name=name,
                business_logo_url=_logo_url(business),
                transaction_id=order_details['transaction_id'],
                order_date=order_details['order_date'],
                product_name=order_details['product_name'],
                quantity=order_details['quantity'],
                total_amount=order_details['total_amount'],
                currency=order_details['currency'],
                business_website_url=order_details['business_website_url'],
                business_email=order_details['business_email'],
                business_phone=order_details['business_phone'],
            )
        })
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}}
    except Exception as e:
        print(json.dumps({"event": "order_delivered_email", "Error": str(e)}))
        return {"statusCode": 500, "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"message": str(e)})}

def low_stock_alert_email(to_address, product_name, business_name, current_stock, threshold):
    """Alerta de stock bajo — enviada al dueño del negocio (cliente de Qatalo)."""
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("low_stock_alert.html")
        mail.send({
            "from": QATALO_FROM,
            "to":   [{"email": to_address, "name": business_name}],
            "subject": f"⚠️ Stock bajo — {product_name}",
            "html": template.render(
                is_out_of_stock=False,
                product_name=product_name,
                business_name=business_name,
                current_stock=current_stock,
                threshold=threshold,
                admin_url="https://qatalo.online/admin",
            )
        })
    except Exception as e:
        print(json.dumps({"event": "low_stock_alert_email", "Error": str(e)}))


def out_of_stock_alert_email(to_address, product_name, business_name):
    """Alerta de producto agotado — enviada al dueño del negocio (cliente de Qatalo)."""
    try:
        mail = emails.NewEmail(MAIL_API_TOKEN)
        template = env.get_template("low_stock_alert.html")
        mail.send({
            "from": QATALO_FROM,
            "to":   [{"email": to_address, "name": business_name}],
            "subject": f"🚨 Producto agotado — {product_name}",
            "html": template.render(
                is_out_of_stock=True,
                product_name=product_name,
                business_name=business_name,
                current_stock=0,
                threshold=0,
                admin_url="https://qatalo.online/admin",
            )
        })
    except Exception as e:
        print(json.dumps({"event": "out_of_stock_alert_email", "Error": str(e)}))

