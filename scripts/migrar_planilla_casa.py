# -*- coding: utf-8 -*-
"""
Migra data/raw/casa.xlsx (hoja "Planilla de Gastos") a:
  1. data/casa.csv           — todas las filas normalizadas (ambas partes) + era
  2. data/staging/ledger_casa.csv — parte de Joaco como filas de ledger

Contrato: docs/esquema.md (secciones "data/casa.csv", "data/ledger.csv",
"Planilla casa" y "Reglas anti doble conteo").

Reglas aplicadas:
- Filas de datos: 2..1924 (1923 filas).
- era: sin Origen -> pre-sistema; Origen=Tricount -> tricount;
  Origen=Debito Casa -> 50-50 (mes <= 2025-03) o proporcional (mes >= 2025-04).
  Para filas sin fecha, el mes de referencia de la era se infiere de la fila
  con fecha mas cercana hacia arriba (la planilla es cronologica).
- Filas sin fecha tipo aporte: fecha de la fila de gasto pareada (fila
  adyacente con Egresos == Ingresos de esta y con fecha); si no hay par,
  fecha y mes vacios. Filas de GASTO sin fecha: fecha inferida por posicion
  (fila con fecha mas cercana hacia arriba) — se reporta.
- Ledger (solo parte Joaco):
  * Ventana A (fecha 2023-05..2024-08): gastos (Egresos>0) no Cierre/Ajuste/
    Aporte -> monto = Egresos * 0.5.
  * Ventana B (Origen="Debito Casa"): gastos con Gasto Joaco > 0 no Cierre/
    Ajuste/Aporte -> monto = Gasto Joaco.
  * NO se migran: Aportes (Ingresos>0), Cierres, Ajustes, filas Tricount,
    filas sin Origen fuera de la ventana A.
- Concepto -> categoria via config/mapeo_categorias.csv (case-insensitive,
  strip). Sin mapeo -> "Otros" (reportado). Concepto vacio en fila de gasto
  del ledger -> "Otros"; en casa.csv queda categoria vacia.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/migrar_planilla_casa.py
"""
import csv
import datetime as dt
import hashlib
import os
import re
import warnings
from collections import Counter, defaultdict

import openpyxl

warnings.filterwarnings("ignore")

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(BASE, "data", "raw", "casa.xlsx")
MAPEO_CSV = os.path.join(BASE, "config", "mapeo_categorias.csv")
CATEG_CSV = os.path.join(BASE, "config", "categorias.csv")
OUT_CASA = os.path.join(BASE, "data", "casa.csv")
OUT_LEDGER = os.path.join(BASE, "data", "staging", "ledger_casa.csv")

ROW_FIRST, ROW_LAST = 2, 1924  # datos segun esquema.md
EXCL_PAT = re.compile(r"^\s*(cierre|ajuste|aporte)", re.I)
WIN_A_INI = dt.date(2023, 5, 1)
WIN_B_INI = dt.date(2024, 9, 1)   # fin ventana A (exclusivo)
FIN_5050 = "2025-03"              # ultimo mes de era 50-50 dentro de Debito Casa


def to_bool01(v):
    """Checkbox: bool o string TRUE/FALSE -> 1/0."""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "verdadero", "1"):
            return 1
        if s in ("false", "falso", "0", ""):
            return 0
    if v is None:
        return 0
    return 1 if v else 0


def to_num(v):
    """Numeros de la planilla; '-' y '' -> None."""
    if v is None or v == "-" or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def fmt2(v):
    return "" if v is None else f"{v:.2f}"


