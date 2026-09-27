# -*- coding: utf-8 -*-
"""Fase 1.3: planilla "Carga manual". Gate de la auditoria 2026-09: una fila
invalida en la planilla genera un pendiente, no una fila rota. Todo corre desde
un snapshot armado a mano (--offline): los tests no tocan la red."""
import csv
import os
import subprocess
import sys

from conftest import SCRIPTS, filas, por_id

SNAP_COLS = ["pestana", "fila", "fecha", "monto", "moneda", "que", "quien", "ambito",
             "categoria", "cuenta", "nota"]

# (pestana, fila, fecha, monto, moneda, que, quien, ambito, categoria, cuenta, nota)
PLANILLA = [
    ("Gastos", 2, "2026-09-25", "452", "", "propina del auto", "Joaco efectivo", "casa", "Auto", "", ""),
    ("Gastos", 3, "2026-09-24", "364", "UYU", "Uber al centro", "Lu tarjeta", "", "Transporte", "", ""),
    ("Gastos", 4, "", "100", "", "sin fecha", "Joaco efectivo", "personal", "Otros", "", ""),
    ("Gastos", 5, "2026-09-24", "900", "", "ropa de ella", "Lu tarjeta", "personal", "", "", ""),
    ("Gastos", 6, "2026-09-24", "50", "", "chicle", "Joaco efectivo", "personal", "Inventada", "", ""),
    ("Gastos", 7, "2026-09-23", "77", "", "cosa rara xyz", "Joaco efectivo", "personal", "", "", ""),
    ("Aportes", 2, "2026-09-10", "31000", "", "", "Lu", "", "", "", "ya estaba"),
    ("Aportes", 3, "2026-09-20", "5000", "", "", "Lu", "", "", "", "extra"),
    ("Aportes", 4, "2026-09-02", "48000", "", "", "Joaco", "", "", "", "ya estaba"),
    ("Aportes", 5, "2026-09-21", "7000", "", "", "Joaco", "", "", "", ""),
    ("Saldos", 2, "2026-09-27", "2500", "", "", "", "", "", "Efectivo UYU", ""),
    ("Saldos", 3, "2026-09-27", "10", "", "", "", "", "", "Banco X", ""),
]


def escribir_snapshot(base, filas_planilla):
    p = base / "data" / "fuentes" / "carga_manual.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SNAP_COLS)
        w.writerows(filas_planilla)


def pendientes_planilla(base):
    return [p for p in filas(base / "data" / "pendientes.csv")
            if p["detalle"].startswith("[planilla]") and not p["resuelto"]]


def cargar(base, correr, filas_planilla=PLANILLA):
    escribir_snapshot(base, filas_planilla)
    correr("leer_carga_manual.py", "--offline")
    correr("procesar_mails.py")
    correr("merge_y_validar.py")


def test_filas_validas_entran_e_invalidas_quedan_en_la_bandeja(base, correr):
    correr("cierre_casa.py", "2026-09")
    antes = next(r for r in filas(base / "data" / "cierre_casa.csv") if r["mes"] == "2026-09")
    cargar(base, correr)
    L = [r for r in filas(base / "data" / "ledger.csv") if r["origen"] == "carga_manual"]
    por_monto = {r["monto"]: r for r in L}
    assert sorted(por_monto) == ["364.00", "452.00", "7000.00", "77.00"]
    assert (por_monto["452.00"]["cuenta"], por_monto["452.00"]["ambito"]) == ("Efectivo", "casa")
    assert (por_monto["364.00"]["cuenta"], por_monto["364.00"]["ambito"]) == ("Lu tarjeta", "casa")
    assert por_monto["7000.00"]["cuenta_destino"] == "Cuenta Casa"
    assert por_monto["77.00"]["estado"] == "pendiente"         # sin categoria -> bandeja

    motivos = sorted((p["motivo"], p["detalle"].split(":")[0]) for p in pendientes_planilla(base))
    assert motivos == [
        ("carga_manual_duplicado", "[planilla] Aportes fila 2"),
        ("carga_manual_duplicado", "[planilla] Aportes fila 4"),
        ("carga_manual_invalida", "[planilla] Gastos fila 4"),
        ("carga_manual_invalida", "[planilla] Gastos fila 5"),
        ("carga_manual_invalida", "[planilla] Gastos fila 6"),
        ("carga_manual_invalida", "[planilla] Saldos fila 3"),
        ("sin_categoria", "[planilla] Gastos fila 7"),
    ]
    assert [r["monto"] for r in filas(base / "data" / "staging" / "aportes_lu_carga.csv")] == ["5000.00"]
    assert [(r["cuenta"], r["saldo"]) for r in filas(base / "data" / "staging" / "saldos_carga.csv")] \
        == [("Efectivo", "2500.00")]

    # la posicion de casa lo toma: Lu +5.000 (aporte) +364 (lo pago ella);
    # Joaco +7.000 (giro) +452 (lo pago en efectivo)
    correr("cierre_casa.py", "2026-09")
    despues = next(r for r in filas(base / "data" / "cierre_casa.csv") if r["mes"] == "2026-09")
    assert round(float(despues["aportes_lu"]) - float(antes["aportes_lu"]), 2) == 5364.00
    assert round(float(despues["aportes_joaco"]) - float(antes["aportes_joaco"]), 2) == 7452.00


