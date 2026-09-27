# -*- coding: utf-8 -*-
"""Fase 1.1: el ledger es un artefacto de build. Gate de la auditoria 2026-09:
borrar data/ledger.csv y reconstruirlo -> identico al actual."""
import csv

import pytest
from conftest import diff_filas, escribir_ledger, filas, por_id

MANUAL = ("data", "fuentes", "manual.csv")


def preparar(base, correr):
    """Las fuentes existen desde la migracion de la Fase 1; si una copia vieja no
    las tiene, la primera corrida de merge las crea adoptando el ledger."""
    if not base.joinpath(*MANUAL).exists():
        correr("merge_y_validar.py")
    return base


def una_manual(base):
    return next(r for r in filas(base.joinpath(*MANUAL)) if r["tipo"] == "gasto")


@pytest.mark.lento
def test_ledger_se_reconstruye_desde_cero(base, correr):
    preparar(base, correr)
    ledger = base / "data" / "ledger.csv"
    antes = por_id(ledger)
    abiertos = lambda: {(p["id_ledger"], p["motivo"])
                        for p in filas(base / "data" / "pendientes.csv") if not p["resuelto"]}
    pend_antes = abiertos()
    ledger.unlink()
    correr("reconstruir_ledger.py")
    assert diff_filas(antes, por_id(ledger)) == ([], [], {}), \
        "el ledger reconstruido desde las fuentes no es identico al original"
    assert abiertos() == pend_antes, "la reconstruccion cambio la bandeja de pendientes"


def test_primera_corrida_adopta_todo_sin_cambiar_el_ledger(base, correr):
    """La migracion de la Fase 1 es esto: sin data/fuentes/, merge adopta cada
    fila manual/estado_cuenta/splitwise del ledger y no mueve ni una."""
    # Solo las tres fuentes. borrados.csv NO: sin el registro, las bajas vuelven
    # a contar como perdida y el gate salta (medido: asi fallo la 1ra version
    # de este test, con el gate haciendo exactamente lo correcto).
    for origen in ("manual", "estado_cuenta", "splitwise"):
        (base / "data" / "fuentes" / f"{origen}.csv").unlink()
    antes = por_id(base / "data" / "ledger.csv")
    p = correr("merge_y_validar.py")
    assert diff_filas(antes, por_id(base / "data" / "ledger.csv")) == ([], [], {})
    for origen in ("manual", "estado_cuenta", "splitwise"):
        en_ledger = {i for i, r in antes.items() if r["origen"] == origen}
        assert {r["id"] for r in filas(base / "data" / "fuentes" / f"{origen}.csv")} == en_ledger
    assert "adoptada(s)" in p.stdout


@pytest.mark.smoke
def test_fila_borrada_del_ledger_vuelve_desde_la_fuente(base, correr):
    """Lo que antes era perdida silenciosa (SA1) ahora se cura solo."""
    preparar(base, correr)
    ledger = base / "data" / "ledger.csv"
    victima = una_manual(base)
    escribir_ledger(ledger, [r for r in filas(ledger) if r["id"] != victima["id"]])
    p = correr("merge_y_validar.py")
    assert victima["id"] in por_id(ledger)
    assert "RESTAURADA" in p.stdout


@pytest.mark.smoke
def test_fila_editada_en_el_ledger_vuelve_a_su_valor(base, correr):
    """Manda la fuente: las correcciones van por resoluciones.csv."""
    preparar(base, correr)
    ledger = base / "data" / "ledger.csv"
    victima = una_manual(base)
    rows = filas(ledger)
    for r in rows:
        if r["id"] == victima["id"]:
            r["monto"] = "999999.99"
    escribir_ledger(ledger, rows)
    correr("merge_y_validar.py")
    assert por_id(ledger)[victima["id"]]["monto"] == victima["monto"]


def test_fila_nueva_de_un_escritor_se_adopta(base, correr):
    """conciliar_estado/conciliar_debito/sync_splitwise escriben solo el ledger:
    merge adopta la fila en su fuente, asi sobrevive a un ledger perdido."""
    preparar(base, correr)
    ledger = base / "data" / "ledger.csv"
    rows = filas(ledger)
    nueva = dict(next(r for r in rows if r["origen"] == "estado_cuenta"),
                 id="feedfacecafe", fuente_id="estado:test-adopcion", monto="12.34", monto_uyu="12.34")
    escribir_ledger(ledger, rows + [nueva])
    correr("merge_y_validar.py")
    assert "feedfacecafe" in {r["id"] for r in filas(base / "data" / "fuentes" / "estado_cuenta.csv")}
    ledger.unlink()
    correr("merge_y_validar.py", espera_exito=False)       # sin mails: DESVIO, pero...
    assert "feedfacecafe" in por_id(ledger)                  # ...la adoptada sobrevivio


def test_estado_conciliado_sobrevive_a_reprocesar(base, correr):
    """Cierra la brecha del 20/9 (test_BRECHA_reprocesar_pierde_el_estado_conciliado):
    procesar_mails --reprocesar no puede reproducir `conciliado`, pero merge lo
    restaura desde data/fuentes/conciliados.csv."""
    preparar(base, correr)
    ledger = base / "data" / "ledger.csv"
    antes = {i for i, r in por_id(ledger, origen="mail").items() if r["estado"] == "conciliado"}
    assert antes, "precondicion: hay filas mail conciliadas"
    correr("procesar_mails.py", "--reprocesar")
    correr("merge_y_validar.py")
    despues = {i for i, r in por_id(ledger, origen="mail").items() if r["estado"] == "conciliado"}
    assert antes <= despues


@pytest.mark.smoke
def test_borrado_registrado_no_dispara_el_gate(base, correr):
    preparar(base, correr)
    correr("merge_y_validar.py")                             # siembra la referencia
    victima = una_manual(base)
    correr("gastos.py", "borrar", victima["id"], "--motivo", "test", "--aplicar", "--sin-pipeline")
    p = correr("merge_y_validar.py")
    assert "[OK] 1.conteo_origen" in p.stdout
    assert victima["id"] not in por_id(base / "data" / "ledger.csv")
    assert victima["id"] not in {r["id"] for r in filas(base.joinpath(*MANUAL))}
    borr = {r["id"]: r for r in filas(base / "data" / "fuentes" / "borrados.csv")}
    assert borr[victima["id"]]["motivo"] == "test"


def test_mail_borrado_no_vuelve_con_reprocesar(base, correr):
    """Los dos avisos duplicados del pago Proyecto PAS del 1/7 (auditoria SB7):
    filas mail, que --reprocesar regenera. Tienen que quedarse afuera."""
    preparar(base, correr)
    correr("merge_y_validar.py")
    for i in ("cc9d9b3463de", "e2a0018c20eb"):
        if i in por_id(base / "data" / "ledger.csv"):
            correr("gastos.py", "borrar", i, "--motivo", "aviso duplicado", "--aplicar", "--sin-pipeline")
    correr("procesar_mails.py", "--reprocesar")
    p = correr("merge_y_validar.py")
    ids = por_id(base / "data" / "ledger.csv")
    assert "cc9d9b3463de" not in ids and "e2a0018c20eb" not in ids
    assert "[OK] 1.conteo_origen" in p.stdout


def test_gastos_no_borra_filas_de_migracion(base, correr):
    preparar(base, correr)
    una_1money = next(r for r in filas(base / "data" / "ledger.csv") if r["origen"] == "1money")
    p = correr("gastos.py", "borrar", una_1money["id"], "--motivo", "x", "--aplicar",
               espera_exito=False)
    assert p.returncode == 2 and "en_analisis=0" in p.stderr
