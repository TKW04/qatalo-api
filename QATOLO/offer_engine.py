"""Motor de ofertas v2 (servidor).

Equivalente exacto de qatalo-web/src/helpers/offerEngine.js, pero con Decimal.
Ambos motores deben pasar los casos de tests/fixtures/offerCases.json
(copia de qatalo-web/src/helpers/__fixtures__/offerCases.json).

Los "items" son dicts genéricos con { product_id, category_id?, price, quantity }.

Reglas v2:
- min_order_amount se mide contra el subtotal de las líneas ELEGIBLES.
- min_quantity (opcional): mínimo de unidades elegibles para que la oferta aplique.
- fixed + fixed_mode 'order' (default): discount_value una vez, tope = subtotal elegible.
- fixed + fixed_mode 'per_unit': discount_value × unidades, tope por línea = subtotal de la línea.
- buy_x_get_y: agrupa por product_id (variantes juntas); se regalan las unidades más baratas.
- Una sola oferta ganadora: prioridad alta > media > baja; empate → mayor descuento.

Sin dependencias externas (solo stdlib).
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")
# Precisión con la que se guarda el precio unitario descontado (price × qty
# reproduce el total al centavo sin acumular error de redondeo).
UNIT_PRICE_Q = Decimal("0.000001")

PRIORITY_RANK = {"alta": 3, "media": 2, "baja": 1}


# ───────── Conversión numérica (misma semántica que Number(x) || d en JS) ─────────
def to_dec(value, default=ZERO):
    """Number(value) || default, en Decimal. NaN / vacío / 0 / inválido → default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not d.is_finite() or d == 0:
        return default
    return d


