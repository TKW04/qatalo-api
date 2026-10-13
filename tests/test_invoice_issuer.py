"""Emisor de facturas informales (recibo sin NCF): validación en business.py y
líneas "Emitido por" / "Cédula|RNC" en el PDF.

Ejecutar:  python3 -m pytest -q tests/test_invoice_issuer.py
Los tests de render real se saltan si fpdf2/pypdf no están instalados.
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import invoice_generator as ig  # noqa: E402
import business  # noqa: E402

REAL_FPDF = ig.FPDF is not object
try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False


def _doc(invoice_type, **biz):
    raw = [{"product_name": "Pan", "quantity": 1, "price": "100", "itbis_mode": "included"}]
    with_ncf = invoice_type == "factura"
    items, totals = ig.calc_invoice_totals(raw, 18, with_ncf)
    meta = {"invoice_type": invoice_type, "ncf": "B0100000001" if with_ncf else "",
            "order_ref": "AB12CD34", "date": "08/10/2026", "currency": "DOP "}
    return biz, {"name": "Cliente"}, items, totals, meta


@unittest.skipUnless(REAL_FPDF and HAVE_PYPDF, "fpdf2/pypdf no instalados")
class IssuerRenderTest(unittest.TestCase):
    def _text(self, invoice_type, **biz):
        with mock.patch.object(ig, "_fetch_logo", return_value=None):
            pdf = ig.build_invoice_pdf(*_doc(invoice_type, **biz))
        from pypdf import PdfReader
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)

    # Item crudo de DynamoDB (así llega desde customers.emit_invoice)
    ISSUER = {"issuer_name": "María Elena Peña", "issuer_id": "00112345678", "issuer_id_type": "cedula"}

    def test_recibo_prints_issuer(self):
        txt = self._text("recibo", **self.ISSUER)
        self.assertIn("Emitido por: María Elena Peña", txt)
        self.assertIn("Cédula: 001-1234567-8", txt)

    def test_recibo_camelcase_and_rnc(self):
        txt = self._text("recibo", issuerName="Ana", issuerId="1-01-23456-7", issuerIdType="rnc")
        self.assertIn("Emitido por: Ana", txt)
        self.assertIn("RNC: 1-01-23456-7", txt)
        self.assertNotIn("Cédula", txt)

    def test_recibo_omits_empty_line(self):
        txt = self._text("recibo", issuer_name="", issuer_id="00112345678", issuer_id_type="cedula")
        self.assertNotIn("Emitido por", txt)
        self.assertIn("Cédula: 001-1234567-8", txt)

    def test_factura_does_not_print_issuer(self):
        txt = self._text("factura", **self.ISSUER)
        self.assertNotIn("Emitido por", txt)
        self.assertNotIn("Cédula", txt)
        self.assertNotIn("001-1234567-8", txt)

    def test_recibo_without_issuer(self):
        txt = self._text("recibo", name="Tienda")
        self.assertNotIn("Emitido por", txt)
        self.assertNotIn("Cédula", txt)


class IssuerLinesTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(ig._issuer_lines({}), [])
        self.assertEqual(
            ig._issuer_lines({"issuer_name": " Juan ", "issuer_id": "00112345678"}),
            ["Emitido por: Juan", "Cédula: 001-1234567-8"],
        )
        self.assertEqual(ig.format_cedula("001-1234567-8"), "001-1234567-8")


class IssuerValidationTest(unittest.TestCase):
    def test_cedula_10_digits_fails(self):
        out, err = business._clean_issuer({"issuerIdType": "cedula", "issuerId": "0011234567"})
        self.assertIsNone(out)
        self.assertIn("11 dígitos", err)

    def test_cedula_with_dashes_passes_and_stores_digits(self):
        out, err = business._clean_issuer({"issuerName": "  María  ", "issuerId": "001-1234567-8"})
        self.assertIsNone(err)
        self.assertEqual(out, {"issuer_name": "María", "issuer_id": "00112345678",
                               "issuer_id_type": "cedula"})

    def test_rnc_not_validated(self):
        out, err = business._clean_issuer({"issuerIdType": "rnc", "issuerId": " 1-01-2 "})
        self.assertIsNone(err)
        self.assertEqual(out["issuer_id"], "1-01-2")
        self.assertEqual(out["issuer_id_type"], "rnc")

    def test_invalid_type_and_lengths(self):
        self.assertIsNotNone(business._clean_issuer({"issuerIdType": "pasaporte"})[1])
        self.assertIsNotNone(business._clean_issuer({"issuerName": "x" * 121})[1])
        self.assertIsNotNone(business._clean_issuer({"issuerIdType": "rnc", "issuerId": "1" * 21})[1])

    def test_defaults_when_missing(self):
        out, err = business._clean_issuer({})
        self.assertIsNone(err)
        self.assertEqual(out, {"issuer_name": "", "issuer_id": "", "issuer_id_type": "cedula"})

    def _update(self, body):
        table = mock.MagicMock()
        table.get_item.return_value = {"Item": {"business_id": "b1", "user_id": "u1"}}
        with mock.patch.object(business, "business_table", table):
            res = business.update_business({"body": json.dumps(body)}, "u1", "b1")
        return res, table

    def test_update_rejects_bad_cedula_with_400(self):
        res, table = self._update({"name": "N", "issuerId": "123"})
        self.assertEqual(res["statusCode"], 400)
        table.update_item.assert_not_called()

    def test_update_sets_issuer_only_when_sent(self):
        res, table = self._update({"name": "N", "issuerId": "001-1234567-8"})
        self.assertEqual(res["statusCode"], 200)
        kw = table.update_item.call_args.kwargs
        self.assertIn("issuer_id=:isi", kw["UpdateExpression"])
        self.assertEqual(kw["ExpressionAttributeValues"][":isi"], "00112345678")

        res, table = self._update({"name": "N"})
        self.assertEqual(res["statusCode"], 200)
        kw = table.update_item.call_args.kwargs
        self.assertNotIn("issuer", kw["UpdateExpression"])


if __name__ == "__main__":
    unittest.main()
