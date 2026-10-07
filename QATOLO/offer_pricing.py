"""Precios y descuentos calculados en el servidor para checkouts y el admin.

Funciones puras (sin DynamoDB): customers.py lee productos/ofertas y llama aquí.
- catalog_unit_price: precio unitario de una línea según el producto en BD
  (misma regla que ProductModal.jsx: moneda alterna → precio fijo de esa moneda;
  variante 'size' → precio de la variante; otras variantes → precio + extra_price).
- validate_offer: oferta vigente para el negocio (activa, fechas, max_uses, código).
- price_lines: aplica el motor (offer_engine) y devuelve los valores por línea.
"""

from decimal import Decimal, InvalidOperation

import offer_engine as eng

ZERO = Decimal("0")
TOL = Decimal("0.01")


def _dec(v):
    try:
        if v is None or v == "" or isinstance(v, bool):
            return None
        d = v if isinstance(v, Decimal) else Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def item_variant_id(item):
    return (item.get("variant") or {}).get("variant_id") or item.get("variant_id") or ""


def catalog_unit_price(product, item):
    """(precio Decimal, None) o (None, motivo) si no se puede verificar con seguridad."""
    if not product:
        return None, "producto_no_encontrado"
    base = _dec(product.get("price"))
    if base is None:
        return None, "producto_sin_precio"

    # 1) Moneda alterna: precio fijo en esa moneda (sin variantes), igual que el front.
    alt_prices = product.get("alt_prices") or []
    currency = item.get("currency") or ""
    base_currency = product.get("currency") or ""
    if alt_prices and currency and currency != base_currency:
        alt = next((a for a in alt_prices if (a or {}).get("currency") == currency), None)
        if not alt:
            return None, "moneda_no_configurada"
        p = _dec(alt.get("price"))
        if p is None:
            return None, "moneda_sin_precio"
        return p, None

    # 2) Variantes (solo si el producto es personalizable, como en el front).
    variants = product.get("variants") or []
    vid = item_variant_id(item)
    if product.get("is_customizable") and variants and vid:
        v = next((x for x in variants if x.get("variant_id") == vid), None)
        if not v:
            return None, "variante_no_encontrada"
        if product.get("variant_type") == "size":
            p = _dec(v.get("price"))
            return (p, None) if p is not None else (ZERO, None)
        extra = _dec(v.get("extra_price")) or ZERO
        return base + extra, None

    # 3) Precio base
    return base, None


def offer_is_current(offer, business_id, today):
    """offer = item crudo de DynamoDB. Devuelve motivo de rechazo o None si es válida."""
    if not offer:
        return "oferta_no_encontrada"
    if offer.get("business_id") != business_id:
        return "oferta_de_otro_negocio"
    if not bool(offer.get("is_active", True)):
        return "oferta_inactiva"
    vf, vu = offer.get("valid_from") or "", offer.get("valid_until") or ""
    if vf and vf > today:
        return "oferta_aun_no_vigente"
    if vu and vu < today:
        return "oferta_vencida"
    mu = offer.get("max_uses")
    # max_uses None/""/0 = sin tope (igual que la condición atómica de offers.reserve_offer_use)
    if mu not in (None, "") and int(mu) > 0 and int(offer.get("uses_count", 0) or 0) >= int(mu):
        return "oferta_sin_usos_disponibles"
    return None


def code_matches(offer, offer_code):
    """Ofertas por código: el cliente debe haber enviado el código correcto."""
    if (offer.get("trigger") or "code") != "code":
        return True
    code = (offer.get("code") or "").strip().upper()
    return not code or code == (offer_code or "").strip().upper()


def price_lines(engine_items, offer):
    """engine_items: [{product_id, category_id, price, quantity}] con precio base del servidor.
    Devuelve (lines, total) donde lines = [{original_price, discount_amount, price}]."""
    lines = eng.distribute_discount(offer, engine_items) if offer else eng.distribute_discount(None, engine_items)
    total = sum((l["discount_amount"] for l in lines), ZERO)
    if total <= 0:
        lines = eng.distribute_discount(None, engine_items)
        total = ZERO
    return lines, total


def differs(a, b, tol=TOL):
    da, db = _dec(a) or ZERO, _dec(b) or ZERO
    return abs(da - db) > tol
