# -*- coding: utf-8 -*-
"""
Fase 3 -- Conciliacion de saldos por cuenta (docs/fase3.md seccion 4).

saldo_calculado por (cuenta, moneda) = saldo de config/saldos_iniciales.csv
(la semilla fija, corte 2026-07-12) + movimientos del ledger POSTERIORES a
ese corte (fecha > fecha_corte de la semilla) y hasta el corte que se esta
conciliando (inclusive):
  - gasto:          resta de su cuenta         (tarjeta de credito: SUMA deuda)
  - ingreso:        suma a su cuenta           (tarjeta de credito: resta deuda)
  - transferencia:  resta de `cuenta` (origen) (tarjeta de credito: SUMA deuda,
                     ej. "Regalo agus" retirado de la Platinum)
                    suma a `cuenta_destino`     (tarjeta de credito: resta deuda,
                     es un pago que baja la deuda)

Cuentas con saldo multi-moneda que no tienen entrada propia en
config/cuentas.csv (ej. "Santander Platinum USD", "Deudas USD", "Efectivo
USD") se resuelven contra la cuenta base del ledger (se les saca el sufijo
" USD") filtrando por moneda=USD.

Saldos reales: se leen de un CSV `cuenta,moneda,saldo` (default
data/staging/saldos_reales.csv) que la ceremonia /cierre-mes completa
preguntandole a Joaco. Se appendea a data/saldos.csv:
  fecha_corte,cuenta,moneda,saldo_calculado,saldo_real,diferencia,comentario
Si diferencia != 0, el comentario lista los movimientos "sospechosos" (los
de mayor monto) del periodo posterior de esa cuenta -- NUNCA se ajusta a
ciegas.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/conciliar_saldos.py [fecha_corte] [--reales PATH]

fecha_corte: default hoy (YYYY-MM-DD). --reales: default data/staging/saldos_reales.csv
Idempotente: si ya hay filas para (fecha_corte, cuenta, moneda) en
data/saldos.csv, se reemplazan.
"""
import argparse
import csv
import datetime as dt
import json
import os

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

LEDGER = P("data", "ledger.csv")
CUENTAS_CSV = P("config", "cuentas.csv")
SALDOS_INICIALES = P("config", "saldos_iniciales.csv")
SALDOS = P("data", "saldos.csv")
PARAMETROS = P("config", "parametros.json")
REALES_DEFAULT = P("data", "staging", "saldos_reales.csv")

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]
INICIALES_COLS = ["cuenta", "fecha_corte", "saldo", "moneda", "fuente", "nota"]
REALES_COLS = ["cuenta", "moneda", "saldo"]
SALDOS_COLS = ["fecha_corte", "cuenta", "moneda", "saldo_calculado",
               "saldo_real", "diferencia", "comentario"]

# Movimiento del periodo cuyo monto_uyu supere esto se lista como "sospechoso"
# cuando la cuenta queda con diferencia != 0.
UMBRAL_SOSPECHOSO_UYU = 3000.0