def test_corregir_la_fila_en_la_planilla_se_lleva_el_pendiente(base, correr):
    cargar(base, correr)
    corregida = [list(f) for f in PLANILLA]
    corregida[2][2] = "2026-09-22"                            # Gastos fila 4: ahora con fecha
    cargar(base, correr, [tuple(f) for f in corregida])
    assert not any("Gastos fila 4" in p["detalle"] for p in pendientes_planilla(base))
    assert "100.00" in {r["monto"] for r in filas(base / "data" / "ledger.csv")
                        if r["origen"] == "carga_manual"}


def test_duplicado_confirmado_en_la_revision_entra(base, correr):
    cargar(base, correr)
    dup = next(p for p in pendientes_planilla(base) if "Aportes fila 5" in p["detalle"]
               or "Aportes fila 4" in p["detalle"])
    with open(base / "data" / "resoluciones.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerow(
            [dup["id_ledger"], "motivo_resuelto", "carga_manual_duplicado", "es otro giro", "2026-09-27"])
    cargar(base, correr)
    montos = [r["monto"] for r in filas(base / "data" / "ledger.csv") if r["origen"] == "carga_manual"]
    assert montos.count("48000.00") == 1
    assert not any(p["id_ledger"] == dup["id_ledger"] for p in pendientes_planilla(base))


def test_reordenar_la_planilla_no_cambia_los_ids(base, correr):
    cargar(base, correr)
    ids = {r["id"] for r in filas(base / "data" / "ledger.csv") if r["origen"] == "carga_manual"}
    invertida = [(p, 100 - n, *resto) for (p, n, *resto) in reversed(PLANILLA)]
    cargar(base, correr, invertida)
    assert {r["id"] for r in filas(base / "data" / "ledger.csv") if r["origen"] == "carga_manual"} == ids


def test_planilla_vaciada_de_golpe_no_pisa_el_snapshot(base, correr):
    """Si alguien borra la pestaña entera, se sigue con lo anterior y el paso
    falla (salud_pasos lo escala). Se simula la bajada con una planilla vacia."""
    escribir_snapshot(base, PLANILLA)
    antes = (base / "data" / "fuentes" / "carga_manual.csv").read_bytes()
    codigo = ("import sys; sys.argv=['leer_carga_manual.py']; import leer_carga_manual as m; "
              "m.bajar = lambda: []; sys.exit(m.main())")
    env = dict(os.environ, GASTOS_BASE=str(base), PYTHONIOENCODING="utf-8", PYTHONPATH=str(SCRIPTS))
    p = subprocess.run([sys.executable, "-c", codigo], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 3, p.stdout + p.stderr
    assert "NO se pisa" in p.stderr
    assert (base / "data" / "fuentes" / "carga_manual.csv").read_bytes() == antes


def test_rebuild_incluye_la_planilla(base, correr):
    """La planilla tambien es fuente reproducible: sin red, desde el snapshot."""
    cargar(base, correr)
    antes = por_id(base / "data" / "ledger.csv", origen="carga_manual")
    (base / "data" / "staging" / "ledger_carga_manual.csv").unlink()
    correr("leer_carga_manual.py", "--offline")
    correr("merge_y_validar.py")
    assert por_id(base / "data" / "ledger.csv", origen="carga_manual") == antes
