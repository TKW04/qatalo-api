"""Texto del PDF de factura/recibo: emojis y caracteres fuera de latin-1 no deben
romper el render (fuentes core de fpdf = latin-1), y el texto latin-1 (ñ, acentos,
¿¡) y los importes no deben cambiar.

Ejecutar:  python3 -m pytest -q tests
Los tests de render real se saltan si fpdf2 no está instalado (en los tests
normales fpdf es un stub); con fpdf2 instalado generan el PDF de verdad.
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import invoice_generator as ig  # noqa: E402
from invoice_generator import pdf_safe_text, safe_filename_part  # noqa: E402

REAL_FPDF = ig.FPDF is not object
try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False

LATIN1_SAMPLES = [
    "Niño Pequeño", "Árbol Éxito Índigo Óscar Último", "pingüino", "¿Qué tal? ¡Hola!",
    "Café «especial» 50% $10 £2 ¥3 ¢4 © ® ° ª º ½", "  Descripción", "Calle 5ª, Apto. 3-B",
]

HOSTILE = {
    "emoji": ("Tienda 🛍️ Feliz 🔥", "Tienda Feliz"),
    "emoji_inicio": ("🔥Promo", "Promo"),
    "emoji_final": ("Oferta 🔥", "Oferta"),
    "emoji_zwj": ("👨‍👩‍👧 Familia", "Familia"),
    "solo_emoji": ("🛍️🔥", ""),
    "asiatico": ("東京 Store 寿司", "Store"),
    "comillas": ("“Especial” ‘del’ día", "\"Especial\" 'del' día"),
    "guiones": ("Niño – grande — azul − 1", "Niño - grande - azul - 1"),
    "elipsis_euro": ("Ahorra €5…", "Ahorra EUR5..."),
    "fullwidth_ligadura": ("Ａｂｃ ﬁno", "Abc fino"),
    "acento_no_latin1": ("Łódź Dvořák Ő Œuvre", "Lódz Dvorák O OEuvre"),
    "decomp_nfd": ("José Niño", "José Niño"),
    "controles": ("a\tb\r\nc\x07d​e", "a b\ncde"),
    "emoji_fin_de_linea": ("Apto 3 \U0001F3E0\n\U0001F525 Sector", "Apto 3\nSector"),
}


class PdfSafeTextTest(unittest.TestCase):
    def test_latin1_untouched(self):
        for s in LATIN1_SAMPLES:
            self.assertEqual(pdf_safe_text(s), s)

    def test_hostile_inputs(self):
        for key, (raw, expected) in HOSTILE.items():
            with self.subTest(key):
                out = pdf_safe_text(raw)
                self.assertEqual(out, expected)
                out.encode("latin-1")  # siempre representable

    def test_idempotent_and_none(self):
        for raw, _ in HOSTILE.values():
            once = pdf_safe_text(raw)
            self.assertEqual(pdf_safe_text(once), once)
        self.assertEqual(pdf_safe_text(None), "")
        self.assertEqual(pdf_safe_text(12.5), "12.5")

    def test_latin1_alias(self):
        self.assertEqual(ig._latin1("Promo 🔥 “X”"), "Promo \"X\"")

    def test_safe_filename_part(self):
        self.assertEqual(safe_filename_part("AB12CD34"), "AB12CD34")
        self.assertEqual(safe_filename_part("Ñandú 🔥 \"x\"/../y"), "Nandu-x-y")
        self.assertEqual(safe_filename_part("🔥🛍️", fallback="orden"), "orden")
        self.assertEqual(safe_filename_part(None), "doc")
        for raw, _ in HOSTILE.values():
            out = safe_filename_part(raw)
            self.assertRegex(out, r"^[A-Za-z0-9_-]+$")


def _hostile_doc(invoice_type):
    raw_items = [
        {"product_name": "Camiseta 🔥 “Edición” — limitada", "variant_label": "Talla M 🛍️ / 東京",
         "quantity": 2, "price": "90", "original_price": "100", "discount_amount": "20",
         "itbis_mode": "included", "delivery_price": "150",
         "offer_name": "2x1 🎉 “Navidad” – ñoña"},
        {"product_name": "🛍️🔥", "variant_label": "", "quantity": 1, "price": "300",
         "itbis_mode": "added", "offer_name": ""},
        {"product_name": "Piña colada ¿grande? ¡sí! (personalización: “Feliz 🎂 cumple, Ana”…)",
         "variant_label": "Envase ½ litro", "quantity": 3, "price": "33.333333",
         "original_price": "50", "discount_amount": "50", "itbis_mode": "exempt",
         "offer_name": "Promo 寿司"},
    ]
    with_ncf = invoice_type == "factura"
    items, totals = ig.calc_invoice_totals(raw_items, 18, with_ncf)
    business = {"name": "Tienda 🛍️ Doña Peña “La Mejor” — 東京", "phone": "809–555–1234 📞",
                "rnc": "1-01-23456-7", "themePalette": {"primary": "#113f67"}}
    customer = {"name": "José Ñúñez 😀 李", "phone": "+1 809 555 0000",
                "address": "Calle “Duarte” #5 — Apto 3 🏠\nSector Piantini, 東京",
                "email": "x@example.com"}
    meta = {"invoice_type": invoice_type, "ncf": "B0100000001" if with_ncf else "",
            "order_ref": "AB12CD34", "date": "08/10/2026", "currency": "DOP ",
            "payment_method": "Transferencia 💳 – Banco “Popular”", "status": "Pagado ✅"}
    return business, customer, items, totals, meta


@unittest.skipUnless(REAL_FPDF, "fpdf2 no instalado (stub)")
class RealRenderTest(unittest.TestCase):
    def _render(self, invoice_type):
        business, customer, items, totals, meta = _hostile_doc(invoice_type)
        with mock.patch.object(ig, "_fetch_logo", return_value=None):
            pdf = ig.build_invoice_pdf(business, customer, items, totals, meta)
        self.assertTrue(pdf.startswith(b"%PDF"))
        return pdf, totals

    def _text(self, pdf):
        import io
        from pypdf import PdfReader
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)

    def test_recibo_and_factura_render(self):
        for kind in ("recibo", "factura"):
            with self.subTest(kind):
                pdf, totals = self._render(kind)
                if not HAVE_PYPDF:
                    continue
                txt = self._text(pdf)
                self.assertIn(ig._money(totals["total"], "DOP "), txt)
                for s in ("Tienda Doña Peña \"La Mejor\" -", "José Ñúñez", "Piña colada ¿grande? ¡sí!",
                          "Calle \"Duarte\" #5 - Apto 3", "Sector Piantini",
                          "Transferencia - Banco \"Popular\"", "Estado: Pagado", "Producto",
                          "2x1 \"Navidad\" - ñoña", "Método de pago"):
                    self.assertIn(s, txt)
                for bad in ("🔥", "🛍", "東京", "“", "—"):
                    self.assertNotIn(bad, txt)

    def test_total_unchanged_by_text(self):
        business, customer, items, totals, meta = _hostile_doc("factura")
        clean = [dict(it, product_name="X", variant_label="", offer_name="") for it in items]
        with mock.patch.object(ig, "_fetch_logo", return_value=None):
            ig.build_invoice_pdf(business, customer, clean, totals, meta)
        raw = [{"quantity": 2, "price": "90", "original_price": "100", "discount_amount": "20",
                "itbis_mode": "included", "delivery_price": "150"},
               {"quantity": 1, "price": "300", "itbis_mode": "added"},
               {"quantity": 3, "price": "33.333333", "original_price": "50",
                "discount_amount": "50", "itbis_mode": "exempt"}]
        _, plain_totals = ig.calc_invoice_totals(raw, 18, True)
        self.assertEqual(totals, plain_totals)

    def test_normalize_text_hook_is_single_point(self):
        pdf = ig._InvoicePDF((0, 0, 0), (0, 0, 0), format="A4")
        pdf.add_page()
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, "Directo 🔥 “sin” helper — 東京")   # no debe lanzar
        pdf.ln()
        pdf.multi_cell(0, 5, "Multi 🛍️\nlínea – ñ")
        self.assertGreater(pdf.get_string_width("ñ 🔥"), 0)


if __name__ == "__main__":
    unittest.main()
