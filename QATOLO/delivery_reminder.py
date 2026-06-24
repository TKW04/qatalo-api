import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from customers import customers_table, _get_business, _owner_email
from SendMails.mails import delivery_reminder_email


def _scan_all(table, **kwargs):
    items, resp = [], table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    return items


def run_delivery_reminders():
    now_rd   = datetime.now(timezone.utc) - timedelta(hours=4)
    tomorrow = (now_rd + timedelta(days=1)).strftime("%Y-%m-%d")
    print(json.dumps({"event": "delivery_reminder.start", "tomorrow": tomorrow}))

    customers = _scan_all(customers_table)

    # business_id → order_group → { meta + items[] }
    by_business = defaultdict(lambda: defaultdict(lambda: {"meta": {}, "items": []}))

    for customer in customers:
        business_id = customer.get("business_id", "")
        if not business_id:
            continue

        cust_name = (
            customer.get("full_name")
            or f"{customer.get('given_name','')} {customer.get('family_name','')}".strip()
            or "Cliente"
        )

        for txn in customer.get("transactions", []):
            # Normaliza la fecha de entrega a YYYY-MM-DD
            d_day = str(txn.get("delivery_day", ""))[:10]
            if d_day != tomorrow:
                continue
            if txn.get("status") != "Aprobada":
                continue

            # Agrupa por order_group si existe; si no, por transaction_id
            og = txn.get("order_group") or txn.get("transaction_id", "")
            bucket = by_business[business_id][og]

            if not bucket["meta"]:
                bucket["meta"] = {
                    "customer_name":    cust_name,
                    "order_group":      str(og)[:8].upper(),
                    "locality":         txn.get("locality", ""),
                    "fulfillment_type": txn.get("fulfillment_type", ""),
                    "delivery_address": txn.get("delivery_address", ""),
                }
            bucket["items"].append({
                "product_name":  txn.get("product_name", ""),
                "quantity":      int(txn.get("quantity", 1)),
                "variant_label": txn.get("variant_label", ""),
            })

    if not by_business:
        print(json.dumps({"event": "delivery_reminder.done", "sent": 0, "tomorrow": tomorrow}))
        return {"statusCode": 200, "body": json.dumps({"sent": 0})}

    sent = 0
    for business_id, groups in by_business.items():
        business = _get_business(business_id)
        if not business:
            continue
        if not business.get("delivery_reminder_enabled", False):
            continue

        owner_email = _owner_email(business)
        if not owner_email:
            continue

        groups_for_email = [
            {**g["meta"], "order_items": g["items"]}
            for g in groups.values()
        ]

        try:
            delivery_reminder_email(
                to_address=owner_email,
                business_name=business.get("name", ""),
                delivery_date=tomorrow,
                order_groups=groups_for_email,
                admin_url="https://qatalo.online/admin",
            )
            sent += 1
            print(json.dumps({
                "event": "delivery_reminder.sent",
                "business_id": business_id,
                "orders": len(groups_for_email),
            }))
        except Exception as e:
            print(json.dumps({
                "event": "delivery_reminder.error",
                "business_id": business_id,
                "Error": str(e),
            }))

    print(json.dumps({"event": "delivery_reminder.done", "sent": sent, "tomorrow": tomorrow}))
    return {"statusCode": 200, "body": json.dumps({"sent": sent})}