"""
Generador de facturas / recibos en PDF para Qatalo.
Usa fpdf2 (Python puro, sin dependencias binarias → funciona en Lambda por zip).

Función principal:
    build_invoice_pdf(business, customer, items, totals, meta) -> bytes

Donde:
  business : dict del negocio (name, logo_url, rnc, themePalette, phone, ...)
  customer : dict del cliente (name, phone, address, email)
  items    : lista de líneas ya calculadas (ver _calc_invoice_totals)
  totals   : dict con subtotal_gravado, itbis, exento, descuento, delivery, total
  meta     : dict con invoice_type ("factura"|"recibo"), ncf, order_ref, date,
             payment_method, status
"""

import json
import io
import urllib.request
from decimal import Decimal
import PIL
from PIL import Image
from fpdf import FPDF
# ──────────────────────────────────────────────────────────
#  Cálculo de ITBIS por línea
# ──────────────────────────────────────────────────────────
def _D(v):
    return Decimal(str(v or 0))


def _q2(d):
    """Redondea a 2 decimales."""
    return d.quantize(Decimal("0.01"))


def calc_invoice_totals(raw_items, itbis_rate, with_ncf):
    """
    Calcula los totales de la factura/recibo desglosando ITBIS por línea
    según el itbis_mode de cada producto.

    raw_items: lista de dicts con:
        product_name, variant_label, quantity, price (precio unitario final
        que el cliente paga, ya con descuento aplicado), itbis_mode,
        delivery_price (por línea, opcional)

    Retorna (items_calculados, totals_dict).

    Reglas:
      - Sin NCF (recibo): no se desglosa ITBIS. Todo va como total simple.
      - included: el precio YA incluye ITBIS → se desglosa (base = precio/(1+tasa)).
      - added:    el precio NO incluye ITBIS → se suma (itbis = precio*tasa).
      - exempt:   no paga ITBIS.
    """
    rate = _D(itbis_rate) / Decimal("100")
    items_calc = []

    sub_gravado = Decimal("0")   # base imponible (sin itbis)
    sub_exento = Decimal("0")    # base de productos exentos / recibo
    itbis_total = Decimal("0")
    delivery_total = Decimal("0")
    line_total_sum = Decimal("0")

    for it in raw_items:
        qty = _D(it.get("quantity", 1))
        unit = _D(it.get("price", 0))
        mode = it.get("itbis_mode", "included")
        line_gross = _q2(unit * qty)          # lo que el cliente paga por la línea

        if not with_ncf:
            # Recibo: sin desglose
            base = line_gross
            itbis = Decimal("0")
            sub_exento += base
        elif mode == "exempt":
            base = line_gross
            itbis = Decimal("0")
            sub_exento += base
        elif mode == "added":
            base = line_gross
            itbis = _q2(base * rate)
            sub_gravado += base
            itbis_total += itbis
        else:  # included
            base = _q2(line_gross / (Decimal("1") + rate))
            itbis = _q2(line_gross - base)
            sub_gravado += base
            itbis_total += itbis

        # total de la línea como lo ve el cliente
        line_payable = base + itbis if mode == "added" and with_ncf else line_gross
        line_total_sum += line_payable

        items_calc.append({
            "product_name": it.get("product_name", ""),
            "variant_label": it.get("variant_label", ""),
            "quantity": int(qty),
            "unit_price": unit,
            "base": base,
            "itbis": itbis,
            "line_total": line_payable,
            "itbis_mode": mode,
        })

    delivery_total = sum((_D(it.get("delivery_price", 0)) for it in raw_items), Decimal("0"))
    descuento = sum((_D(it.get("discount_amount", 0)) for it in raw_items), Decimal("0"))

    total = line_total_sum + delivery_total

    totals = {
        "sub_gravado": _q2(sub_gravado),
        "sub_exento": _q2(sub_exento),
        "itbis": _q2(itbis_total),
        "descuento": _q2(descuento),
        "delivery": _q2(delivery_total),
        "total": _q2(total),
    }
    return items_calc, totals


# ──────────────────────────────────────────────────────────
#  Utilidades de color / formato
# ──────────────────────────────────────────────────────────
def _hex_to_rgb(h, fallback=(17, 63, 103)):
    try:
        h = (h or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _money(v, symbol=""):
    d = _q2(_D(v))
    s = f"{d:,.2f}"
    return f"{symbol}{s}" if symbol else s


def _fetch_logo(url):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Qatalo-Invoice"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            data = resp.read()
            is_png = data[:4] == b'\x89PNG'
            is_jpg = data[:2] == b'\xff\xd8'
            if not (is_png or is_jpg):
                return None
            buf = io.BytesIO(data)
            buf.seek(0)        # ← asegura que el puntero esté al inicio
            return buf
    except Exception as e:
        print(json.dumps({"event": "_fetch_logo", "error": str(e)}))
        return None


# ──────────────────────────────────────────────────────────
#  PDF
# ──────────────────────────────────────────────────────────
class _InvoicePDF(FPDF):
    def __init__(self, primary, accent, *a, **kw):
        super().__init__(*a, **kw)
        self.primary = primary
        self.accent = accent
        self.set_auto_page_break(auto=True, margin=18)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 5, "Generado con Qatalo  -  qatalo.online", align="C")


def build_invoice_pdf(business, customer, items, totals, meta):
    primary = _hex_to_rgb((business.get("themePalette") or {}).get("primary"), (17, 63, 103))
    accent = _hex_to_rgb((business.get("themePalette") or {}).get("secondary"), (52, 105, 154))

    is_factura = meta.get("invoice_type") == "factura"
    symbol = meta.get("currency", "")

    pdf = _InvoicePDF(primary, accent, format="A4")
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    # ── Encabezado: logo + datos del negocio ──
    logo = _fetch_logo(business.get("business_logo_url"))
    top_y = pdf.get_y()
    if logo:
        try:
            logo.seek(0)    # por si acaso
            pdf.image(logo, x=pdf.l_margin, y=top_y, w=28, type="PNG")
            text_x = pdf.l_margin + 33
        except Exception as e:
            print(json.dumps({"event": "build_invoice_pdf.logo", "error": str(e)}))
            text_x = pdf.l_margin
    else:
        text_x = pdf.l_margin

    pdf.set_xy(text_x, top_y)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*primary)
    pdf.cell(0, 7, business.get("name", "")[:50], ln=1)

    pdf.set_x(text_x)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    if business.get("phone"):
        pdf.cell(0, 5, f"Tel: {business.get('phone')}", ln=1)
        pdf.set_x(text_x)
    if business.get("rnc"):
        pdf.cell(0, 5, f"RNC: {business.get('rnc')}", ln=1)
        pdf.set_x(text_x)

    # ── Caja de tipo de comprobante (derecha) ──
    box_w = 62
    box_x = pdf.w - pdf.r_margin - box_w
    pdf.set_xy(box_x, top_y)
    pdf.set_fill_color(*primary)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 12)
    title = "FACTURA" if is_factura else "RECIBO DE PAGO"
    pdf.cell(box_w, 9, title, align="C", fill=True, ln=2)

    pdf.set_x(box_x)
    pdf.set_text_color(*primary)
    pdf.set_font("Helvetica", "", 8)
    if is_factura and meta.get("ncf"):
        pdf.cell(box_w, 6, f"NCF: {meta.get('ncf')}", align="C", ln=2)
        pdf.set_x(box_x)
    pdf.cell(box_w, 6, f"No. {meta.get('order_ref', '')}", align="C", ln=2)
    pdf.set_x(box_x)
    pdf.cell(box_w, 6, meta.get("date", ""), align="C", ln=2)

    # ── Línea separadora ──
    y = max(pdf.get_y(), top_y + 30) + 4
    pdf.set_draw_color(*accent)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.set_y(y + 6)

    # ── Datos del cliente ──
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*primary)
    pdf.cell(0, 6, "Cliente", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 5, customer.get("name", "") or "Consumidor final", ln=1)
    if customer.get("phone"):
        pdf.cell(0, 5, f"Tel: {customer.get('phone')}", ln=1)
    if customer.get("address"):
        pdf.multi_cell(0, 5, f"Dir: {customer.get('address')}")
    pdf.ln(3)

    # ── Tabla de productos ──
    # Anchos de columna
    if is_factura:
        col = {"desc": W * 0.40, "qty": W * 0.10, "price": W * 0.18, "itbis": W * 0.14, "total": W * 0.18}
    else:
        col = {"desc": W * 0.54, "qty": W * 0.12, "price": W * 0.17, "total": W * 0.17}

    pdf.set_fill_color(*primary)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(col["desc"], 8, "  Descripción", border=0, fill=True)
    pdf.cell(col["qty"], 8, "Cant.", border=0, fill=True, align="C")
    pdf.cell(col["price"], 8, "Precio", border=0, fill=True, align="R")
    if is_factura:
        pdf.cell(col["itbis"], 8, "ITBIS", border=0, fill=True, align="R")
    pdf.cell(col["total"], 8, "Total  ", border=0, fill=True, align="R", ln=1)

    pdf.set_text_color(50, 50, 50)
    pdf.set_font("Helvetica", "", 8.5)
    fill = False
    for it in items:
        pdf.set_fill_color(245, 247, 250)
        name = it["product_name"]
        if it.get("variant_label"):
            name += f" ({it['variant_label']})"
        # Recortar nombre largo
        if len(name) > 48:
            name = name[:45] + "..."

        h = 7
        pdf.cell(col["desc"], h, f"  {name}", border=0, fill=fill)
        pdf.cell(col["qty"], h, str(it["quantity"]), border=0, fill=fill, align="C")
        pdf.cell(col["price"], h, _money(it["unit_price"], symbol), border=0, fill=fill, align="R")
        if is_factura:
            itbis_txt = "Exento" if it["itbis_mode"] == "exempt" else _money(it["itbis"], symbol)
            pdf.cell(col["itbis"], h, itbis_txt, border=0, fill=fill, align="R")
        pdf.cell(col["total"], h, _money(it["line_total"], symbol) + "  ", border=0, fill=fill, align="R", ln=1)
        fill = not fill

    pdf.ln(4)

    # ── Totales (alineados a la derecha) ──
    label_w = W * 0.62
    val_w = W * 0.38

    def total_row(label, value, bold=False, color=None, big=False):
        pdf.set_x(pdf.l_margin)
        pdf.cell(label_w, 7, "", border=0)  # espacio vacío a la izquierda
        pdf.set_font("Helvetica", "B" if bold else "", 11 if big else 9)
        pdf.set_text_color(*(color or (60, 60, 60)))
        pdf.cell(val_w * 0.5, 7, label, align="R")
        pdf.cell(val_w * 0.5, 7, _money(value, symbol) + "  ", align="R", ln=1)

    if is_factura:
        if totals["sub_gravado"] > 0:
            total_row("Subtotal gravado:", totals["sub_gravado"])
        if totals["sub_exento"] > 0:
            total_row("Subtotal exento:", totals["sub_exento"])
        if totals["itbis"] > 0:
            total_row(f"ITBIS:", totals["itbis"])
    else:
        total_row("Subtotal:", totals["sub_exento"] + totals["sub_gravado"])

    if totals["descuento"] > 0:
        total_row("Descuento:", -totals["descuento"], color=(6, 118, 71))
    if totals["delivery"] > 0:
        total_row("Delivery:", totals["delivery"])

    # Línea total
    pdf.set_x(pdf.l_margin + label_w)
    pdf.set_draw_color(*primary)
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin + label_w, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)
    total_row("TOTAL:", totals["total"], bold=True, color=primary, big=True)

    pdf.ln(6)

    # ── Pie: método de pago + estado ──
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(80, 80, 80)
    if meta.get("payment_method"):
        pdf.cell(0, 5, f"Método de pago: {meta.get('payment_method')}", ln=1)
    if meta.get("status"):
        pdf.cell(0, 5, f"Estado: {meta.get('status')}", ln=1)

    if not is_factura:
        pdf.ln(3)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(140, 140, 140)
        pdf.multi_cell(0, 4, "Este documento es un recibo de pago sin valor fiscal. "
                             "Para una factura con valor fiscal (NCF), solicítela al negocio.")

    return bytes(pdf.output())