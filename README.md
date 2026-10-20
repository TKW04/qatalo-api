# batallo-api
api del catalogo de QR

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
