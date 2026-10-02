# -*- coding: utf-8 -*-
"""Fase 1.2: scripts/gastos.py. Gate de la auditoria 2026-09: recrear con la
herramienta tres casos reales que se habian cargado con Python descartable
-- split Pantagruel, los tres gastos de Lu de agosto, la compensacion del
Dilema -- y obtener las mismas filas y las mismas posiciones de casa."""
import csv

import pytest
from conftest import escribir_ledger, filas, por_id

# Lo que define a una fila. Afuera: id/fuente_id (el esquema de nombres de
# gastos.py no es el del Python descartable) y los textos libres.
CONTENIDO = ("fecha", "mes", "tipo", "monto", "moneda", "monto_uyu", "cuenta",
             "cuenta_destino", "categoria", "ambito", "origen", "en_analisis")


def preparar(base, correr):
    if not (base / "data" / "fuentes" / "manual.csv").exists():
        correr("merge_y_validar.py")


def quitar(base, ids):
    """Deshace filas manual como si nunca se hubieran cargado."""
    ids = set(ids)
    viejas = [r for r in filas(base / "data" / "fuentes" / "manual.csv") if r["id"] in ids]
    assert len(viejas) == len(ids), f"precondicion: {ids} son filas manual"
    for rel in (("data", "fuentes", "manual.csv"), ("data", "ledger.csv")):
        p = base.joinpath(*rel)
        escribir_ledger(p, [r for r in filas(p) if r["id"] not in ids])
    # El escenario es "estas filas nunca se cargaron": la referencia del gate
    # anti-perdida tiene que olvidarlas tambien, o (con razon) marca la baja.
    (base / "data" / "staging" / "ledger_stats.json").unlink(missing_ok=True)
    return viejas


def quitar_resoluciones(base, pred):
    p = base / "data" / "resoluciones.csv"
    rows = filas(p)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(r for r in rows if not pred(r))


def contenido(r):
    return {c: r[c] for c in CONTENIDO}


def nuevas_manual(base, antes_ids):
    return [r for r in filas(base / "data" / "fuentes" / "manual.csv") if r["id"] not in antes_ids]


def posicion(base, mes):
    fila = next(r for r in filas(base / "data" / "cierre_casa.csv") if r["mes"] == mes)
    return fila["posicion_lu"], fila["posicion_joaco"]


# ------------------------------------------------------------------ gate: 3 casos reales
def test_split_pantagruel(base, correr):
    """21/8: Pantagruel 4.300, 1/3 propio + 2.866,67 a Deudas (sw:4643398938)."""
    preparar(base, correr)
    orig, herm = "3f5724944a85", "60d81d39a44c"
    esperado_herm = contenido(por_id(base / "data" / "ledger.csv")[herm])
    quitar(base, [herm])
    quitar_resoluciones(base, lambda r: r["id_ledger"] == orig and (
        r["campo"] in ("monto", "monto_uyu") or r["valor"] == "split_a_cargar"))
    correr("procesar_mails.py", "--reprocesar")              # la fila vuelve a 4.300
    correr("merge_y_validar.py")
    assert por_id(base / "data" / "ledger.csv")[orig]["monto"] == "4300.00"

    correr("gastos.py", "split", orig, "--mia", "1/3", "--nota", "sw:4643398938", "--aplicar")
    L = por_id(base / "data" / "ledger.csv")
    assert (L[orig]["monto"], L[orig]["monto_uyu"]) == ("1433.33", "1433.33")
    assert herm in L, "la hermana tiene que salir con el MISMO id (sha1('manual'+'<id>-split'))"
    assert contenido(L[herm]) == esperado_herm
    abiertos = [p for p in filas(base / "data" / "pendientes.csv")
                if p["id_ledger"] == orig and not p["resuelto"]]
    assert not abiertos, f"el split no cerro el pendiente: {abiertos}"


