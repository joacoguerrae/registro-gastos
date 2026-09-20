# -*- coding: utf-8 -*-
"""Red de caracterizacion: el pipeline, corrido sobre los datos reales, tiene
que reproducir lo que ya esta en el ledger. Sin marker `smoke` a proposito: un
cambio legitimo de reglas_comercio.csv o splits_recurrentes.csv los rompe y en
sync.yml eso frenaria el commit de datos 2x/dia."""
import pytest
from conftest import diff_filas, filas, por_id

# `estado` lo escriben los CONCILIADORES (conciliado) y la bandeja (pendiente),
# no el parser de mails: --reprocesar no puede reproducirlo. Verificado el 19/9
# sobre 4.959 filas: es la UNICA diferencia, en 9 de 284 filas mail.
NO_REPRODUCIBLE_POR_MAILS = ("estado",)


def test_merge_es_idempotente(base, correr):
    antes = por_id(base / "data" / "ledger.csv")
    correr("merge_y_validar.py")
    assert diff_filas(antes, por_id(base / "data" / "ledger.csv")) == ([], [], {})


def test_reprocesar_mails_reproduce_las_filas(base, correr):
    antes = por_id(base / "data" / "ledger.csv", origen="mail")
    correr("procesar_mails.py", "--reprocesar")
    despues = por_id(base / "data" / "ledger.csv", origen="mail")
    solo_a, solo_b, campos = diff_filas(antes, despues, ignorar=NO_REPRODUCIBLE_POR_MAILS)
    assert not solo_a, f"--reprocesar PERDIO filas mail: {solo_a[:10]}"
    assert not solo_b, f"--reprocesar INVENTO filas mail: {solo_b[:10]}"
    assert not campos, f"campos que cambiaron: {({c: v[:3] for c, v in campos.items()})}"


def test_reprocesar_no_toca_los_otros_origenes(base, correr):
    sel = lambda: {i: r for i, r in por_id(base / "data" / "ledger.csv").items()
                   if r["origen"] != "mail"}
    antes = sel()
    correr("procesar_mails.py", "--reprocesar")
    assert diff_filas(antes, sel()) == ([], [], {})


def test_reprocesar_no_pierde_pendientes_abiertos(base, correr):
    abiertos = lambda: {(p["id_ledger"], p["motivo"])
                        for p in filas(base / "data" / "pendientes.csv") if not p["resuelto"]}
    antes = abiertos()
    correr("procesar_mails.py", "--reprocesar")
    assert antes - abiertos() == set()


@pytest.mark.brecha
def test_BRECHA_reprocesar_pierde_el_estado_conciliado(base, correr):
    """DOCUMENTA el agujero, no lo bendice: estado=conciliado vive SOLO dentro de
    ledger.csv (resoluciones.csv no tiene ningun campo=estado). Hoy --reprocesar
    borra 8 conciliaciones de agosto. Cuando la fase 1.1 lo cierre este test
    falla: hay que BORRARLO y sacar 'estado' de NO_REPRODUCIBLE_POR_MAILS."""
    antes = por_id(base / "data" / "ledger.csv", origen="mail")
    correr("procesar_mails.py", "--reprocesar")
    despues = por_id(base / "data" / "ledger.csv", origen="mail")
    perdidos = [i for i in antes
                if antes[i]["estado"] == "conciliado" and despues[i]["estado"] != "conciliado"]
    assert perdidos, "ya no se pierde estado=conciliado -> brecha cerrada, borrar este test"


def test_splitwise_export_es_idempotente(base, correr):
    antes = por_id(base / "data" / "ledger.csv")
    sw_antes = filas(base / "data" / "splitwise.csv")
    p = correr("sync_splitwise.py", "--fuente", "export")
    assert "FUENTE: EXPORT" in p.stdout
    assert diff_filas(antes, por_id(base / "data" / "ledger.csv")) == ([], [], {})
    assert filas(base / "data" / "splitwise.csv") == sw_antes


def test_conciliar_debito_dry_run_no_escribe(base, correr):
    """Ademas de no escribir, congela el 'antes' de SA3: el extracto trae UNA
    linea (DEVOTO EXPRESS 275,41) donde el ledger tiene el split en dos filas
    (46,25 + 229,16), y el Movistar 875,41 aparece dos veces en sin_respaldo.
    Cuando la fase 2.1 agrupe por familia de split, esta asercion cambia."""
    antes = {p.name: p.read_bytes() for p in (base / "data").glob("*.csv")}
    p = correr("conciliar_debito.py", "Movimientos_0726_cajaAhorro.csv")
    assert {q.name: q.read_bytes() for q in (base / "data").glob("*.csv")} == antes
    assert "dry-run: no se escribio nada" in p.stdout
    assert "1 filas nuevas propuestas" in p.stdout      # el total del split, hoy propuesto como nuevo
