# -*- coding: utf-8 -*-
"""
Fase 3 — Cierre mensual de casa, cuenta continua (docs/fase3.md seccion 3).

Calcula para un mes:
  - Parte de cada uno: gastos ambito=casa en_analisis=1 del mes x coeficiente
    de config/ingresos.csv (mes faltante -> ultimo disponible, "a confirmar").
  - Aportes de Joaco: transferencias del ledger con cuenta_destino=Cuenta Casa
    + gastos ambito=casa pagados desde cuentas que no son de la casa
    (aporte implicito, ej. seguro BSE en la Platinum).
  - Aportes de Lu: data/aportes_lu.csv (input manual del cierre).
  - Posicion nueva = posicion anterior + aportes - parte. La posicion anterior
    sale de la ultima fila de data/cierre_casa.csv, o de la semilla
    config/posicion_casa.json si el archivo esta vacio.
  - Sugerencia de convergencia: con el gasto casa promedio de los ultimos 3
    meses, cuanto conviene que aporte cada uno el mes proximo para que la
    posicion tienda a 0 (sin transferencias entre ellos).

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/cierre_casa.py [YYYY-MM]

Sin argumento cierra el mes actual (parcial si el mes no termino).
Idempotente: si el mes ya tiene fila en data/cierre_casa.csv la recalcula
(y avisa si quedan meses posteriores que habria que recerrar).
"""
import argparse
import csv
import datetime as dt
import json
import os

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

LEDGER = P("data", "ledger.csv")
INGRESOS = P("config", "ingresos.csv")
POSICION_SEED = P("config", "posicion_casa.json")
APORTES_LU = P("data", "aportes_lu.csv")
CIERRE = P("data", "cierre_casa.csv")

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]
CIERRE_COLS = ["mes", "parte_lu", "parte_joaco", "aportes_lu", "aportes_joaco",
               "posicion_lu", "posicion_joaco", "nota"]
APORTES_COLS = ["mes", "fecha", "monto", "nota"]

# Cuentas cuyo dinero YA es de la casa: pagar desde ellas (o transferir desde
# ellas a Cuenta Casa) no es aporte nuevo de Joaco. Mercado Pago fue la cuenta
# casa temporal (jun-jul 2026): el retorno del fondo a Santander es un
# movimiento interno, no un aporte (ver handler transferencia_mp).
CUENTAS_CASA = {"Cuenta Casa", "Santander Mastercard", "Mercado Pago"}
MESES_PROMEDIO = 3


