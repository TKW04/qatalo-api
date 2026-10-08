"""
Generador de facturas / recibos en PDF para Qatalo.
Usa fpdf2 (Python puro, sin dependencias binarias → funciona en Lambda por zip).

Función principal:
    build_invoice_pdf(business, customer, items, totals, meta) -> bytes

Donde:
  business : dict del negocio (name, logo_url, rnc, themePalette, phone, ...)
  customer : dict del cliente (name, phone, address, email)
  items    : lista de líneas ya calculadas (ver _calc_invoice_totals)
  totals   : dict de calc_invoice_totals (subtotal, descuento_aplicado, neto,
             sub_gravado, sub_exento, itbis*, delivery, total)
  meta     : dict con invoice_type ("factura"|"recibo"), ncf, order_ref, date,
             payment_method, status
"""

import json
import io
import re
import unicodedata
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


def _line_original_total(line_gross, qty, original_price, discount_amount):
    """Importe de la línea ANTES del descuento (precio de lista × cantidad).

    - Línea sin descuento (discount_amount <= 0) → line_gross (se ignora un
      original_price viejo/incoherente: no se inventa un descuento).
    - Con original_price → round2(original_price × qty).
    - Órdenes antiguas sin original_price → line_gross + discount_amount.
    Nunca menor que line_gross (no hay descuentos negativos).
    Devuelve (original_total, original_unit).
    """
    disc = _D(discount_amount)
    orig = _D(original_price)
    unit_net = (line_gross / qty) if qty > 0 else line_gross
    if disc <= 0:
        return line_gross, unit_net
    if orig > 0:
        total = _q2(orig * qty)
        if total >= line_gross:
            return total, orig
    total = line_gross + _q2(disc)
    return total, ((total / qty) if qty > 0 else total)


