import json
import os
from SendMails.mails import contact_team_email

FRONT_END_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')


def contact_team_routes(path, method, event, alias):
    print("Entered contact_team_routes")
    if path == f"/{alias}/team/contact" and method == 'POST':
        return send_contact_team_email(event=event)


def send_contact_team_email(event):
    try:
        body = json.loads(event['body'])
        name = body['name']
        email = body['email']
        message = body['message']
        loginLink = f"{FRONT_END_URL}/login"
        return contact_team_email(
            to_address=email,
            to_name=name,
            login_link=loginLink,
            message=message
        )

    except Exception as e:
        print(json.dumps({"event": "contact_team_email", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }
