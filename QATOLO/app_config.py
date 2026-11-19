"""Configuración pública de la app móvil: actualización obligatoria (GET /{alias}/app/config).

Público (sin JWT, sin guard de suscripción). La app compara su build nativo (iOS CFBundleVersion)
con `minBuild`: si es menor, muestra una pantalla bloqueante "Actualización requerida".

Valores en SSM Parameter Store (caché en memoria de 5 min):
  /qatalo/app/ios_min_build      -> minBuild (int)
  /qatalo/app/ios_latest_build   -> latestBuild (int, opcional)
Fail-open: si SSM falla o el parámetro no existe, minBuild = env APP_IOS_MIN_BUILD o 0, y
latestBuild = env APP_IOS_LATEST_BUILD o null. Un error del servidor nunca bloquea a nadie.
"""
import json
import os
import time

import boto3

IOS_STORE_URL = "https://apps.apple.com/app/id6820258202"
PARAM_IOS_MIN_BUILD = "/qatalo/app/ios_min_build"
PARAM_IOS_LATEST_BUILD = "/qatalo/app/ios_latest_build"
CACHE_TTL_SECONDS = 300
HTTP_MAX_AGE = 300

_ssm = None
_cache = {"at": 0.0, "values": None}


def _ssm_client():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ssm


def _to_int(value):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _read_ssm():
    """Devuelve {nombre: valor_str} de los parámetros que existan. Lanza si SSM falla."""
    resp = _ssm_client().get_parameters(Names=[PARAM_IOS_MIN_BUILD, PARAM_IOS_LATEST_BUILD])
    return {p["Name"]: p.get("Value") for p in resp.get("Parameters", [])}


def _load_values():
    now = time.time()
    if _cache["values"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["values"]
    try:
        params = _read_ssm()
    except Exception as e:
        print(json.dumps({"event": "app_config.ssm_failed", "Error": type(e).__name__}))
        params = {}
    min_build = _to_int(params.get(PARAM_IOS_MIN_BUILD))
    if min_build is None:
        min_build = _to_int(os.environ.get("APP_IOS_MIN_BUILD")) or 0
    latest_build = _to_int(params.get(PARAM_IOS_LATEST_BUILD))
    if latest_build is None:
        latest_build = _to_int(os.environ.get("APP_IOS_LATEST_BUILD"))
    values = {"ios_min_build": min_build, "ios_latest_build": latest_build}
    _cache["values"] = values
    _cache["at"] = now
    return values


def get_app_config():
    v = _load_values()
    return {
        "ios": {"minBuild": v["ios_min_build"], "latestBuild": v["ios_latest_build"], "storeUrl": IOS_STORE_URL},
        "android": {"minBuild": None, "latestBuild": None, "storeUrl": None},
    }


def app_config_routes(method, cors):
    headers = dict(cors)
    if method != "GET":
        return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "method_not_allowed"})}
    headers["Cache-Control"] = f"public, max-age={HTTP_MAX_AGE}"
    headers["Content-Type"] = "application/json"
    return {"statusCode": 200, "headers": headers, "body": json.dumps(get_app_config())}