def calc_invoice_totals(raw_items, itbis_rate, with_ncf):
    """
    Calcula los totales de la factura/recibo desglosando ITBIS por línea
    según el itbis_mode de cada producto.

    raw_items: lista de dicts con:
        product_name, variant_label, quantity, price (precio unitario final
        que el cliente paga, ya con descuento aplicado), itbis_mode,
        delivery_price (por línea, opcional), original_price (precio unitario
        sin descuento, opcional), discount_amount (descuento de la línea),
        offer_name (opcional)

    Retorna (items_calculados, totals_dict).

    Reglas fiscales (sin cambios: se calculan SIEMPRE sobre el precio ya
    descontado, que es lo que el cliente paga):
      - Sin NCF (recibo): no se desglosa ITBIS. Todo va como total simple.
      - included: el precio YA incluye ITBIS → se desglosa (base = precio/(1+tasa)).
      - added:    el precio NO incluye ITBIS → se suma (itbis = precio*tasa).
      - exempt:   no paga ITBIS.

    Presentación (para que el PDF cuadre a la vista):
      subtotal            = Σ precio_original × cant           (antes de descuento)
      descuento_aplicado  = subtotal − neto                    (≈ Σ discount_amount;
                            se deriva así para que no haya céntimos sueltos)
      neto                = Σ round2(price × cant)             (lo que se cobra por productos)
      neto = sub_gravado + sub_exento + itbis_incluido         (desglose fiscal del neto)
      total = neto + itbis_agregado + delivery                 (idéntico al cálculo previo)
    """
    rate = _D(itbis_rate) / Decimal("100")
    items_calc = []

    sub_gravado = Decimal("0")   # base imponible (sin itbis)
    sub_exento = Decimal("0")    # base de productos exentos / recibo
    itbis_total = Decimal("0")
    itbis_incluido = Decimal("0")
    itbis_agregado = Decimal("0")
    delivery_total = Decimal("0")
    line_total_sum = Decimal("0")
    subtotal = Decimal("0")      # antes de descuento
    neto = Decimal("0")          # después de descuento (Σ line_gross)

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
            itbis_agregado += itbis
        else:  # included
            base = _q2(line_gross / (Decimal("1") + rate))
            itbis = _q2(line_gross - base)
            sub_gravado += base
            itbis_total += itbis
            itbis_incluido += itbis

        # total de la línea como lo ve el cliente
        line_payable = base + itbis if mode == "added" and with_ncf else line_gross
        line_total_sum += line_payable

        orig_total, orig_unit = _line_original_total(
            line_gross, qty, it.get("original_price"), it.get("discount_amount")
        )
        line_discount = orig_total - line_gross
        subtotal += orig_total
        neto += line_gross

        items_calc.append({
            "product_name": it.get("product_name", ""),
            "variant_label": it.get("variant_label", ""),
            "quantity": int(qty),
            "unit_price": orig_unit,          # precio unitario de lista (mostrado)
            "unit_price_net": unit,           # precio unitario cobrado (con descuento)
            "line_subtotal": orig_total,      # = unit_price × cant (columna Total)
            "line_discount": line_discount,   # descuento mostrado bajo la línea
            "offer_name": it.get("offer_name", "") or "",
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
        "itbis_incluido": _q2(itbis_incluido),
        "itbis_agregado": _q2(itbis_agregado),
        "descuento": _q2(descuento),                  # Σ discount_amount (dato crudo)
        "subtotal": _q2(subtotal),
        "descuento_aplicado": _q2(subtotal - neto),   # el que se imprime
        "neto": _q2(neto),
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


def _neg_money(v, symbol=""):
    """Importe negativo en ASCII (las fuentes core de fpdf son latin-1)."""
    return "-" + _money(abs(_D(v)), symbol)


# ──────────────────────────────────────────────────────────
#  Texto seguro para el PDF
#  Las fuentes core de fpdf (Helvetica) solo codifican latin-1: cualquier
#  carácter fuera (emoji, CJK, comillas tipográficas, guion largo, €…) lanza
#  FPDFUnicodeEncodingException y tumba todo el PDF. pdf_safe_text se aplica
#  en UN solo punto (_InvoicePDF.normalize_text, por donde pasa todo texto de
#  cell/multi_cell/get_string_width) → ningún campo puede romper el render.
# ──────────────────────────────────────────────────────────
# Equivalentes legibles para caracteres frecuentes fuera de latin-1.
_PDF_TEXT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2039": "<", "\u203a": ">",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\u2043": "-",
    "\u2026": "...", "\u2022": "-", "\u2023": "-", "\u2219": "-", "\u25cf": "-",
    "\u20ac": "EUR", "\u2122": "TM", "\u2116": "No.",
    # Letras latinas sin descomposición NFKD
    "\u0141": "L", "\u0142": "l", "\u0110": "D", "\u0111": "d", "\u0131": "i",
    "\u0152": "OE", "\u0153": "oe", "\u0126": "H", "\u0127": "h",
    "\t": " ",
}
_DROPPED = "\x00"   # marcador interno de caracteres eliminados (se limpia al final)
_DROPPED_RUN = re.compile(r"[ \x00]*\x00[ \x00]*")


def pdf_safe_text(text, encoding="latin-1"):
    """Texto representable por una fuente core de fpdf (latin-1 por defecto).

    - Conserva todo lo latin-1 (ñ, á…ú, ü, ¿, ¡, «», °, $, £, ¥, ¢, ©, ®…).
    - Traduce comillas tipográficas, guiones largos, elipsis, €… a equivalentes ASCII.
    - Translitera el resto por NFKD (ő→o, ﬁ→fi, Ａ→A) quitando diacríticos no latin-1.
    - Elimina lo no representable (emoji, CJK, ZWJ, selectores de variación,
      controles) sin dejar espacios dobles donde estaba.
    Idempotente; acepta None/números.
    """
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text))
    out = []
    for ch in s:
        if ch == "\n":
            out.append(ch)
            continue
        if ch in _PDF_TEXT_MAP:
            out.append(_PDF_TEXT_MAP[ch])
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            out.append(_DROPPED)
            continue
        try:
            ch.encode(encoding)
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        rep = []
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            c = _PDF_TEXT_MAP.get(c, c)
            try:
                c.encode(encoding)
                rep.append(c)
            except UnicodeEncodeError:
                pass
        out.append("".join(rep) or _DROPPED)
    res = "".join(out)
    if _DROPPED in res:
        def _gap(m):
            # Borde de texto o de línea → sin espacio sobrante
            at_edge = (m.start() == 0 or m.end() == len(res)
                       or res[m.start() - 1] == "\n" or res[m.end()] == "\n")
            return "" if at_edge or " " not in m.group(0) else " "
        res = _DROPPED_RUN.sub(_gap, res)
    return res


