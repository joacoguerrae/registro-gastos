# -*- coding: utf-8 -*-
"""Genera out/presupuesto_vs_real.csv: por (mes, categoria nueva),
presupuesto (suma de data/presupuesto.csv) vs gasto real (out/resumen_mensual.csv).
Solo meses con presupuesto (2025-01 en adelante)."""
import csv
import os
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