def round2(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def qty_of(it):
    return to_dec(it.get("quantity"), Decimal("1"))


def price_of(it):
    return to_dec(it.get("price"), ZERO)


def line_subtotal(it):
    return price_of(it) * qty_of(it)


def priority_rank(offer):
    return PRIORITY_RANK.get(str((offer or {}).get("priority") or "media").lower(), 2)


def fixed_mode_of(offer):
    return "per_unit" if (offer or {}).get("fixed_mode") == "per_unit" else "order"


# ───────── Elegibilidad ─────────
def is_item_eligible(offer, it):
    scope = offer.get("applies_to") or "all"
    if scope == "all":
        return True
    if scope == "products":
        return it.get("product_id") in (offer.get("product_ids") or [])
    if scope == "categories":
        return (it.get("category_id") or "") in (offer.get("category_ids") or [])
    return False


def get_applicable_items(offer, items):
    if not offer:
        return []
    return [it for it in (items or []) if is_item_eligible(offer, it)]


def is_offer_applicable(offer, items):
    if not offer:
        return False
    applicable = get_applicable_items(offer, items)
    if not applicable:
        return False
    min_amount = to_dec(offer.get("min_order_amount"), ZERO)
    if min_amount > 0:
        eligible_sub = sum((line_subtotal(it) for it in applicable), ZERO)
        if eligible_sub < min_amount:
            return False
    min_qty = to_dec(offer.get("min_quantity"), ZERO)
    if min_qty > 0:
        eligible_qty = sum((qty_of(it) for it in applicable), ZERO)
        if eligible_qty < min_qty:
            return False
    return True


# ───────── Núcleo ─────────
def _settle_residual(line_discounts, items, eligible_idx, total):
    """Ajusta el residuo para que la suma por línea = total, en la línea elegible
    de mayor subtotal (primera en caso de empate), sin pasar de su subtotal ni de 0."""
    s = round2(sum(line_discounts, ZERO))
    diff = round2(total - s)
    if diff == 0 or not eligible_idx:
        return line_discounts
    target = eligible_idx[0]
    for i in eligible_idx:
        if line_subtotal(items[i]) > line_subtotal(items[target]):
            target = i
    line_discounts[target] = round2(
        min(round2(line_subtotal(items[target])), max(ZERO, line_discounts[target] + diff))
    )
    return line_discounts


def compute_line_discounts(offer, items):
    """Descuento por línea (alineado con `items`), redondeado a 2 decimales.
    Lista de ceros si la oferta no aplica."""
    lst = list(items or [])
    zeros = [ZERO for _ in lst]
    if not offer or not is_offer_applicable(offer, lst):
        return zeros

    eligible_idx = [i for i, it in enumerate(lst) if is_item_eligible(offer, it)]
    dtype = offer.get("discount_type")

    if dtype == "buy_x_get_y":
        X = int(to_dec(offer.get("buy_quantity"), ZERO))
        Y = int(to_dec(offer.get("paid_quantity"), ZERO))
        if X < 2 or Y < 1 or Y >= X:
            return zeros
        groups = {}
        order = []
        for i in eligible_idx:
            pid = lst[i].get("product_id")
            if pid not in groups:
                groups[pid] = []
                order.append(pid)
            groups[pid].append(i)
        out = list(zeros)
        for pid in order:
            idxs = groups[pid]
            total_qty = sum((qty_of(lst[i]) for i in idxs), ZERO)
            free = (total_qty // X) * (X - Y)
            # Más baratas primero; empate → orden original de las líneas
            for i in sorted(idxs, key=lambda k: (price_of(lst[k]), k)):
                if free <= 0:
                    break
                take = min(free, qty_of(lst[i]))
                out[i] = round2(out[i] + take * price_of(lst[i]))
                free -= take
        return out

    value = to_dec(offer.get("discount_value"), ZERO)
    if value <= 0:
        return zeros

    if dtype == "percentage":
        pct = min(value, Decimal("100")) / Decimal("100")
        eligible_sub = sum((line_subtotal(lst[i]) for i in eligible_idx), ZERO)
        total = round2(eligible_sub * pct)
        out = list(zeros)
        for i in eligible_idx:
            out[i] = round2(line_subtotal(lst[i]) * pct)
        return _settle_residual(out, lst, eligible_idx, total)

    if dtype == "fixed":
        out = list(zeros)
        if fixed_mode_of(offer) == "per_unit":
            for i in eligible_idx:
                out[i] = round2(min(value * qty_of(lst[i]), line_subtotal(lst[i])))
            return out
        eligible_sub = sum((line_subtotal(lst[i]) for i in eligible_idx), ZERO)
        total = round2(min(value, eligible_sub))
        if eligible_sub <= 0:
            return zeros
        for i in eligible_idx:
            out[i] = round2(total * line_subtotal(lst[i]) / eligible_sub)
        return _settle_residual(out, lst, eligible_idx, total)

    return zeros


def _clean_unit(d):
    """Quita ceros sobrantes sin notación exponencial (100.000000 → 100.00)."""
    if d == d.quantize(CENT):
        return d.quantize(CENT)
    return d.normalize()


def calc_discount(offer, items):
    return round2(sum(compute_line_discounts(offer, items), ZERO))


def pick_winning_offer(candidates, items):
    """Devuelve (winner, discount). Mayor prioridad gana; empate → mayor descuento."""
    winner, winner_discount = None, ZERO
    for o in candidates or []:
        if not is_offer_applicable(o, items):
            continue
        d = calc_discount(o, items)
        if d <= 0:
            continue
        if winner is None:
            winner, winner_discount = o, d
            continue
        pr, pw = priority_rank(o), priority_rank(winner)
        if pr > pw or (pr == pw and d > winner_discount):
            winner, winner_discount = o, d
    return winner, winner_discount


def distribute_discount(offer, items, total_discount=None):
    """Devuelve una lista (alineada con `items`) de dicts:
    { original_price, discount_amount, price (unitario descontado), offer_id }.
    Sin oferta o total <= 0 → sin descuento (price = original_price, offer_id '')."""
    lst = list(items or [])
    if total_discount is None:
        total_discount = calc_discount(offer, lst) if offer else ZERO
    if not offer or not (to_dec(total_discount, ZERO) > 0):
        return [
            {"original_price": price_of(it), "discount_amount": ZERO, "price": price_of(it), "offer_id": ""}
            for it in lst
        ]
    lines = compute_line_discounts(offer, lst)
    out = []
    for it, d in zip(lst, lines):
        price = price_of(it)
        if not d:
            out.append({"original_price": price, "discount_amount": ZERO, "price": price, "offer_id": ""})
            continue
        unit = max(ZERO, price - d / qty_of(it)).quantize(UNIT_PRICE_Q, rounding=ROUND_HALF_UP)
        out.append(
            {
                "original_price": price,
                "discount_amount": d,
                "price": _clean_unit(unit),
                "offer_id": offer.get("offer_id", ""),
            }
        )
    return out