def _latin1(text):
    """Compatibilidad: alias de pdf_safe_text."""
    return pdf_safe_text(text)


def safe_filename_part(text, fallback="doc", max_len=40):
    """Fragmento ASCII seguro para nombres de archivo / claves S3 /
    Content-Disposition: [A-Za-z0-9_-], sin acentos ni emoji."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-_")[:max_len].strip("-_")
    return s or fallback


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

    def normalize_text(self, text):
        # Punto único de sanitizado: todo texto del PDF pasa por aquí.
        if not getattr(self, "is_ttf_font", False):
            text = pdf_safe_text(text, getattr(self, "core_fonts_encoding", None) or "latin-1")
        return super().normalize_text(text)

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
    pdf.cell(0, 7, pdf_safe_text(business.get("name"))[:50], ln=1)

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
    pdf.cell(0, 5, pdf_safe_text(customer.get("name")).strip() or "Consumidor final", ln=1)
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
        # Sanitizar antes de recortar para que el largo sea el impreso.
        name = pdf_safe_text(it.get("product_name")).strip()
        variant = pdf_safe_text(it.get("variant_label")).strip()
        if variant:
            name = f"{name} ({variant})" if name else variant
        name = name or "Producto"
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
        # Total de la línea a precio de lista (= Precio × Cant.); el descuento
        # va en una sub-fila y el ITBIS agregado en los totales.
        pdf.cell(col["total"], h, _money(it["line_subtotal"], symbol) + "  ", border=0, fill=fill, align="R", ln=1)
        if it.get("line_discount", 0) > 0:
            label = "Descuento"
            if it.get("offer_name"):
                label += f" ({pdf_safe_text(it['offer_name']).strip()[:30]})"
            pdf.set_font("Helvetica", "I", 7.5)
            pdf.set_text_color(6, 118, 71)
            pdf.cell(W - col["total"], 5, f"      {label}", border=0, fill=fill)
            pdf.cell(col["total"], 5, _neg_money(it["line_discount"], symbol) + "  ",
                     border=0, fill=fill, align="R", ln=1)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(50, 50, 50)
        fill = not fill

    pdf.ln(4)

    # ── Totales (alineados a la derecha) ──
    label_w = W * 0.62
    val_w = W * 0.38

    def total_row(label, value, bold=False, color=None, big=False, small=False, negative=False):
        pdf.set_x(pdf.l_margin)
        pdf.cell(label_w, 7 if not small else 5, "", border=0)  # espacio vacío a la izquierda
        if small:
            pdf.set_font("Helvetica", "I", 8)
        else:
            pdf.set_font("Helvetica", "B" if bold else "", 11 if big else 9)
        pdf.set_text_color(*(color or ((120, 120, 120) if small else (60, 60, 60))))
        txt = _neg_money(value, symbol) if negative else _money(value, symbol)
        pdf.cell(val_w * 0.5, 7 if not small else 5, label, align="R")
        pdf.cell(val_w * 0.5, 7 if not small else 5, txt + "  ", align="R", ln=1)

    # Cadena que suma a la vista:
    #   Subtotal (precios de lista) − Descuento = Subtotal con descuento
    #   [factura: desglose fiscal de ese neto = Base gravada + Exento + ITBIS incluido]
    #   + ITBIS agregado + Delivery = TOTAL
    subtotal = totals.get("subtotal", totals["sub_exento"] + totals["sub_gravado"])
    desc = totals.get("descuento_aplicado", Decimal("0"))
    total_row("Subtotal:", subtotal)
    if desc > 0:
        total_row("Descuento:", desc, color=(6, 118, 71), negative=True)
        total_row("Subtotal con descuento:", totals.get("neto", subtotal - desc))

    if is_factura:
        if totals["sub_gravado"] > 0:
            total_row("Base gravada:", totals["sub_gravado"], small=True)
        if totals["sub_exento"] > 0:
            total_row("Exento:", totals["sub_exento"], small=True)
        if totals.get("itbis_incluido", 0) > 0:
            total_row("ITBIS incluido:", totals["itbis_incluido"], small=True)
        if totals.get("itbis_agregado", 0) > 0:
            total_row("ITBIS agregado:", totals["itbis_agregado"])

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