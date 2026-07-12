# -*- coding: utf-8 -*-
"""Genera los datasets del dashboard:
- out/presupuesto_vs_real.csv: por (mes, categoria), presupuesto vs gasto real.
- out/casa_mensual.csv: gastos de la CASA por (mes, categoria) con la parte de
  Lu y de Joaco. Historico desde data/casa.csv (planilla congelada, reglas
  espejo de las ventanas del ledger) + jul-2026 en adelante desde data/ledger.csv
  (ambito=casa, dividido por coeficiente de config/ingresos.csv)."""
import csv
import os
import re
from collections import defaultdict

BASE = r"C:\Users\joaco\Desktop\proyectos claude\gastos"
P = lambda *a: os.path.join(BASE, *a)

ppto = defaultdict(float)
meses_ppto = set()
with open(P("data", "presupuesto.csv"), encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        ppto[(r["mes"], r["categoria"])] += float(r["presupuesto"] or 0)
        meses_ppto.add(r["mes"])

real = defaultdict(float)
with open(P("out", "resumen_mensual.csv"), encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        if r["mes"] in meses_ppto:
            real[(r["mes"], r["categoria"])] += float(r["gasto_uyu"] or 0)

claves = sorted(set(ppto) | set(real))
out_path = P("out", "presupuesto_vs_real.csv")
with open(out_path, "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["mes", "categoria", "presupuesto", "gasto_real", "diferencia"])
    for mes, cat in claves:
        p, g = round(ppto.get((mes, cat), 0), 2), round(real.get((mes, cat), 0), 2)
        w.writerow([mes, cat, p, g, round(g - p, 2)])

print(f"{out_path}: {len(claves)} filas, "
      f"ppto total {sum(ppto.values()):,.0f}, real total {sum(real.values()):,.0f}")

# ------------------------------------------------------------------
# out/casa_mensual.csv
# ------------------------------------------------------------------
EXCL = re.compile(r"^\s*(cierre|ajuste|aporte)", re.I)
num = lambda v: float(v) if v not in ("", None) else 0.0

casa = defaultdict(lambda: [0.0, 0.0, 0.0])   # (mes, cat) -> [total, lu, joaco]

# 1) historico: planilla congelada (hasta 2026-06)
with open(P("data", "casa.csv"), encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        egreso = num(r["egreso"])
        if egreso <= 0 or r["era"] == "tricount" or not r["mes"]:
            continue
        if EXCL.match(r["descripcion"] or ""):
            continue
        cat = r["categoria"] or "(sin categoria)"
        if r["era"] == "pre-sistema":
            lu = joaco = egreso / 2          # misma regla que la ventana A del ledger
        else:
            lu, joaco = num(r["gasto_lu"]), num(r["gasto_joaco"])
            if lu <= 0 and joaco <= 0:       # fila sin atribucion -> mitades
                lu = joaco = egreso / 2
        casa[(r["mes"], cat)][0] += egreso
        casa[(r["mes"], cat)][1] += max(lu, 0.0)
        casa[(r["mes"], cat)][2] += max(joaco, 0.0)

# 2) nuevo: ledger ambito=casa desde 2026-07 (dividido por coeficiente)
ingresos = {}
with open(P("config", "ingresos.csv"), encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        tot = float(r["ingreso_lu"]) + float(r["ingreso_joaco"])
        ingresos[r["mes"]] = (float(r["ingreso_lu"]) / tot, float(r["ingreso_joaco"]) / tot)

def coefs(mes):
    if mes in ingresos:
        return ingresos[mes]
    previos = sorted(m for m in ingresos if m <= mes)
    return ingresos[previos[-1]] if previos else (0.5, 0.5)

with open(P("data", "ledger.csv"), encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        if (r["mes"] >= "2026-07" and r["tipo"] == "gasto"
                and r["ambito"] == "casa" and r["en_analisis"] == "1"):
            total = num(r["monto_uyu"])
            c_lu, c_jo = coefs(r["mes"])
            cat = r["categoria"] or "(sin categoria)"
            casa[(r["mes"], cat)][0] += total
            casa[(r["mes"], cat)][1] += total * c_lu
            casa[(r["mes"], cat)][2] += total * c_jo

out_casa = P("out", "casa_mensual.csv")
with open(out_casa, "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["mes", "categoria", "gasto_total", "gasto_lu", "gasto_joaco"])
    for (mes, cat) in sorted(casa):
        t, lu, jo = casa[(mes, cat)]
        w.writerow([mes, cat, round(t, 2), round(lu, 2), round(jo, 2)])

tot = [sum(v[i] for v in casa.values()) for i in range(3)]
print(f"{out_casa}: {len(casa)} filas, total {tot[0]:,.0f} "
      f"(Lu {tot[1]:,.0f} / Joaco {tot[2]:,.0f})")
