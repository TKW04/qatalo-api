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
