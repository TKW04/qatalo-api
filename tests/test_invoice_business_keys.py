"""El item crudo de DynamoDB (business_name/business_phone/theme_palette) debe
llegar al PDF y al correo de factura/recibo vía invoice_generator.business_doc_view.

Ejecutar:  python3 -m pytest -q tests/test_invoice_business_keys.py
"""
import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import invoice_generator as ig  # noqa: E402

REAL_FPDF = ig.FPDF is not object
try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False

# Así llega desde customers._get_business
RAW = {
    "business_id": "b1",
    "business_name": "Panadería La Esquina",
    "business_phone": "809-555-1234",
    "theme_palette": {"primary": "#aa0000", "secondary": "#00aa00"},
    "business_logo_url": "https://example.com/logo.png",
    "rnc": "101234567",
    "issuer_name": "María Elena Peña",
    "issuer_id": "00112345678",
    "issuer_id_type": "cedula",
}


class BusinessDocViewTest(unittest.TestCase):
    def test_maps_raw_keys_and_keeps_originals(self):
        view = ig.business_doc_view(RAW)
        self.assertEqual(view["name"], "Panadería La Esquina")
        self.assertEqual(view["phone"], "809-555-1234")
        self.assertEqual(view["themePalette"], RAW["theme_palette"])
        self.assertEqual(view["logo_url"], RAW["business_logo_url"])
        for k, v in RAW.items():
            self.assertEqual(view[k], v)
        self.assertNotIn("name", RAW)  # no muta el item original

    def test_does_not_override_existing(self):
        view = ig.business_doc_view({"name": "Ya", "business_name": "Otro"})
        self.assertEqual(view["name"], "Ya")

    def test_none(self):
        self.assertEqual(ig.business_doc_view(None), {})


@unittest.skipUnless(REAL_FPDF and HAVE_PYPDF, "fpdf2/pypdf no instalados")
class RawBusinessRenderTest(unittest.TestCase):
    def _text(self, business, invoice_type="recibo"):
        raw = [{"product_name": "Pan", "quantity": 1, "price": "100", "itbis_mode": "included"}]
        items, totals = ig.calc_invoice_totals(raw, 18, invoice_type == "factura")
        meta = {"invoice_type": invoice_type, "ncf": "", "order_ref": "AB12CD34",
                "date": "08/10/2026", "currency": "DOP "}
        with mock.patch.object(ig, "_fetch_logo", return_value=None):
            pdf = ig.build_invoice_pdf(ig.business_doc_view(business), {"name": "Cliente"},
                                       items, totals, meta)
        from pypdf import PdfReader
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)

    def test_pdf_contains_name_phone_and_issuer(self):
        txt = self._text(RAW)
        self.assertIn("Panadería La Esquina", txt)
        self.assertIn("Tel: 809-555-1234", txt)
        self.assertIn("RNC: 101234567", txt)
        self.assertIn("Emitido por: María Elena Peña", txt)
        self.assertIn("Cédula: 001-1234567-8", txt)

    def test_palette_reaches_generator(self):
        seen = {}
        real = ig._InvoicePDF

        def spy(primary, accent, **kw):
            seen["primary"], seen["accent"] = primary, accent
            return real(primary, accent, **kw)

        with mock.patch.object(ig, "_InvoicePDF", side_effect=spy):
            self._text(RAW)
        self.assertEqual(seen["primary"], (170, 0, 0))
        self.assertEqual(seen["accent"], (0, 170, 0))


if __name__ == "__main__":
    unittest.main()
