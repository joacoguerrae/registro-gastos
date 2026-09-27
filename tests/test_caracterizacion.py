# -*- coding: utf-8 -*-
"""Red de caracterizacion: el pipeline, corrido sobre los datos reales, tiene
que reproducir lo que ya esta en el ledger. Sin marker `smoke` a proposito: un
cambio legitimo de reglas_comercio.csv o splits_recurrentes.csv los rompe y en
sync.yml eso frenaria el commit de datos 2x/dia."""
import pytest
from conftest import diff_filas, filas, por_id



def test_merge_es_idempotente(base, correr):
    antes = por_id(base / "data" / "ledger.csv")
    correr("merge_y_validar.py")
    assert diff_filas(antes, por_id(base / "data" / "ledger.csv")) == ([], [], {})


def test_reprocesar_mails_reproduce_las_filas(base, correr):
    """Entero, `estado` incluido. Hasta la Fase 1 se ignoraba `estado`: la marca
    `conciliado` la escriben los conciliadores y --reprocesar la perdia (9 de
    284 filas el 19/9). Desde la 1.1, merge() la restaura desde
    data/fuentes/conciliados.csv, asi que el ciclo reprocesar + merge tiene que
    devolver las filas identicas."""
    antes = por_id(base / "data" / "ledger.csv", origen="mail")
    correr("procesar_mails.py", "--reprocesar")
    correr("merge_y_validar.py")
    despues = por_id(base / "data" / "ledger.csv", origen="mail")
    solo_a, solo_b, campos = diff_filas(antes, despues)
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
