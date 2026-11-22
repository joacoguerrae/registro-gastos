# -*- coding: utf-8 -*-
"""Dashboard (generar_dashboard_html.py): la parte que calcula, no el dibujo.

Corre el script en subproceso sobre una copia (GASTOS_BASE) con saldos, ledger e
inversiones armados a mano, y lee el JSON embebido en el HTML. Gate de la mejora
del 25/10/2026: antes los saldos arrancaban de una fecha fija (12/7) con semillas
al 31/8 y contaban dos veces los movimientos del medio."""
import csv
import json
import re

import pytest

from conftest import escribir_ledger, filas

pytestmark = pytest.mark.smoke


def escribir(path, cols, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def fila_ledger(cols, **kw):
    r = {c: "" for c in cols}
    r.update(en_analisis="1", estado="confirmado", origen="manual", ambito="personal")
    r.update(kw)
    r["mes"] = r["fecha"][:7]
    return r


def data_de(base, correr):
    correr("generar_dashboard_html.py")
    html = (base / "out" / "dashboard.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert m, "no esta el bloque JSON en el HTML"
    return json.loads(m.group(1))


@pytest.fixture
def escenario(base):
    cols = list(filas(base / "data" / "ledger.csv")[0].keys())
    L = lambda **kw: fila_ledger(cols, **kw)
    escribir_ledger(base / "data" / "ledger.csv", [
        # anterior al saldo real del 30/9: NO tiene que contar
        L(id="a1", fecha="2026-09-15", tipo="gasto", monto="500", moneda="UYU", cuenta="Santander UYU"),
        # posteriores
        L(id="a2", fecha="2026-10-02", tipo="gasto", monto="300", moneda="UYU", cuenta="Santander UYU"),
        L(id="a3", fecha="2026-10-03", tipo="ingreso", monto="1000", moneda="UYU", cuenta="Santander UYU"),
        L(id="a4", fecha="2026-10-04", tipo="transferencia", monto="200", moneda="UYU",
          cuenta="Santander UYU", cuenta_destino="Itaú UYU", en_analisis="0", ambito="interno"),
        # USD en una cuenta UYU: no mezcla monedas
        L(id="a5", fecha="2026-10-05", tipo="gasto", monto="7", moneda="USD", cuenta="Santander UYU"),
        # inversion: giro antes del saldo cargado (no cuenta), uno despues y un retiro
        L(id="i1", fecha="2026-09-20", tipo="transferencia", monto="999", moneda="USD",
          cuenta="Santander USD", cuenta_destino="Etoro", en_analisis="0", ambito="interno"),
        L(id="i2", fecha="2026-10-10", tipo="transferencia", monto="2000", moneda="USD",
          cuenta="Santander USD", cuenta_destino="Etoro", en_analisis="0", ambito="interno"),
        L(id="i3", fecha="2026-10-12", tipo="transferencia", monto="150", moneda="USD",
          cuenta="Etoro", cuenta_destino="Santander USD", en_analisis="0", ambito="interno"),
    ])
    escribir(base / "config" / "saldos_iniciales.csv",
             ["cuenta", "fecha_corte", "saldo", "moneda", "fuente", "nota"],
             [{"cuenta": "Santander UYU", "fecha_corte": "2026-08-31", "saldo": "8000", "moneda": "UYU"},
              {"cuenta": "Itaú UYU", "fecha_corte": "2026-08-31", "saldo": "0", "moneda": "UYU"}])
    escribir(base / "data" / "saldos.csv",
             ["fecha_corte", "cuenta", "moneda", "saldo_calculado", "saldo_real", "diferencia", "comentario"],
             [{"fecha_corte": "2026-09-30", "cuenta": "Santander UYU", "moneda": "UYU",
               "saldo_calculado": "1", "saldo_real": "20000"},
              # sin saldo_real: no puede ser base aunque sea mas nueva
              {"fecha_corte": "2026-10-01", "cuenta": "Santander UYU", "moneda": "UYU",
               "saldo_calculado": "123", "saldo_real": ""}])
    escribir(base / "data" / "staging" / "saldos_carga.csv",
             ["fecha", "cuenta", "moneda", "saldo", "nota", "fila"],
             [{"fecha": "2026-08-31", "cuenta": "Etoro", "moneda": "USD", "saldo": "5000", "fila": "2"},
              {"fecha": "2026-09-30", "cuenta": "Etoro", "moneda": "USD", "saldo": "9324", "fila": "3"}])
    return base


def test_saldo_arranca_del_ultimo_real_de_cada_cuenta(escenario, correr):
    d = data_de(escenario, correr)
    s = {x["cuenta"]: x for x in d["saldos"]}
    # 20000 al 30/9 - 300 + 1000 - 200; el gasto del 15/9 y el de USD no cuentan
    assert s["Santander $"]["saldo"] == 20500
    assert s["Santander $"]["base_fecha"] == "2026-09-30"
    # semilla 0 al 31/8 + transferencia recibida
    assert s["Itaú $"]["saldo"] == 200
    assert s["Itaú $"]["base_fecha"] == "2026-08-31"
    assert {x["nivel"] for x in d["saldos"]} == {"principal", "secundaria"}


def test_inversion_saldo_planilla_mas_giros_posteriores(escenario, correr):
    d = data_de(escenario, correr)
    inv = {x["cuenta"]: x for x in d["inversiones"]}
    assert inv["eToro"]["base_fecha"] == "2026-09-30"
    assert inv["eToro"]["aportes_desde"] == 1850          # +2000 - 150; el de 999 es previo
    assert inv["eToro"]["saldo"] == 9324 + 1850
    assert inv["Interactive Brokers"]["saldo"] is None     # sin carga todavia


def test_tipo_por_categoria_y_logos(escenario, correr):
    d = data_de(escenario, correr)
    t = d["cat_tipo"]
    assert t["Vivienda"] == "Fijo" and t["Super"] == "Variable"
    assert set(t.values()) <= {"Fijo", "Variable"}         # las que no estan caen en Variable
    assert set(t) == set(d["categorias"])
    logos = {x["cuenta"]: x["logo"] for x in d["saldos"] + d["inversiones"]}
    assert logos["Itaú $"].startswith("data:image/svg+xml;base64,")
    assert logos["Interactive Brokers"].startswith("data:image/")
