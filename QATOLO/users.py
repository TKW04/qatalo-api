import base64
import hashlib
import hmac
import json
import re
import time
import boto3
import os
from urllib.parse import unquote

from zoneinfo import ZoneInfo
from datetime import datetime
from SendMails.mails import send_forgot_password_email, welcome_email

cognito = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')
FRONT_END_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')


def users_routes(path, method, event, alias):

    if path == f"/{alias}/users" and method == 'POST':
        return register_user(event=event)
    if path == f"/{alias}/users/change-password" and method == 'POST':
        return reset_password(event=event, alias=alias)

    match_forgot = re.fullmatch(rf'/{alias}/users/forgot-password/([^/]+)', path)
    if match_forgot:
        user_name = unquote(match_forgot.group(1))
        if method == 'POST':
            return forgot_password(user_name=user_name, alias=alias)

    match = re.fullmatch(r'/users/([^/]+)', path)
    if match:
        username = match.group(1)
        if method == 'GET':
            return get_user(username)
        elif method == 'PUT':
            return update_user(username, event)

    return {
        'statusCode': 404,
        'body': 'Ruta no encontrada'
    }


def get_user(username):
    try:
        user = cognito.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=username
        )

        # Formatear los atributos
        attrs = {attr['Name']: attr['Value']
                 for attr in user['UserAttributes']}

        user_response = {"id": attrs.get('sub'),
                         "email": attrs.get('email'),
                         "given_name": attrs.get('given_name'),
                         "family_name": attrs.get('family_name'),
                         "price_id": attrs.get('custom:price_id'),
                         "transaction_id": attrs.get('custom:transaction_id'),
                         "transaction_status": attrs.get('custom:transaction_status'),
                         "customer_id": attrs.get('custom:customer_id'),
                         "due_date": attrs.get('custom:due_date'),
                         "status": user['Enabled']}

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps(user_response, default=str)
        }

    except cognito.exceptions.UserNotFoundException as ex:
        print(json.dumps({"event": "get_user", "Error": str(ex)}))
        return {
            'statusCode': 404,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario no encontrado'})
        }
    except Exception as e:
        print(json.dumps({"event": "get_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'}
        }


def update_user(username, event):
    try:
        # Parsear el body recibido (debe ser JSON)
        body = json.loads(event.get('body', '{}'))
        # Ejecutar la actualización en Cognito
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[
                {"Name": "given_name",
                 "Value": body['given_name']},
                {"Name": "family_name",
                 "Value": body.get('family_name')},
                {"Name": "phone_number", "Value": body.get('phone_number')}
            ]
        )
        if 'password' in body and body['password'] != '':
            nueva_clave = body['password']
            cognito.admin_set_user_password(
                UserPoolId=USER_POOL_ID,
                Username=username,
                Password=nueva_clave,
                Permanent=True
            )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario actualizado correctamente'})
        }

    except Exception as e:
        print(json.dumps({"event": "update_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def register_user(event):
    try:

        body = json.loads(event.get('body', '{}'))
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email'),
            UserAttributes=[
                {"Name": "email", "Value": body.get('email')},
                {"Name": "email_verified", "Value": 'true'},
                {"Name": "given_name", "Value": body.get('given_name')},
                {"Name": "family_name", "Value": body.get('family_name')},
                {"Name": "custom:customer_id", "Value": ""},
                {"Name": "custom:price_id", "Value": "0"},
                {"Name": "custom:transaction_id", "Value": "0"},
                {"Name": "custom:transaction_status", "Value": "pending"},
                {"Name": "custom:due_date", "Value": datetime.now(
                    ZoneInfo("America/Santo_Domingo")).strftime("%Y-%m-%d")}

            ],
            MessageAction="SUPPRESS"  # No enviar email

        )
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email'),
            Password=body.get('password'),
            Permanent=True
        )
        cognito.admin_enable_user(
            UserPoolId=USER_POOL_ID,
            Username=body.get('email')
        )

        sent = send_welcome_email(body.get('email'), body.get('given_name'), body.get('family_name'))
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': 'Usuario creado', 'welcome_email_sent': sent})
        }

    except Exception as e:
        print(json.dumps({"event": "create_user", "Error": str(e)}))
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'message': str(e)})
        }


