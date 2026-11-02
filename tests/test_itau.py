# -*- coding: utf-8 -*-
"""Avisos de Itau (desde oct-2026): los dos layouts reales y el ruteo.

Los cuerpos son los body_text que arma fetch_mails.py (recortados al principio:
el resto es el disclaimer). Puro parseo: no corre el pipeline ni toca archivos."""
import procesar_mails as pm

CREDITO = ("Aviso de consumo aprobado con tarjeta de crédito\nSe aprobó un consumo con su "
           "tarjeta de crédito\nDetalles:\nVISA nro. *** 5023\nImporte: 3451.0 UYU\n"
           "Comercio: EBN*SHEIN MONTE\nAdemás ingresando con tu documento y contraseña")
DEBITO = ("Aviso de consumo aprobado con tarjeta de débito\nSe aprobo un consumo de su tarjeta "
          "Visa terminada en 6136 . Realizado en DELIVIANDA*HANDY*0PJHVIS3 Monto: 2,962.00 "
          "Pesos. Si no fuiste tú comunícate al 1784.")


def test_credito():
    t, mon, monto, est, fh = pm.parse_aviso_itau(
        {"body_text": CREDITO, "fecha_mail": "2026-10-16T19:55:14Z"})
    assert (t, mon, monto, est) == ("5023", "UYU", 3451.0, "EBN*SHEIN MONTE")
    assert fh.isoformat() == "2026-10-16T16:55:14"        # hora de Uruguay


def test_debito_formato_us_y_fecha_uy():
    # 02:00Z del 19 = 23:00 del 18 en Uruguay: el gasto es del 18
    t, mon, monto, est, fh = pm.parse_aviso_itau(
        {"body_text": DEBITO, "fecha_mail": "2026-10-19T02:00:00Z"})
    assert (t, mon, monto, est) == ("6136", "UYU", 2962.0, "DELIVIANDA*HANDY*0PJHVIS3")
    assert fh.date().isoformat() == "2026-10-18"


def test_debito_en_dolares():
    body = DEBITO.replace("2,962.00 Pesos", "12.99 Dólares")
    assert pm.parse_aviso_itau({"body_text": body, "fecha_mail": "2026-10-18T12:00:00Z"})[1:3] \
        == ("USD", 12.99)


def test_reembolso_usa_el_layout_de_credito():
    body = ("Aviso de consumo reembolsado con tarjeta de crédito\nSe reversó un consumo realizado "
            "con su tarjeta de crédito\nDetalles:\nVISA nro. *** 5023\nImporte: 1.0 USD\n"
            "Comercio: PAYPAL 40293\nAdemás ingresando")
    assert pm.parse_aviso_itau({"body_text": body, "fecha_mail": "2026-10-20T13:17:20Z"})[:4] \
        == ("5023", "USD", 1.0, "PAYPAL 40293")


def test_transferencia_realizada():
    body = ("Aviso de transferencia realizada\nTransferencia realizada.\nDetalles:\n"
            "Transferencia realizada desde la cuenta **** 4405\nImporte: 590.00 UYU\n"
            "Cuenta destino: 8155077\nBanco/Institución destino: OCA Blue\nAdemás ingresando")
    assert pm.parse_transferencia_itau(body) == ("4405", "UYU", 590.0, "8155077", "OCA Blue")


def test_transferencia_realizada_urgp():
    # 30/10/2026: Itau escribio los pesos como 'URGP' y los dos avisos cayeron
    # como formato_nuevo. URGP = UYU.
    body = ("Aviso de transferencia realizada\nTransferencia realizada.\nDetalles:\n"
            "Transferencia realizada desde la cuenta **** 4405\nImporte: 4905.00 URGP\n"
            "Cuenta destino: 1409211\nBanco/Institución destino: Itau\nAdemás ingresando")
    assert pm.parse_transferencia_itau(body) == ("4405", "UYU", 4905.0, "1409211", "Itau")


def test_pago_realizado():
    body = ("Pago realizado.\nTu pago fue realizado con éxito. Siguen los detalles del mismo:\n"
            "Cuenta origen terminada en ... 4405 .\nMonto: $ 300.00 .\nPago a SISTARBANC .\n"
            "Si no fuiste tu comunícate al 1784.")
    assert pm.parse_pago_itau(body) == ("4405", "UYU", 300.0, "SISTARBANC")


def test_layout_desconocido_lanza():
    try:
        pm.parse_aviso_itau({"body_text": "Aviso de consumo aprobado\nalgo nuevo",
                             "fecha_mail": "2026-10-18T12:00:00Z"})
    except ValueError:
        return
    raise AssertionError("un layout nuevo tiene que lanzar (el pipeline lo deja como formato_nuevo)")