def leer_csv(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        assert rd.fieldnames == cols, f"columnas de {path} != esperadas: {rd.fieldnames}"
        return list(rd)


def escribir_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def fmt(x):
    return f"{x:.2f}"


def mes_anterior(mes):
    y, m = int(mes[:4]), int(mes[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def coeficientes(mes):
    """-> (coef_lu, coef_joaco, mes_usado). Mes faltante: ultimo disponible."""
    filas = leer_csv(INGRESOS, ["mes", "ingreso_lu", "ingreso_joaco"])
    por_mes = {r["mes"]: r for r in filas}
    usado = mes
    if usado not in por_mes:
        previos = sorted(m for m in por_mes if m < mes)
        if not previos:
            raise SystemExit(f"config/ingresos.csv no tiene ningun mes <= {mes}")
        usado = previos[-1]
    r = por_mes[usado]
    lu, joaco = float(r["ingreso_lu"]), float(r["ingreso_joaco"])
    return lu / (lu + joaco), joaco / (lu + joaco), usado


def monto_uyu(r):
    if r["monto_uyu"]:
        return float(r["monto_uyu"])
    if r["moneda"] == "UYU":
        return float(r["monto"])
    raise SystemExit(f"fila {r['id']} ({r['fecha']} {r['descripcion']!r}) en "
                     f"{r['moneda']} sin monto_uyu: resolver antes del cierre")


def gasto_casa_mes(ledger, mes):
    return [r for r in ledger
            if r["mes"] == mes and r["tipo"] == "gasto"
            and r["ambito"] == "casa" and r["en_analisis"] == "1"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mes", nargs="?", default=dt.date.today().strftime("%Y-%m"),
                    help="mes a cerrar YYYY-MM (default: mes actual)")
    args = ap.parse_args()
    mes = args.mes

    with open(POSICION_SEED, encoding="utf-8") as f:
        seed = json.load(f)
    ledger = leer_csv(LEDGER, COLUMNS)
    aportes_lu_rows = leer_csv(APORTES_LU, APORTES_COLS)
    cierres = [r for r in leer_csv(CIERRE, CIERRE_COLS)]

    notas = []

    # ---------------------------------------------------------- posicion previa
    previos = sorted((r for r in cierres if r["mes"] < mes), key=lambda r: r["mes"])
    posteriores = sorted(r["mes"] for r in cierres if r["mes"] > mes)
    if previos:
        pos_lu_prev = float(previos[-1]["posicion_lu"])
        pos_joaco_prev = float(previos[-1]["posicion_joaco"])
        origen_prev = f"cierre {previos[-1]['mes']}"
        if mes_anterior(mes) != previos[-1]["mes"]:
            notas.append(f"hueco: ultimo cierre es {previos[-1]['mes']}")
    else:
        pos_lu_prev, pos_joaco_prev = float(seed["lu"]), float(seed["joaco"])
        origen_prev = f"semilla {seed['fecha_corte']}"
        if mes_anterior(mes) != seed["fecha_corte"][:7]:
            notas.append(f"hueco: semilla es {seed['fecha_corte']}")
    if posteriores:
        notas.append(f"OJO: hay cierres posteriores ({', '.join(posteriores)}) "
                     "que quedan desactualizados — recerrarlos")

    # ---------------------------------------------------------- parte del mes
    coef_lu, coef_joaco, mes_coef = coeficientes(mes)
    if mes_coef != mes:
        notas.append(f"coeficientes de {mes_coef} (mes sin ingresos cargados) — a confirmar")

    gastos = gasto_casa_mes(ledger, mes)
    total = round(sum(monto_uyu(r) for r in gastos), 2)

    # data/participaciones.csv: filas casa con fraccion fija por decision de la
    # revision (ej. Paladar 2/3 Lu por ser con amigas de ella) en vez del
    # coeficiente del mes.
    part_path = P("data", "participaciones.csv")
    participaciones = {}
    if os.path.exists(part_path):
        with open(part_path, encoding="utf-8-sig", newline="") as f:
            participaciones = {r["id_ledger"]: (float(r["frac_lu"]), float(r["frac_joaco"]))
                               for r in csv.DictReader(f)}
    parte_lu = parte_joaco = 0.0
    for r in gastos:
        m = monto_uyu(r)
        f_lu, f_jo = participaciones.get(r["id"], (coef_lu, coef_joaco))
        parte_lu += m * f_lu
        parte_joaco += m * f_jo
    parte_lu = round(parte_lu, 2)
    parte_joaco = round(total - parte_lu, 2)

    # ---------------------------------------------------------- aportes del mes
    # config/cierre_exclusiones.csv: ids que NO cuentan como aporte (ej. espejos
    # de aportes ya incluidos en la posicion semilla de la planilla)
    excl_path = P("config", "cierre_exclusiones.csv")
    excluidos = set()
    if os.path.exists(excl_path):
        with open(excl_path, encoding="utf-8-sig", newline="") as f:
            excluidos = {r["id_ledger"] for r in csv.DictReader(f)}
    transf = [r for r in ledger
              if r["mes"] == mes and r["tipo"] == "transferencia"
              and r["cuenta_destino"] == "Cuenta Casa"
              and r["cuenta"] not in CUENTAS_CASA   # mover el fondo de lugar no es aporte
              and r["id"] not in excluidos]
    implicitos = [r for r in gastos
                  if r["cuenta"] not in CUENTAS_CASA and r["id"] not in excluidos]
    # Retiros personales del fondo: gastos NO-casa o prestamos a Deudas pagados
    # desde cuentas de la casa -> restan del aporte de Joaco.
    retiros = [r for r in ledger
               if r["mes"] == mes and r["cuenta"] in CUENTAS_CASA
               and r["id"] not in excluidos
               and ((r["tipo"] == "gasto" and r["ambito"] in ("personal", "split")
                     and r["en_analisis"] == "1")
                    or (r["tipo"] == "transferencia" and r["cuenta_destino"] == "Deudas"))]
    aportes_joaco = round(sum(monto_uyu(r) for r in transf)
                          + sum(monto_uyu(r) for r in implicitos)
                          - sum(monto_uyu(r) for r in retiros), 2)
    lu_mes = [r for r in aportes_lu_rows if r["mes"] == mes]
    aportes_lu = round(sum(float(r["monto"]) for r in lu_mes), 2)

    # ---------------------------------------------------------- posicion nueva
    pos_lu = round(pos_lu_prev + aportes_lu - parte_lu, 2)
    pos_joaco = round(pos_joaco_prev + aportes_joaco - parte_joaco, 2)

    hoy = dt.date.today().strftime("%Y-%m")
    if mes == hoy:
        notas.append("mes en curso: cierre PARCIAL")

    fila = dict(mes=mes, parte_lu=fmt(parte_lu), parte_joaco=fmt(parte_joaco),
                aportes_lu=fmt(aportes_lu), aportes_joaco=fmt(aportes_joaco),
                posicion_lu=fmt(pos_lu), posicion_joaco=fmt(pos_joaco),
                nota="; ".join(notas))
    cierres = [r for r in cierres if r["mes"] != mes] + [fila]
    cierres.sort(key=lambda r: r["mes"])
    escribir_csv(CIERRE, CIERRE_COLS, cierres)

    # ---------------------------------------------------------- sugerencia
    meses_prom, m = [], mes
    for _ in range(MESES_PROMEDIO):
        meses_prom.append(m)
        m = mes_anterior(m)
    por_mes = {mp: round(sum(monto_uyu(r) for r in gasto_casa_mes(ledger, mp)), 2)
               for mp in meses_prom}
    promedio = round(sum(por_mes.values()) / MESES_PROMEDIO, 2)
    sug_lu = max(0.0, round(coef_lu * promedio - pos_lu, 2))
    sug_joaco = max(0.0, round(coef_joaco * promedio - pos_joaco, 2))

    # ---------------------------------------------------------- reporte
    print(f"=== Cierre de casa {mes} ===")
    print(f"posicion previa ({origen_prev}): Lu {fmt(pos_lu_prev)} / Joaco {fmt(pos_joaco_prev)}")
    print(f"coeficientes ({mes_coef}): Lu {coef_lu:.4f} / Joaco {coef_joaco:.4f}")
    print(f"gasto casa del mes ({len(gastos)} filas): {fmt(total)} UYU")
    print(f"  parte Lu   {fmt(parte_lu):>12s}")
    print(f"  parte Joaco{fmt(parte_joaco):>12s}")
    print(f"aportes Joaco: {fmt(aportes_joaco)}"
          + (f"  (incluye -{sum(monto_uyu(r) for r in retiros):.2f} de retiros personales del fondo)"
             if retiros else ""))
    for r in transf:
        print(f"  transferencia {r['fecha']} {fmt(monto_uyu(r)):>12s}  "
              f"{r['cuenta']} -> Cuenta Casa  {r['descripcion']}")
    for r in implicitos:
        print(f"  implicito     {r['fecha']} {fmt(monto_uyu(r)):>12s}  "
              f"{r['cuenta']}  {r['descripcion']}")
    print(f"aportes Lu: {fmt(aportes_lu)}" +
          (f" ({len(lu_mes)} filas de aportes_lu.csv)" if lu_mes
           else "  (sin filas en data/aportes_lu.csv para el mes)"))
    print(f"POSICION {mes}: Lu {fmt(pos_lu)} / Joaco {fmt(pos_joaco)}  (positivo = a favor)")
    if notas:
        print("notas: " + "; ".join(notas))
    print(f"--- convergencia (promedio casa {MESES_PROMEDIO}m: {fmt(promedio)}: "
          + ", ".join(f"{k} {fmt(v)}" for k, v in sorted(por_mes.items())) + ")")
    print(f"  aporte sugerido proximo mes: Lu {fmt(sug_lu)} / Joaco {fmt(sug_joaco)}")
    print(f"  (coef x promedio - posicion; con eso la posicion tiende a 0 sin "
          "transferencias entre ellos)")


if __name__ == "__main__":
    main()