def _welcome_enabled():
    return os.environ.get('WELCOME_EMAIL_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def plans_url():
    """Página de planes de qatalo-web (/payment: tras iniciar sesión muestra los planes de Paddle)."""
    return os.environ.get('PLANS_URL') or f"{FRONT_END_URL.rstrip('/')}/payment"


def send_welcome_email(email, given_name, family_name):
    """Correo de bienvenida (MailerSend). Nunca lanza: un fallo no debe romper el registro."""
    if not _welcome_enabled() or not email:
        return False
    try:
        name = " ".join(x for x in (given_name, family_name) if x) or email
        res = welcome_email(email, to_name=name,
                            loginLink=f"{FRONT_END_URL.rstrip('/')}/login",
                            plans_link=plans_url())
        ok = isinstance(res, dict) and res.get('statusCode') == 200
        if not ok:
            print(json.dumps({"event": "welcome_email.failed"}))
        return ok
    except Exception as e:
        print(json.dumps({"event": "welcome_email.failed", "Error": type(e).__name__}))
        return False


# ───────── Recuperación de contraseña ─────────
# El enlace del correo lleva un token firmado (HMAC-SHA256 con PASSWORD_RESET_SECRET), no el sub:
#   token = b64url(payload_json) + "." + b64url(hmac(secret, "qatalo-pwreset-v1." + b64url(payload_json)))
#   payload = {"sub": <sub de Cognito>, "exp": <epoch s>, "v": <huella del estado del usuario>}
# "v" se deriva de UserLastModifiedDate: admin_set_user_password la cambia, así que el token deja de
# valer tras usarse (un solo uso) y también invalida los enlaces anteriores pendientes.

RESET_TOKEN_TTL_SECONDS = 60 * 60
_RESET_TOKEN_MAX_LEN = 1024
_RESET_SIG_PREFIX = b"qatalo-pwreset-v1."
_MIN_SECRET_LEN = 32
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 256

_CORS = {'Access-Control-Allow-Origin': '*'}
_FORGOT_GENERIC = ('Si el correo está registrado, recibirás un enlace para restablecer tu contraseña. '
                   'El enlace caduca en 60 minutos.')
_INVALID_LINK = 'El enlace es inválido o ha caducado. Solicita uno nuevo.'
_PASSWORD_POLICY_MSG = ('La contraseña no cumple los requisitos: mínimo 8 caracteres, con mayúsculas, '
                        'minúsculas, números y un símbolo.')


class _MisconfiguredError(Exception):
    pass


def _json_resp(status, body):
    return {'statusCode': status, 'headers': dict(_CORS), 'body': json.dumps(body)}


def _error_code(e):
    try:
        return e.response['Error']['Code']
    except Exception:
        return type(e).__name__


def _reset_secret(alias=None):
    """PASSWORD_RESET_SECRET (admite override por alias, p. ej. PASSWORD_RESET_SECRET_PROD).
    Falla cerrado: sin secreto (o demasiado corto) no se emiten ni aceptan tokens."""
    v = ''
    if alias:
        v = os.environ.get(f"PASSWORD_RESET_SECRET_{alias.upper()}", '')
    v = v or os.environ.get('PASSWORD_RESET_SECRET', '')
    if len(v) < _MIN_SECRET_LEN:
        raise _MisconfiguredError('PASSWORD_RESET_SECRET ausente o corto')
    return v.encode('utf-8')


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _user_fingerprint(username, last_modified):
    """Huella del estado actual del usuario; cambia cuando Cognito modifica al usuario (p. ej. su contraseña)."""
    lm = last_modified.timestamp() if hasattr(last_modified, 'timestamp') else str(last_modified)
    return hashlib.sha256(f"{username}|{lm}".encode('utf-8')).hexdigest()[:32]


def _sign(payload_b64, secret):
    return hmac.new(secret, _RESET_SIG_PREFIX + payload_b64.encode('ascii'), hashlib.sha256).digest()


def make_reset_token(sub, fingerprint, secret, now=None):
    now = int(now if now is not None else time.time())
    payload = json.dumps({'sub': sub, 'exp': now + RESET_TOKEN_TTL_SECONDS, 'v': fingerprint},
                         separators=(',', ':'), sort_keys=True).encode('utf-8')
    p64 = _b64e(payload)
    return f"{p64}.{_b64e(_sign(p64, secret))}"


def verify_reset_token(token, secret, now=None):
    """Devuelve el payload si la firma y la caducidad son válidas; None en cualquier otro caso."""
    if not isinstance(token, str) or not token or len(token) > _RESET_TOKEN_MAX_LEN:
        return None
    parts = token.split('.')
    if len(parts) != 2:
        return None
    try:
        sig = _b64d(parts[1])
        if not hmac.compare_digest(sig, _sign(parts[0], secret)):
            return None
        payload = json.loads(_b64d(parts[0]))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    sub, exp, v = payload.get('sub'), payload.get('exp'), payload.get('v')
    if not isinstance(sub, str) or not isinstance(v, str) or not isinstance(exp, int):
        return None
    now = int(now if now is not None else time.time())
    if exp < now or exp > now + RESET_TOKEN_TTL_SECONDS + 60:
        return None
    return payload


def _find_user_by_sub(sub):
    """Busca el usuario por sub (atributo inmutable). Devuelve el dict de list_users o None."""
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', sub or ''):
        return None
    res = cognito.list_users(UserPoolId=USER_POOL_ID, Filter=f'sub = "{sub}"', Limit=1)
    users = res.get('Users') or []
    return users[0] if users else None


def _password_problem(password):
    if not isinstance(password, str) or not (PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN):
        return _PASSWORD_POLICY_MSG
    if password != password.strip():
        return 'La contraseña no puede empezar ni terminar con espacios.'
    return None


def forgot_password(user_name, alias=None):
    """Siempre responde 200 genérico: no revela si el correo existe."""
    try:
        secret = _reset_secret(alias)
    except _MisconfiguredError:
        print(json.dumps({"event": "forgot_password.misconfigured"}))
        return _json_resp(503, {'message': 'Servicio no disponible temporalmente. Inténtalo más tarde.'})
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_name)
        attrs = {a['Name']: a['Value'] for a in user.get('UserAttributes', [])}
        sub, email = attrs.get('sub'), attrs.get('email')
        if not user.get('Enabled', True) or not sub or not email:
            print(json.dumps({"event": "forgot_password.skipped"}))
            return _json_resp(200, {'message': _FORGOT_GENERIC})
        token = make_reset_token(sub, _user_fingerprint(user['Username'], user['UserLastModifiedDate']), secret)
        reset_link = f"{FRONT_END_URL.rstrip('/')}/reset-password/{token}"
        name = " ".join(x for x in (attrs.get('given_name'), attrs.get('family_name')) if x) or email
        res = send_forgot_password_email(to_address=email, to_name=name, resetLink=reset_link)
        ok = isinstance(res, dict) and res.get('statusCode') == 200
        print(json.dumps({"event": "forgot_password.sent" if ok else "forgot_password.mail_failed"}))
    except Exception as e:
        # UserNotFoundException incluido: misma respuesta que en el caso feliz.
        print(json.dumps({"event": "forgot_password.error", "Error": _error_code(e)}))
    return _json_resp(200, {'message': _FORGOT_GENERIC})