def leer_csv(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        assert rd.fieldnames == cols, f"columnas de {path} != esperadas: {rd.fieldnames}"
        rows = list(rd)
        for i, r in enumerate(rows):
            if None in r:
                raise SystemExit(
                    f"{path}: fila {i + 2} mal formada (mas columnas que el header, "
                    f"probablemente una coma sin comillas en un campo de texto): {r}")
        return rows


def escribir_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def fmt(x):
    return f"{x:.2f}"


def cargar_tipos_cuenta():
    """cuenta canonica (config/cuentas.csv) -> tipo (banco/tarjeta_credito/...)."""
    out = {}
    with open(CUENTAS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["cuenta"]] = r["tipo"]
    return out


def cuenta_ledger(cuenta_saldo, tipos_cuenta):
    """Traduce el nombre de config/saldos_iniciales.csv al (cuenta) tal como
    aparece en el ledger. Sufijo ' USD' sintetico (no existe en cuentas.csv)
    -> se le saca el sufijo (la cuenta base ya trae la moneda en el campo
    `moneda` del ledger)."""
    if cuenta_saldo in tipos_cuenta:
        return cuenta_saldo
    if cuenta_saldo.endswith(" USD"):
        base = cuenta_saldo[:-4].strip()
        if base in tipos_cuenta:
            return base
    return cuenta_saldo


def monto_uyu_aprox(r, tc):
    if r["monto_uyu"]:
        return float(r["monto_uyu"])
    if r["moneda"] == "UYU":
        return float(r["monto"])
    return float(r["monto"]) * tc.get(r["moneda"], 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fecha_corte", nargs="?",
                    default=dt.date.today().isoformat(),
                    help="corte a conciliar YYYY-MM-DD (default: hoy)")
    ap.add_argument("--reales", default=REALES_DEFAULT,
                    help="CSV cuenta,moneda,saldo con los saldos reales reportados")
    args = ap.parse_args()
    fecha_corte = args.fecha_corte

    tipos_cuenta = cargar_tipos_cuenta()
    iniciales = leer_csv(SALDOS_INICIALES, INICIALES_COLS)
    if not iniciales:
        raise SystemExit(f"{SALDOS_INICIALES} vacio o inexistente")
    reales_rows = leer_csv(args.reales, REALES_COLS)
    if not reales_rows:
        raise SystemExit(f"no hay saldos reales en {args.reales} "
                          "(cuenta,moneda,saldo) -- cargarlos antes de conciliar")
    reales = {(r["cuenta"], r["moneda"]): float(r["saldo"]) for r in reales_rows}

    ledger = leer_csv(LEDGER, COLUMNS)
    with open(PARAMETROS, encoding="utf-8") as f:
        params = json.load(f)
    tc = {"USD": params["tc_usd_uyu"], "EUR": params["tc_eur_uyu"]}

    corte_inicial = {r["fecha_corte"] for r in iniciales}
    if len(corte_inicial) > 1:
        raise SystemExit(f"config/saldos_iniciales.csv tiene mas de un fecha_corte: {corte_inicial}")
    corte_inicial = corte_inicial.pop()
    if fecha_corte < corte_inicial:
        raise SystemExit(f"fecha_corte {fecha_corte} es anterior a la semilla ({corte_inicial})")

    # movimientos posteriores al corte inicial y hasta el corte a conciliar (incl.)
    movs = [r for r in ledger if corte_inicial < r["fecha"] <= fecha_corte]

    def efecto(cuenta_ledg, es_origen):
        es_tarjeta = tipos_cuenta.get(cuenta_ledg) == "tarjeta_credito"
        if es_origen:
            return 1.0 if es_tarjeta else -1.0
        return -1.0 if es_tarjeta else 1.0

    resultados = []
    for ini in iniciales:
        cuenta_saldo, moneda = ini["cuenta"], ini["moneda"]
        cuenta_l = cuenta_ledger(cuenta_saldo, tipos_cuenta)
        saldo = float(ini["saldo"])
        movimientos_cuenta = []

        for r in movs:
            if r["moneda"] != moneda:
                continue
            monto = float(r["monto"])
            if r["tipo"] == "gasto" and r["cuenta"] == cuenta_l:
                saldo += efecto(cuenta_l, True) * monto
                movimientos_cuenta.append((r, "gasto", efecto(cuenta_l, True) * monto))
            elif r["tipo"] == "ingreso" and r["cuenta"] == cuenta_l:
                saldo += efecto(cuenta_l, False) * monto
                movimientos_cuenta.append((r, "ingreso", efecto(cuenta_l, False) * monto))
            elif r["tipo"] == "transferencia":
                if r["cuenta"] == cuenta_l:
                    saldo += efecto(cuenta_l, True) * monto
                    movimientos_cuenta.append((r, "transf-origen", efecto(cuenta_l, True) * monto))
                if r["cuenta_destino"] == cuenta_l:
                    saldo += efecto(cuenta_l, False) * monto
                    movimientos_cuenta.append((r, "transf-destino", efecto(cuenta_l, False) * monto))

        saldo = round(saldo, 2)
        key = (cuenta_saldo, moneda)
        if key not in reales:
            resultados.append((cuenta_saldo, moneda, saldo, None, None, movimientos_cuenta,
                                "SIN saldo real reportado"))
            continue
        real = reales[key]
        diferencia = round(real - saldo, 2)
        comentario = ini["nota"]
        if diferencia != 0:
            sospechosos = [(r, tag, efecto_monto) for (r, tag, efecto_monto) in movimientos_cuenta
                           if monto_uyu_aprox(r, tc) >= UMBRAL_SOSPECHOSO_UYU]
            if sospechosos:
                detalle = "; ".join(
                    f"{r['fecha']} {tag} {fmt(efecto_monto)} {r['moneda']} "
                    f"({r['descripcion'] or r['comercio']})"
                    for r, tag, efecto_monto in sospechosos)
                comentario = f"DIFERENCIA {fmt(diferencia)}. sospechosos: {detalle}"
            else:
                comentario = (f"DIFERENCIA {fmt(diferencia)}. sin movimientos grandes "
                              f"(>= {UMBRAL_SOSPECHOSO_UYU:.0f}) en el periodo -- revisar a mano")
        resultados.append((cuenta_saldo, moneda, saldo, real, diferencia, movimientos_cuenta, comentario))

    # -------------------------------------------------------------- persistir
    saldos = leer_csv(SALDOS, SALDOS_COLS)
    claves_nuevas = {(fecha_corte, c, m) for (c, m, *_ ) in resultados}
    # si ya habia una fila con la MISMA clave (fecha_corte,cuenta,moneda) y un
    # comentario propio (ej. una fila cargada a mano antes de que existiera
    # esta cuenta canonica), preservamos ese texto agregandolo al nuevo
    comentarios_previos = {(r["fecha_corte"], r["cuenta"], r["moneda"]): r["comentario"]
                           for r in saldos if r["comentario"]}
    saldos = [r for r in saldos
              if (r["fecha_corte"], r["cuenta"], r["moneda"]) not in claves_nuevas]
    for (cuenta_saldo, moneda, saldo, real, diferencia, _, comentario) in resultados:
        previo = comentarios_previos.get((fecha_corte, cuenta_saldo, moneda))
        if previo:
            if comentario and comentario in previo:
                comentario = previo   # ya fusionado en una corrida anterior
            elif comentario:
                comentario = f"{comentario} (hist: {previo})"
            else:
                comentario = previo
        saldos.append(dict(
            fecha_corte=fecha_corte, cuenta=cuenta_saldo, moneda=moneda,
            saldo_calculado=fmt(saldo),
            saldo_real=fmt(real) if real is not None else "",
            diferencia=fmt(diferencia) if diferencia is not None else "",
            comentario=comentario))
    saldos.sort(key=lambda r: (r["fecha_corte"], r["cuenta"], r["moneda"]))
    escribir_csv(SALDOS, SALDOS_COLS, saldos)

    # -------------------------------------------------------------- reporte
    print(f"=== Conciliacion de saldos {fecha_corte} (semilla {corte_inicial}) ===")
    print(f"movimientos posteriores a la semilla considerados: {len(movs)}")
    n_ok, n_diff, n_sin_real = 0, 0, 0
    for (cuenta_saldo, moneda, saldo, real, diferencia, movimientos_cuenta, comentario) in resultados:
        etiqueta = "SIN REAL" if real is None else ("OK" if diferencia == 0 else "DIFERENCIA")
        if real is None:
            n_sin_real += 1
        elif diferencia == 0:
            n_ok += 1
        else:
            n_diff += 1
        real_txt = fmt(real) if real is not None else "-"
        diff_txt = fmt(diferencia) if diferencia is not None else "-"
        print(f"  {cuenta_saldo:26s} {moneda:3s}  calculado {fmt(saldo):>12s}  "
              f"real {real_txt:>12s}  dif {diff_txt:>10s}  [{etiqueta}]  "
              f"({len(movimientos_cuenta)} mov. posteriores)")
    print(f"resumen: {n_ok} OK, {n_diff} con diferencia, {n_sin_real} sin saldo real")
    if n_diff:
        print("cuentas con diferencia (detalle en data/saldos.csv, columna comentario):")
        for (cuenta_saldo, moneda, saldo, real, diferencia, _, comentario) in resultados:
            if real is not None and diferencia != 0:
                print(f"  {cuenta_saldo} {moneda}: {comentario}")


if __name__ == "__main__":
    main()
