"""Factura/recibo de órdenes aún no pagadas ("Pendiente de pago" / "Pendiente de validación").

Ejecutar:  python3 -m pytest -q tests/test_invoice_unpaid.py
"""
import importlib
import io
import json
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_offers_api  # noqa: E402,F401  (registra los stubs de dependencias de la Layer)
import invoice_generator as ig  # noqa: E402
import customers  # noqa: E402

QATOLO = os.path.abspath(os.path.join(HERE, "..", "QATOLO"))


def _doc(invoice_type, status):
    raw = [{"product_name": "Pan", "quantity": 1, "price": "100", "itbis_mode": "included"}]
    with_ncf = invoice_type == "factura"
    items, totals = ig.calc_invoice_totals(raw, 18, with_ncf)
    meta = {"invoice_type": invoice_type, "ncf": "B0100000001" if with_ncf else "",
            "order_ref": "AB12CD34", "date": "08/10/2026", "currency": "DOP ",
            "status": status}
    return {}, {"name": "Cliente"}, items, totals, meta


def _pdf_text(invoice_type, status):
    from pypdf import PdfReader
    with mock.patch.object(ig, "_fetch_logo", return_value=None):
        pdf = ig.build_invoice_pdf(*_doc(invoice_type, status))
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


class StatusRuleTest(unittest.TestCase):
    def test_invoiceable(self):
        for s in ("Pendiente de pago", "Pendiente de validación", "Aprobada", "Entregada"):
            self.assertTrue(ig.is_invoiceable(s), s)
        for s in ("Cancelada", "", None, "Otro"):
            self.assertFalse(ig.is_invoiceable(s), s)

    def test_unpaid(self):
        self.assertTrue(ig.is_unpaid_status("Pendiente de pago"))
        self.assertTrue(ig.is_unpaid_status("Pendiente de validación"))
        for s in ("Aprobada", "Entregada", "Cancelada", "", None):
            self.assertFalse(ig.is_unpaid_status(s), s)


class UnpaidPdfTest(unittest.TestCase):
    def test_unpaid_recibo_is_factura_informal(self):
        for status in ("Pendiente de pago", "Pendiente de validación"):
            txt = _pdf_text("recibo", status)
            self.assertIn("FACTURA INFORMAL", txt)
            self.assertIn("PENDIENTE DE PAGO", txt)
            self.assertIn("Estado: Por cobrar", txt)
            self.assertIn("Documento sin valor fiscal. Pendiente de pago.", txt)
            self.assertNotIn("RECIBO DE PAGO", txt)
            self.assertNotIn("recibo de pago sin valor fiscal", txt)
            self.assertNotIn(f"Estado: {status}", txt)

    def test_unpaid_factura_keeps_ncf(self):
        txt = _pdf_text("factura", "Pendiente de pago")
        self.assertIn("FACTURA", txt)
        self.assertNotIn("FACTURA INFORMAL", txt)
        self.assertIn("NCF: B0100000001", txt)
        self.assertIn("PENDIENTE DE PAGO", txt)
        self.assertIn("Estado: Por cobrar", txt)

    def test_paid_unchanged(self):
        txt = _pdf_text("recibo", "Aprobada")
        self.assertIn("RECIBO DE PAGO", txt)
        self.assertIn("Estado: Aprobada", txt)
        self.assertIn("recibo de pago sin valor fiscal", txt)
        self.assertNotIn("PENDIENTE DE PAGO", txt)
        self.assertNotIn("FACTURA INFORMAL", txt)

        txt = _pdf_text("factura", "Entregada")
        self.assertIn("FACTURA", txt)
        self.assertIn("NCF: B0100000001", txt)
        self.assertIn("Estado: Entregada", txt)
        self.assertNotIn("PENDIENTE DE PAGO", txt)
        self.assertNotIn("Por cobrar", txt)


def _load_real_mails():
    """Importa el SendMails.mails real (test_offers_api lo sustituye por un stub)."""
    saved = {k: sys.modules.pop(k) for k in ("SendMails", "SendMails.mails", "mailersend") if k in sys.modules}
    mailersend = types.ModuleType("mailersend")
    mailersend.emails = mock.MagicMock()
    sys.modules["mailersend"] = mailersend
    try:
        mod = importlib.import_module("SendMails.mails")
    finally:
        for k in ("SendMails", "SendMails.mails", "mailersend"):
            sys.modules.pop(k, None)
        sys.modules.update(saved)
    from jinja2 import Environment, FileSystemLoader
    mod.env = Environment(loader=FileSystemLoader(os.path.join(QATOLO, "templates")))
    return mod


class InvoiceEmailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mails = _load_real_mails()

    def _send(self, invoice_type, unpaid):
        sent = []
        client = mock.MagicMock()
        client.send.side_effect = lambda body: sent.append(body)
        with mock.patch.object(self.mails.emails, "NewEmail", return_value=client):
            self.mails.invoice_email("c@x.com", "Ana", {"name": "Panadería"}, invoice_type,
                                     "AB12CD34", b"%PDF", "recibo", unpaid=unpaid)
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_unpaid_recibo(self):
        body = self._send("recibo", True)
        self.assertEqual(body["subject"], "Factura informal de tu pedido en Panadería — #AB12CD34")
        self.assertIn("Te enviamos el detalle de tu pedido, pendiente de pago", body["html"])
        self.assertNotIn("Gracias por tu compra", body["html"])
        self.assertNotIn("Recibo de Pago", body["html"])

    def test_unpaid_factura(self):
        body = self._send("factura", True)
        self.assertEqual(body["subject"], "Factura de tu pedido en Panadería — #AB12CD34")
        self.assertIn("pendiente de pago", body["html"])
        self.assertIn("valor fiscal", body["html"])

    def test_paid_unchanged(self):
        body = self._send("recibo", False)
        self.assertEqual(body["subject"], "Recibo de pago de tu compra en Panadería — #AB12CD34")
        self.assertIn("Gracias por tu compra", body["html"])
        self.assertNotIn("pendiente de pago", body["html"])
        body = self._send("factura", False)
        self.assertEqual(body["subject"], "Factura de tu compra en Panadería — #AB12CD34")


class EmitInvoiceTest(unittest.TestCase):
    """emit_invoice: estados permitidos, bandera unpaid y NCF consumido solo tras subir el PDF."""

    def _run(self, status, with_ncf=False, action="download", s3_fail=False):
        customer = {"customer_id": "c1", "business_id": "b1", "full_name": "Ana", "email": "a@x.com",
                    "transactions": [{"order_group": "g1", "transaction_id": "t1", "status": status,
                                      "product_name": "Pan", "quantity": 1, "price": 100}]}
        business = {"business_id": "b1", "name": "Panadería", "ncf_enabled": True,
                    "ncf_pool": [{"ncf": "B0100000001", "used": False}]}
        cust_t, biz_t, prod_t, s3 = (mock.MagicMock() for _ in range(4))
        cust_t.get_item.return_value = {"Item": customer}
        prod_t.get_item.return_value = {}
        s3.generate_presigned_url.return_value = "https://u"
        if s3_fail:
            s3.put_object.side_effect = RuntimeError("s3 down")
        mail = mock.MagicMock()
        event = {"body": json.dumps({"order_group": "g1", "customer_id": "c1",
                                     "with_ncf": with_ncf, "action": action})}
        with mock.patch.multiple(customers, customers_table=cust_t, business_table=biz_t,
                                 products_table=prod_t, s3=s3, invoice_email=mail,
                                 build_invoice_pdf=mock.MagicMock(return_value=b"%PDF"),
                                 _get_business=mock.MagicMock(return_value=business)):
            resp = customers.emit_invoice(event, "u", "u")
        return resp, biz_t, cust_t, mail

    def test_cancelada_rejected(self):
        resp, *_ = self._run("Cancelada")
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("Cancelada", json.loads(resp["body"])["message"])

    def test_pending_allowed(self):
        for status in ("Pendiente de pago", "Pendiente de validación"):
            resp, *_ = self._run(status)
            self.assertEqual(resp["statusCode"], 200, status)

    def test_unpaid_flag_to_email(self):
        resp, _, _, mail = self._run("Pendiente de pago", action="email")
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(mail.call_args.kwargs["unpaid"])
        resp, _, _, mail = self._run("Aprobada", action="email")
        self.assertFalse(mail.call_args.kwargs["unpaid"])

    def test_ncf_committed_after_upload(self):
        resp, biz_t, cust_t, _ = self._run("Pendiente de pago", with_ncf=True)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["ncf"], "B0100000001")
        biz_t.update_item.assert_called_once()
        cust_t.update_item.assert_called_once()

    def test_ncf_not_consumed_if_upload_fails(self):
        resp, biz_t, cust_t, _ = self._run("Pendiente de pago", with_ncf=True, s3_fail=True)
        self.assertEqual(resp["statusCode"], 500)
        biz_t.update_item.assert_not_called()
        cust_t.update_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
