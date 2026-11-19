# batallo-api
api del catalogo de QR

## Recuperación de contraseña (`QATOLO/users.py`)

Rutas públicas (sin autorizador Cognito):

- `POST /{alias}/users/forgot-password/{email}` — responde **siempre** 200 con un mensaje genérico (no revela si
  el correo existe). Si el usuario existe y está habilitado, envía un correo con
  `${FRONTEND_URL}/reset-password/{token}`. 503 solo si falta `PASSWORD_RESET_SECRET`.
- `POST /{alias}/users/change-password` — **cambio de contrato**: body `{"token": "...", "password": "..."}`.
  El usuario se obtiene exclusivamente del token; un campo `username` se ignora (el contrato anterior
  `{"username", "password"}` permitía cambiar la contraseña de cualquier cuenta y ya no funciona).
  Respuestas: 200 ok; 400 `{"code": "invalid_token"}` (firma inválida, caducado, usado, usuario inexistente o
  deshabilitado — mismo mensaje en todos los casos); 400 `{"code": "invalid_password"}` (menos de 8 / más de 256
  caracteres o rechazada por la política del User Pool); 503 sin secreto; 500 error de Cognito. Tras el cambio se
  ejecuta `admin_user_global_sign_out` (cierra las sesiones abiertas).

Token: `b64url(payload).b64url(HMAC-SHA256)` con payload `{sub, exp, v}`; caduca a los 60 min y `v` es una huella de
`UserLastModifiedDate`, que Cognito cambia al fijar la contraseña → el token es de un solo uso y un reset invalida
los demás enlaces pendientes. Los enlaces antiguos `/reset-password/{sub}` dejan de funcionar. Los logs no incluyen
correos, subs, tokens ni contraseñas.

| Variable | Uso |
|---|---|
| `PASSWORD_RESET_SECRET` | Secreto HMAC de los enlaces de recuperación (≥ 32 caracteres aleatorios, p. ej. `openssl rand -base64 48`). Admite override por alias (`PASSWORD_RESET_SECRET_PROD`). **Obligatoria**: sin ella forgot/change-password responden 503 (falla cerrado). Rotarla invalida los enlaces pendientes. |

## Bitácora de producción (`QATOLO/production.py`)

Rutas: `POST/GET /production`, `DELETE /production/{log_id}`, `GET /production/report`.
Requiere la tabla `qatalo.production_logs` (PK `business_id`, SK `log_id`), que NO está en IaC.
Crearla a mano (y dar a la Lambda permisos PutItem/GetItem/DeleteItem/Query sobre ella):

```
aws dynamodb create-table \
  --table-name qatalo.production_logs \
  --attribute-definitions AttributeName=business_id,AttributeType=S AttributeName=log_id,AttributeType=S \
  --key-schema AttributeName=business_id,KeyType=HASH AttributeName=log_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --profile BMCargo --region us-east-1
```

## Recetas (`QATOLO/recipes.py`)

Solo control de costos: NO afecta inventario/stock.
Rutas: `GET/POST /recipes`, `PUT/DELETE /recipes/{recipe_id}` (escrituras requieren suscripción activa).
Requiere la tabla `qatalo.recipes` (PK `business_id`, SK `recipe_id`), que NO está en IaC.
Crearla a mano (y dar a la Lambda permisos PutItem/GetItem/DeleteItem/Query/UpdateItem sobre ella):

```
aws dynamodb create-table \
  --table-name qatalo.recipes \
  --attribute-definitions AttributeName=business_id,AttributeType=S AttributeName=recipe_id,AttributeType=S \
  --key-schema AttributeName=business_id,KeyType=HASH AttributeName=recipe_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --profile BMCargo --region us-east-1
```

## Suscripciones de Apple vía RevenueCat (`QATOLO/revenuecat.py`)

Una compra In-App en iOS (App Store 3.1.1) activa el acceso igual que Paddle: escribe en Cognito
`custom:transaction_status` (`active` mientras el entitlement no venza — incluye cancelación de la renovación y
problema de cobro en gracia — y `expired` al vencer), `custom:transaction_id` = `apple:<original_transaction_id>`,
`custom:customer_id` = `rc:<sub>` y `custom:due_date` (RFC 3339 UTC, `""` si es vitalicio).
En RevenueCat el `app_user_id` es el `sub` de Cognito (la app hace `Purchases.logIn(sub)`); los ids
`$RCAnonymousID:` se ignoran.

Convivencia con Paddle: un evento de Apple que desactiva NO pisa una suscripción Paddle activa
(`transaction_id` sin prefijo `apple:`); una activación de Apple sí escribe. Simétricamente, un evento Paddle que
desactiva (canceled/past_due/paused) se ignora si el usuario tiene `apple:` activo y `due_date` futura.