def test_pagos_de_lu_de_agosto(base, correr):
    """31/8: llavero 602 + cubiertero 451 + goma eva 1.167 pagados por Lu. Antes:
    3 filas + 3 exclusiones + 1 fila de aportes_lu.csv a mano. Ahora: 3 comandos,
    y la posicion de agosto tiene que dar lo mismo al centavo."""
    preparar(base, correr)
    assert not any("pagados por Lu con su tarjeta" in r["nota"]
                   for r in filas(base / "data" / "aportes_lu.csv")), \
        "precondicion: aportes_lu.csv ya migrado (los pagos de Lu son derivados)"
    correr("cierre_casa.py", "2026-08")
    pos = posicion(base, "2026-08")
    assert pos == ("-3446.37", "-7825.94")
    viejas = quitar(base, ["06874e517520", "2fab52531ec6", "826f32ee6d11"])
    antes_ids = {r["id"] for r in filas(base / "data" / "fuentes" / "manual.csv")}
    for monto, cat, desc in (("602", "Auto", "Llavero del auto nuevo"),
                             ("451", "Casa & hogar", "Cubiertero"),
                             ("1167", "Auto", "Goma eva para el auto")):
        correr("gastos.py", "pago-lu", "--fecha", "2026-08-31", "--monto", monto,
               "--categoria", cat, "--desc", desc, "--aplicar", "--sin-pipeline")
    correr("procesar_mails.py")
    correr("merge_y_validar.py")
    correr("cierre_casa.py", "2026-08")
    nuevas = nuevas_manual(base, antes_ids)
    clave = lambda r: (r["monto"], r["categoria"])
    assert sorted(map(contenido, nuevas), key=clave) == sorted(map(contenido, viejas), key=clave)
    assert posicion(base, "2026-08") == pos


def test_compensacion_dilema(base, correr):
    """6/9: Bea paga los 3.110 del Dilema compensandolos con la contribucion:
    un cobro de A cobrar + un gasto de casa desde Cuenta Casa."""
    preparar(base, correr)
    correr("cierre_casa.py", "2026-09")
    pos = posicion(base, "2026-09")
    viejas = quitar(base, ["12446bf72971", "39a0db01f910"])
    antes_ids = {r["id"] for r in filas(base / "data" / "fuentes" / "manual.csv")}
    correr("gastos.py", "cobro", "--monto", "3110", "--fecha", "2026-09-06", "--a", "Santander UYU",
           "--desc", "Cobro del Dilema: Bea paga 3.110 compensando la contribucion",
           "--aplicar", "--sin-pipeline")
    correr("gastos.py", "gasto", "--fecha", "2026-09-06", "--monto", "3110", "--cuenta", "Cuenta Casa",
           "--categoria", "Vivienda", "--ambito", "casa", "--comercio", "Bea Itau (compensado)",
           "--desc", "Contribucion inmobiliaria, ultima cuota: 3.110 compensados con el Dilema",
           "--aplicar")                                      # este corre el pipeline entero
    nuevas = nuevas_manual(base, antes_ids)
    clave = lambda r: r["tipo"]
    assert sorted(map(contenido, nuevas), key=clave) == sorted(map(contenido, viejas), key=clave)
    assert posicion(base, "2026-09") == pos


# ------------------------------------------------------------------ comportamiento
def test_en_seco_no_escribe_nada(base, correr):
    preparar(base, correr)
    antes = {p.name: p.read_bytes() for p in (base / "data").rglob("*.csv")}
    p = correr("gastos.py", "gasto", "--fecha", "2026-09-20", "--monto", "100", "--cuenta", "Efectivo",
               "--categoria", "Super", "--ambito", "personal", "--desc", "prueba")
    assert "en seco" in p.stdout
    assert {q.name: q.read_bytes() for q in (base / "data").rglob("*.csv")} == antes


def test_split_multiple_suma_exacto(base, correr):
    """El caso YouTube: 2/6 propio, 3/6 a Deudas, el resto (madre) a A cobrar.
    Original + hermanas = cargo, en la moneda Y en pesos."""
    preparar(base, correr)
    L = por_id(base / "data" / "ledger.csv")
    o = next(r for r in L.values() if r["origen"] == "mail" and r["tipo"] == "gasto"
             and r["moneda"] == "USD" and not any(x["fuente_id"].startswith(r["id"] + "-split")
                                                  for x in L.values()))
    correr("gastos.py", "split", o["id"], "--mia", "2/6", "--a", "Deudas=3/6", "--a", "A cobrar",
           "--aplicar", "--sin-pipeline")
    correr("merge_y_validar.py")
    L = por_id(base / "data" / "ledger.csv")
    herm = [r for r in L.values() if r["fuente_id"].startswith(o["id"] + "-split")]
    assert sorted(r["cuenta_destino"] for r in herm) == ["A cobrar", "Deudas"]
    for campo in ("monto", "monto_uyu"):
        suma = round(float(L[o["id"]][campo]) + sum(float(r[campo]) for r in herm), 2)
        assert suma == float(o[campo]), f"{campo}: {suma} != {o[campo]}"


def test_split_rechaza_partes_que_no_suman(base, correr):
    preparar(base, correr)
    o = next(r for r in filas(base / "data" / "ledger.csv")
             if r["origen"] == "mail" and r["tipo"] == "gasto" and float(r["monto"]) > 100)
    p = correr("gastos.py", "split", o["id"], "--mia", "50%", "--a", "Deudas=10%", espera_exito=False)
    assert p.returncode == 2 and "no suman" in p.stderr


def test_aporte_repetido_se_rechaza(base, correr):
    """SM12: re-correr la ceremonia duplicaba el aporte de Lu."""
    preparar(base, correr)
    p = correr("gastos.py", "aporte", "--quien", "lu", "--monto", "31000", "--mes", "2026-09",
               "--aplicar", espera_exito=False)
    assert p.returncode == 2 and "ya hay un aporte de Lu" in p.stderr


def test_resolver_valida_contra_los_catalogos(base, correr):
    preparar(base, correr)
    o = next(r for r in filas(base / "data" / "ledger.csv") if r["origen"] == "mail")
    for args, error in ((["categoria=Inventada"], "categorias.csv"),
                        (["cuenta=Santander Platinum USD"], "cuentas.csv"),
                        (["campo_que_no_existe=1"], "no existe"),
                        (["id=abc"], "identidad")):
        p = correr("gastos.py", "resolver", o["id"], *args, "--nota", "t", espera_exito=False)
        assert p.returncode == 2 and error in p.stderr, (args, p.stderr)


def test_resolver_fecha_mueve_el_mes_y_mes_imputa(base, correr):
    """El caso ORT (2/10): una cuota de setiembre pagada en octubre. La fecha
    queda REAL (la conciliacion bancaria la necesita) y `mes` la imputa. Y un
    cambio de fecha a secas arrastra el mes (antes lo dejaba viejo)."""
    preparar(base, correr)
    L = filas(base / "data" / "ledger.csv")
    a, b = [r for r in L if r["origen"] == "mail" and r["tipo"] == "gasto"
            and r["fecha"].endswith(("-10", "-11", "-12"))][:2]
    otro_mes = lambda m: f"{m[:4]}-{int(m[5:7]) % 12 + 1:02d}"
    correr("gastos.py", "resolver", a["id"], f"mes={otro_mes(a['mes'])}", "--nota", "imputacion",
           "--aplicar", "--sin-pipeline")
    nueva_fecha = f"{otro_mes(b['mes'])}-01"
    correr("gastos.py", "resolver", b["id"], f"fecha={nueva_fecha}", "--nota", "fecha real",
           "--aplicar", "--sin-pipeline")
    correr("merge_y_validar.py")
    L = por_id(base / "data" / "ledger.csv")
    assert (L[a["id"]]["fecha"], L[a["id"]]["mes"]) == (a["fecha"], otro_mes(a["mes"]))
    assert (L[b["id"]]["fecha"], L[b["id"]]["mes"]) == (nueva_fecha, nueva_fecha[:7])


def test_resolver_transferencia_a_gasto_completa_monto_uyu(base, correr):
    """Paso dos veces (SA2): una transferencia pasada a gasto sin monto_uyu no
    suma en ningun agregado."""
    preparar(base, correr)
    L = filas(base / "data" / "ledger.csv")
    t = next(r for r in L if r["origen"] == "mail" and r["tipo"] == "transferencia"
             and r["moneda"] == "UYU")
    rows = [dict(r, monto_uyu="") if r["id"] == t["id"] else r for r in L]
    escribir_ledger(base / "data" / "ledger.csv", rows)
    p = correr("gastos.py", "resolver", t["id"], "tipo=gasto", "categoria=Otros", "ambito=personal",
               "--nota", "t")
    assert f"monto_uyu = {t['monto']}" in p.stdout
