# -*- coding: utf-8 -*-
"""Verificacion de data/presupuesto.csv: conteos, sumas, muestra, NaN."""
import sys
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
BASE = r"C:\Users\joaco\Desktop\proyectos claude\gastos"
df = pd.read_csv(BASE + r"\data\presupuesto.csv")
print("shape:", df.shape)
print("columnas:", list(df.columns))
print("meses:", df.mes.nunique(), df.mes.min(), "->", df.mes.max())
print("suma presupuesto:", round(df.presupuesto.sum(), 2))
print("suma gasto_real_registrado:", round(df.gasto_real_registrado.sum(), 2))
print("\nNaN por columna:")
print(df.isna().sum().to_string())
print("\ncomentario no nulo:", int(df.comentario.notna().sum()))
print("\ntipos:", sorted(df.tipo.unique()), "| categorias distintas:", df.categoria.nunique())
cat = pd.read_csv(BASE + r"\config\categorias.csv")
fuera = df.loc[~df.categoria.isin(cat.categoria), "categoria"].unique()
print("categorias fuera de catalogo:", list(fuera))
print("negativos: ppto", int((df.presupuesto < 0).sum()), "| gasto", int((df.gasto_real_registrado < 0).sum()))
print("duplicados (mes,concepto):", int(df.duplicated(["mes", "concepto_original"]).sum()))
print("\nmuestra 5 filas:")
print(df.sample(5, random_state=7).to_string())
print("\nfilas por mes:")
print(df.groupby("mes").size().to_string())