Rutas (ambas exentas del guard 402):

- `POST /{alias}/revenuecat/webhook` — **pública** en API Gateway (sin autorizador Cognito); se autentica con el
  header `Authorization`, que debe coincidir exactamente con `REVENUECAT_WEBHOOK_AUTH` (401 si no). Body: el JSON
  estándar de RevenueCat (`{"event": {...}}`). 200 `{ok, results|ignored}`; 500 en errores transitorios
  (RevenueCat/Cognito) para que RevenueCat reintente.
- `POST /{alias}/subscription/sync` — autenticada (mismo autorizador que `GET /subscription/status`), sin body.
  Consulta `GET https://api.revenuecat.com/v1/subscribers/{sub}`, actualiza Cognito y responde lo mismo que
  `/subscription/status`: `{"transaction_status": "...", "active": bool}`. 401 sin usuario, 429 si se repite en
  < 10 s, 503 si falta `REVENUECAT_SECRET_API_KEY`, 502 si RevenueCat/Cognito fallan.

Variables de entorno de la Lambda (cada una admite override por alias, p. ej. `REVENUECAT_WEBHOOK_AUTH_PROD`):

| Variable | Uso |
|---|---|
| `REVENUECAT_WEBHOOK_AUTH` | Valor exacto del header Authorization configurado en el webhook de RevenueCat (p. ej. `Bearer <secreto largo aleatorio>`). Obligatoria: sin ella el webhook responde 401. |
| `REVENUECAT_SECRET_API_KEY` | Clave secreta `sk_...` (REST API v1). Recomendada: el webhook usa el estado real del suscriptor en vez de deducirlo del evento. Obligatoria para `/subscription/sync`. |
| `REVENUECAT_ENTITLEMENT` | Id del entitlement que da acceso. Default `qatalo_pro` (el id real en RevenueCat); configúrala explícitamente como `REVENUECAT_ENTITLEMENT=qatalo_pro`. |

Requisitos de despliegue (obligatorios):

- `POST /revenuecat/webhook` **sin** authorizer de API Gateway: RevenueCat no envía un JWT de Cognito; la
  autenticación es el header `Authorization` comparado con `REVENUECAT_WEBHOOK_AUTH` dentro de la Lambda.
- `POST /subscription/sync` **con el mismo authorizer** que `GET /subscription/status` (JWT de Cognito; el `sub`
  del token es el app_user_id de RevenueCat).
- Variables: `REVENUECAT_WEBHOOK_AUTH`, `REVENUECAT_SECRET_API_KEY` (clave secreta **v1** `sk_...`, no una v2) y
  `REVENUECAT_ENTITLEMENT=qatalo_pro`.
- Sin `REVENUECAT_SECRET_API_KEY` el webhook deduce el estado del evento y descarta los eventos atrasados
  (`expiration_at_ms` anterior a la `due_date` Apple ya guardada). Con la clave, la REST API es la fuente de
  verdad y el orden de llegada no importa.
- Si el usuario tiene una suscripción Paddle activa, los eventos Apple no modifican Cognito (se conservan
  `sub_...`/`ctm_...` para que el borrado de cuenta cancele Paddle).

Configuración en RevenueCat (Project → Integrations → Webhooks): URL
`https://enoo1ip7ga.execute-api.us-east-1.amazonaws.com/Prod/revenuecat/webhook`, header de autorización = el
valor de `REVENUECAT_WEBHOOK_AUTH`, entorno "Production and Sandbox" (App Review compra en sandbox). Envía un
evento TEST desde el panel: debe dar 200 sin cambios.

Deploy (perfil `--profile BMCargo --region us-east-1`; la API invoca el alias `Prod`, subir el zip solo cambia
`$LATEST`):

1. API Gateway `enoo1ip7ga`: si las rutas son explícitas, crear `POST /revenuecat/webhook` (sin autorizador) y
   `POST /subscription/sync` (mismo autorizador que `GET /subscription/status`) con la integración Lambda `:Prod`
   (ver `../deploy-v2.md` §3b para los comandos `get-routes`/`create-route`).
2. Añadir las variables de entorno anteriores a la función (`aws lambda update-function-configuration ...
   --environment`, conservando las existentes; las variables se congelan en cada versión publicada).
3. Subir el código, publicar versión y mover el alias:
   `aws lambda update-function-code --function-name <NOMBRE> --zip-file fileb://...` →
   `aws lambda wait function-updated ...` → `NEW=$(aws lambda publish-version ... --query Version --output text)` →
   `aws lambda update-alias --function-name <NOMBRE> --name Prod --function-version "$NEW"` (todos con
   `--profile BMCargo --region us-east-1`).
4. Enviar el evento TEST desde RevenueCat y revisar CloudWatch (`revenuecat.webhook`).