def fmt_coef(v):
    if v is None:
        return ""
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def main():
    # --- config ---
    with open(MAPEO_CSV, encoding="utf-8-sig") as f:
        mapeo = {r["original"].strip().lower(): r["categoria"].strip()
                 for r in csv.DictReader(f)}
    with open(CATEG_CSV, encoding="utf-8-sig") as f:
        categorias_validas = {r["categoria"].strip() for r in csv.DictReader(f)}
    bad_targets = set(mapeo.values()) - categorias_validas
    assert not bad_targets, f"mapeo apunta a categorias inexistentes: {bad_targets}"

    # --- leer planilla ---
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Planilla de Gastos"]
    rows = []
    for r in range(ROW_FIRST, ROW_LAST + 1):
        c = lambda col: ws.cell(row=r, column=col).value
        f = c(8)
        rows.append({
            "fila": r,
            "descripcion": (str(c(1)).strip() if c(1) is not None else ""),
            "egreso": to_num(c(2)),
            "ingreso": to_num(c(3)),
            "lu": to_bool01(c(4)),
            "joaco": to_bool01(c(5)),
            "concepto": (str(c(6)).strip() if c(6) is not None else ""),
            "tipo_concepto": (str(c(7)).strip() if c(7) is not None else ""),
            "fecha": f.date() if isinstance(f, dt.datetime) else None,
            "origen": (str(c(9)).strip() if c(9) is not None else ""),
            "coef_lu": to_num(c(13)),
            "coef_joaco": to_num(c(14)),
            "saldo": to_num(c(15)),
            "gasto_lu": to_num(c(16)),
            "gasto_joaco": to_num(c(17)),
        })

    # --- fechas: pareo de aportes / inferencia posicional ---
    n = len(rows)
    fechas_pareadas = 0
    fechas_inferidas_gasto = []
    for i, row in enumerate(rows):
        row["fecha_out"] = row["fecha"]       # lo que va al CSV
        row["fecha_ref"] = row["fecha"]       # referencia para era
        if row["fecha"] is not None:
            continue
        es_aporte_like = bool(EXCL_PAT.match(row["descripcion"]))
        # pareo: fila adyacente de gasto con mismo monto y con fecha
        par = None
        if row["ingreso"] and row["ingreso"] > 0:
            for j in (i - 1, i + 1):
                if 0 <= j < n and rows[j]["fecha"] is not None \
                        and rows[j]["egreso"] == row["ingreso"]:
                    par = rows[j]["fecha"]
                    break
        if par is not None:
            row["fecha_out"] = par
            row["fecha_ref"] = par
            fechas_pareadas += 1
            continue
        # inferencia posicional (fila con fecha mas cercana hacia arriba)
        prev = None
        for j in range(i - 1, -1, -1):
            if rows[j]["fecha"] is not None:
                prev = rows[j]["fecha"]
                break
        row["fecha_ref"] = prev
        if es_aporte_like or not (row["egreso"] and row["egreso"] > 0):
            row["fecha_out"] = None           # contrato: vacia si no hay par
        else:
            row["fecha_out"] = prev           # gasto sin fecha: inferida
            fechas_inferidas_gasto.append(row["fila"])

    # --- era ---
    for row in rows:
        if row["origen"] == "":
            row["era"] = "pre-sistema"
        elif row["origen"].lower() == "tricount":
            row["era"] = "tricount"
        else:  # Debito Casa
            ref = row["fecha_ref"]
            mes = f"{ref.year:04d}-{ref.month:02d}" if ref else None
            row["era"] = "50-50" if (mes is None or mes <= FIN_5050) else "proporcional"

    # --- categoria ---
    sin_mapeo = Counter()
    for row in rows:
        con = row["concepto"]
        if con == "":
            row["categoria"] = ""
        else:
            cat = mapeo.get(con.lower())
            if cat is None:
                cat = "Otros"
                sin_mapeo[con] += 1
            row["categoria"] = cat

    # --- data/casa.csv ---
    os.makedirs(os.path.dirname(OUT_CASA), exist_ok=True)
    cols_casa = ["fila", "fecha", "mes", "descripcion", "concepto", "categoria",
                 "tipo_concepto", "egreso", "ingreso", "lu", "joaco",
                 "origen_planilla", "coef_lu", "coef_joaco", "gasto_lu",
                 "gasto_joaco", "saldo_tarjeta", "era"]
    with open(OUT_CASA, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols_casa)
        for row in rows:
            fo = row["fecha_out"]
            w.writerow([
                row["fila"],
                fo.isoformat() if fo else "",
                f"{fo.year:04d}-{fo.month:02d}" if fo else "",
                row["descripcion"],
                row["concepto"],
                row["categoria"],
                row["tipo_concepto"],
                fmt2(row["egreso"]),
                fmt2(row["ingreso"]),
                row["lu"],
                row["joaco"],
                row["origen"],
                fmt_coef(row["coef_lu"]),
                fmt_coef(row["coef_joaco"]),
                fmt2(row["gasto_lu"]),
                fmt2(row["gasto_joaco"]),
                fmt2(row["saldo"]),
                row["era"],
            ])

    # --- ledger_casa.csv ---
    os.makedirs(os.path.dirname(OUT_LEDGER), exist_ok=True)
    cols_ledger = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
                   "cuenta", "cuenta_destino", "categoria", "subcategoria",
                   "comercio", "descripcion", "ambito", "origen", "estado",
                   "en_analisis", "fuente_id"]
    ledger = []
    huerfanas = []  # gastos que no caen en ninguna ventana (se reportan)
    for row in rows:
        d = row["descripcion"]
        excluida = bool(EXCL_PAT.match(d))
        es_gasto = row["egreso"] is not None and row["egreso"] > 0
        if not es_gasto or excluida:
            continue
        fecha = row["fecha_out"]
        monto = None
        if fecha and WIN_A_INI <= fecha < WIN_B_INI and row["origen"] == "":
            monto = round(row["egreso"] * 0.5, 2)          # ventana A: 50/50
        elif row["origen"] == "Debito Casa" and row["gasto_joaco"] is not None \
                and row["gasto_joaco"] > 0:
            monto = round(row["gasto_joaco"], 2)            # ventana B
        else:
            huerfanas.append(row)
            continue
        fuente_id = f"fila:{row['fila']}"
        rid = hashlib.sha1(("planilla_casa" + fuente_id).encode()).hexdigest()[:12]
        cat = row["categoria"] if row["categoria"] else "Otros"
        ledger.append({
            "id": rid,
            "fecha": fecha.isoformat() if fecha else "",
            "mes": f"{fecha.year:04d}-{fecha.month:02d}" if fecha else "",
            "tipo": "gasto",
            "monto": f"{monto:.2f}",
            "moneda": "UYU",
            "monto_uyu": f"{monto:.2f}",
            "cuenta": "Cuenta Casa",
            "cuenta_destino": "",
            "categoria": cat,
            "subcategoria": row["concepto"],
            "comercio": "",
            "descripcion": d,
            "ambito": "casa",
            "origen": "planilla_casa",
            "estado": "confirmado",
            "en_analisis": "1",
            "fuente_id": fuente_id,
        })
    with open(OUT_LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols_ledger)
        w.writeheader()
        w.writerows(ledger)

    # ================= REPORTE =================
    print("=== casa.csv ===")
    print("filas:", len(rows))
    print("filas por era:", dict(Counter(r["era"] for r in rows)))
    eg_por_anio = defaultdict(float)
    for r in rows:
        if r["egreso"]:
            k = r["fecha_out"].year if r["fecha_out"] else "sin_fecha"
            eg_por_anio[k] += r["egreso"]
    print("suma egresos por anio:", {k: round(v, 2) for k, v in sorted(eg_por_anio.items(), key=lambda x: str(x[0]))})
    print("suma egresos total:", round(sum(r["egreso"] or 0 for r in rows), 2))
    print("suma ingresos total:", round(sum(r["ingreso"] or 0 for r in rows), 2))
    print("filas sin fecha original:", sum(1 for r in rows if r["fecha"] is None),
          "| pareadas:", fechas_pareadas,
          "| gasto inferidas por posicion:", len(fechas_inferidas_gasto), fechas_inferidas_gasto,
          "| quedan vacias:", sum(1 for r in rows if r["fecha_out"] is None))
    ult_saldo = None
    for r in rows:
        if r["saldo"] is not None:
            ult_saldo = (r["fila"], r["saldo"])
    print("ultimo saldo_tarjeta:", ult_saldo)
    ws2 = wb["Saldos Tarjeta"]
    print("hoja 'Saldos Tarjeta' Total Saldo Final:", ws2.cell(row=4, column=5).value)

    print("\n=== ledger_casa.csv ===")
    print("filas:", len(ledger))
    led_anio = defaultdict(lambda: [0, 0.0])
    for l in ledger:
        k = l["fecha"][:4] if l["fecha"] else "sin_fecha"
        led_anio[k][0] += 1
        led_anio[k][1] += float(l["monto"])
    print("filas y suma parte Joaco por anio:",
          {k: (v[0], round(v[1], 2)) for k, v in sorted(led_anio.items())})
    print("suma total parte Joaco:", round(sum(float(l["monto"]) for l in ledger), 2))
    va = sum(1 for l in ledger if l["fecha"] and l["fecha"] < "2024-09")
    vb = len(ledger) - va
    print("ventana A (50%):", va, "| ventana B (Gasto Joaco):", vb)

    print("\n=== casos raros ===")
    print("conceptos sin mapeo:", dict(sin_mapeo) if sin_mapeo else "ninguno")
    print("gastos huerfanos (en ninguna ventana del ledger):", len(huerfanas),
          "suma:", round(sum(h["egreso"] for h in huerfanas), 2))
    for h in huerfanas:
        print("  fila", h["fila"], "|", h["fecha_out"], "|", h["origen"] or "(sin origen)",
              "|", h["descripcion"], "| egreso", h["egreso"], "| gasto_joaco", h["gasto_joaco"])

    # ================= VALIDACION =================
    print("\n=== validacion ===")
    oblig = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
             "cuenta", "categoria", "subcategoria", "descripcion", "ambito",
             "origen", "estado", "en_analisis", "fuente_id"]
    permitidos_vacios = {"subcategoria", "descripcion"}  # concepto/desc pueden faltar
    problemas = 0
    for l in ledger:
        for c in oblig:
            if c not in permitidos_vacios and (l[c] is None or l[c] == ""):
                print("VACIO:", l["fuente_id"], c)
                problemas += 1
    print("campos obligatorios vacios en ledger:", problemas)
    print("ids unicos:", len({l["id"] for l in ledger}) == len(ledger))
    print("montos > 0:", all(float(l["monto"]) > 0 for l in ledger))
    print("categorias del ledger validas:",
          set(l["categoria"] for l in ledger) <= categorias_validas)
    sub_vacia = sum(1 for l in ledger if l["subcategoria"] == "")
    print("filas ledger con subcategoria (concepto) vacia:", sub_vacia)
    print("\nmuestra 5 filas ledger:")
    for l in ledger[:2] + ledger[len(ledger) // 2:len(ledger) // 2 + 1] + ledger[-2:]:
        print(" ", [l[c] for c in cols_ledger])
    print("\nmuestra 5 filas casa (fila 2, 300, 900, 1500, 1924):")
    idx = {r["fila"]: r for r in rows}
    for fl in (2, 300, 900, 1500, 1924):
        r = idx[fl]
        print(" ", [r["fila"], r["fecha_out"], r["descripcion"], r["concepto"],
                    r["categoria"], r["egreso"], r["ingreso"], r["origen"],
                    r["gasto_joaco"], r["saldo"], r["era"]])


if __name__ == "__main__":
    main()