def reset_password(event, alias=None):
    """POST /users/change-password  body {"token", "password"}. Cualquier "username" se ignora:
    el usuario sale exclusivamente del token firmado."""
    try:
        secret = _reset_secret(alias)
    except _MisconfiguredError:
        print(json.dumps({"event": "reset_password.misconfigured"}))
        return _json_resp(503, {'message': 'Servicio no disponible temporalmente. Inténtalo más tarde.'})
    try:
        body = json.loads(event.get('body') or '{}')
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return _json_resp(400, {'message': 'Solicitud inválida.', 'code': 'bad_request'})

    payload = verify_reset_token(body.get('token'), secret)
    if payload is None:
        print(json.dumps({"event": "reset_password.invalid_token"}))
        return _json_resp(400, {'message': _INVALID_LINK, 'code': 'invalid_token'})

    problem = _password_problem(body.get('password'))
    if problem:
        return _json_resp(400, {'message': problem, 'code': 'invalid_password'})

    try:
        user = _find_user_by_sub(payload['sub'])
        if (user is None or not user.get('Enabled', True)
                or not hmac.compare_digest(
                    _user_fingerprint(user['Username'], user['UserLastModifiedDate']), payload['v'])):
            print(json.dumps({"event": "reset_password.stale_or_unknown"}))
            return _json_resp(400, {'message': _INVALID_LINK, 'code': 'invalid_token'})

        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=user['Username'],
            Password=body['password'],
            Permanent=True
        )
    except Exception as e:
        code = _error_code(e)
        if code == 'InvalidPasswordException':
            return _json_resp(400, {'message': _PASSWORD_POLICY_MSG, 'code': 'invalid_password'})
        print(json.dumps({"event": "reset_password.error", "Error": code}))
        return _json_resp(500, {'message': 'No se pudo restablecer la contraseña. Inténtalo de nuevo.'})

    try:
        # Cierra las sesiones abiertas (revoca refresh tokens) tras el cambio de contraseña.
        cognito.admin_user_global_sign_out(UserPoolId=USER_POOL_ID, Username=user['Username'])
    except Exception as e:
        print(json.dumps({"event": "reset_password.signout_failed", "Error": _error_code(e)}))
    print(json.dumps({"event": "reset_password.ok"}))
    return _json_resp(200, {'message': 'Contraseña restablecida correctamente'})
