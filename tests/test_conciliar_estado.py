# -*- coding: utf-8 -*-
"""Motor de matching de conciliar_estado.py contra un estado SINTETICO armado
con filas reales del ledger. Migrado de scripts/test_conciliacion.py (fase 0.1).

No necesita secretos ni PDF: conciliar() es puro y no pasa por extraer_estado()
(que si pide ci_pdf_password de config/secrets.local.json).
"""
import copy
import importlib

import pytest

pytestmark = pytest.mark.smoke

CUENTA = "Santander Platinum"
PERIODO = "2026-07"
TC_REAL_USD = 41.10          # != al tc estimado de config/parametros.json, a proposito


@pytest.fixture(scope="module")
def ce(base_congelada):
    """conciliar_estado con BASE apuntando a la copia congelada.

    El modulo calcula BASE en tiempo de IMPORT: hay que setear GASTOS_BASE y
    recien ahi importarlo/recargarlo. scope=module + copia congelada porque este
    test solo LEE (deepcopia el ledger antes de tocarlo). El sys.path de scripts/
    lo pone conftest a nivel de modulo: si lo deshiciera mp.undo(), el reload de
    teardown tiraria ModuleNotFoundError.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("GASTOS_BASE", str(base_congelada))
    import conciliar_estado
    importlib.reload(conciliar_estado)
    assert conciliar_estado.LEDGER.startswith(str(base_congelada))
    yield conciliar_estado
    mp.undo()
    importlib.reload(conciliar_estado)


@pytest.fixture(scope="module")
def caso(ce):
    ledger_disco = ce.leer_csv(ce.LEDGER, ce.COLUMNS)
    reales = sorted([r for r in ledger_disco
                     if r["cuenta"] == CUENTA and r["mes"] == PERIODO and r["tipo"] == "gasto"],
                    key=lambda r: (r["fecha"], r["id"]))
    # Minimo, no exacto: antes exigia 6 y el test se rompia solo cada vez que
    # entraba un gasto nuevo al mes.
    assert len(reales) >= 6, f"hacen falta >=6 filas de gasto de {CUENTA} {PERIODO}, hay {len(reales)}"
    reales = reales[:6]
    ids = {r["id"] for r in reales}
    # El motor elige candidatas por la VENTANA del estado (de cierre a cierre,
    # cruza meses), no por mes calendario: hay que sacar TODAS las demas filas de
    # gasto de la cuenta o se cuelan como sin_respaldo.
    ledger_test = [r for r in ledger_disco
                   if not (r["cuenta"] == CUENTA and r["tipo"] == "gasto" and r["id"] not in ids)]

    ordenadas = sorted(reales, key=lambda r: r["fecha"])
    en_estado, afuera = ordenadas[:5], ordenadas[5]
    lineas = []
    for r in en_estado:
        l = dict(fecha=r["fecha"], descripcion=r["comercio"] or r["descripcion"],
                 monto=float(r["monto"]), moneda=r["moneda"])
        if r["moneda"] != "UYU":
            l["monto_uyu"] = round(l["monto"] * TC_REAL_USD, 2)
        lineas.append(l)
    inventadas = [
        dict(fecha="2026-07-15", descripcion="DEBITO AUTOMATICO SEGURO HOGAR",
             monto=450.00, moneda="UYU"),
        dict(fecha="2026-07-22", descripcion="COMISION MANTENIMIENTO ANUAL",
             monto=150.00, moneda="UYU"),
    ]
    lineas += inventadas
    total = {}
    for m in ("UYU", "USD", "EUR"):
        s = round(sum(l["monto"] for l in lineas if l["moneda"] == m), 2)
        if s:
            total[m] = s
    estado = dict(emisor="SANTANDER_MC_SINTETICO_TEST", cuenta=CUENTA, periodo=PERIODO,
                  vencimiento="2026-08-05", lineas=lineas, total=total)

    ledger_copia = copy.deepcopy(ledger_test)   # conciliar() muta las filas in place
    resultado = ce.conciliar(estado, ledger_copia, ce.cargar_reglas())
    return dict(ce=ce, resultado=resultado, ledger=ledger_copia, ledger_disco=ledger_disco,
                reales=reales, afuera=afuera, ids_en_estado=ids - {afuera["id"]},
                inventadas=inventadas)


def test_matches_quedan_conciliados(caso):
    assert {f["id"] for _l, f, _s in caso["resultado"]["matches"]} == caso["ids_en_estado"]
    for r in caso["ledger"]:
        if r["id"] in caso["ids_en_estado"]:
            assert r["estado"] == "conciliado", r["id"]


def test_lineas_no_vistas_se_insertan_como_estado_cuenta(caso):
    nuevas = caso["resultado"]["nuevas"]
    assert len(nuevas) == len(caso["inventadas"])
    assert all(r["origen"] == "estado_cuenta" for r in nuevas)


def test_fila_sin_linea_genera_sin_respaldo_estado(caso):
    ce, afuera = caso["ce"], caso["afuera"]
    assert {r["id"] for r in caso["resultado"]["sin_respaldo"]} == {afuera["id"]}
    pend = ce.pendientes_de_resultado(caso["resultado"], CUENTA, PERIODO, "2026-07-12")
    assert "sin_respaldo_estado" in [p["motivo"] for p in pend
                                     if p["id_ledger"] == afuera["id"]]


def test_cierre_al_peso_por_moneda(caso):
    for moneda, c in caso["resultado"]["cierre"].items():
        assert abs(c["diferencia"]) <= 0.01, (moneda, c)


def test_corrige_el_tc_de_las_lineas_en_moneda_extranjera(caso):
    ids_usd = {r["id"] for r in caso["reales"] if r["moneda"] == "USD"}
    assert {t[0] for t in caso["resultado"]["correcciones_tc"]} == (ids_usd & caso["ids_en_estado"])


def test_no_escribe_el_ledger_en_disco(caso):
    ce = caso["ce"]
    assert ce.leer_csv(ce.LEDGER, ce.COLUMNS) == caso["ledger_disco"]
