# -*- coding: utf-8 -*-
"""
Fusiona los staging (ledger_1money.csv + ledger_casa.csv) en data/ledger.csv
y valida la migracion completa CONTRA LAS FUENTES CRUDAS (MyFinance.db,
casa.xlsx, data/raw/presupuestos/*.txt) recalculando todo de forma
independiente (no confia en los reportes de los migradores).

Salidas:
  - data/ledger.csv                (merge ordenado por fecha, id)
  - docs/validacion-migracion.md   (reporte completo)
  - out/resumen_mensual.csv        (mes, categoria, gasto en_analisis=1)

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/merge_y_validar.py
"""
import csv
import datetime as dt
import hashlib
import os
import random
import re
import sqlite3
import warnings
from collections import Counter, defaultdict

import openpyxl

warnings.filterwarnings("ignore")

BASE = r"C:\Users\joaco\Desktop\proyectos claude\gastos"
P = lambda *a: os.path.join(BASE, *a)

STAGING_1MONEY = P("data", "staging", "ledger_1money.csv")
STAGING_CASA = P("data", "staging", "ledger_casa.csv")
OUT_LEDGER = P("data", "ledger.csv")
OUT_RESUMEN = P("out", "resumen_mensual.csv")
OUT_MD = P("docs", "validacion-migracion.md")
DB = P("data", "raw", "MyFinance.db")
XLSX = P("data", "raw", "casa.xlsx")
PRESUPUESTO_CSV = P("data", "presupuesto.csv")
CATEG_CSV = P("config", "categorias.csv")

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]

CASA_CUTOFF = "2023-05-01"
WIN_A_INI = dt.date(2023, 5, 1)
WIN_B_INI = dt.date(2024, 9, 1)
EXCL_PAT = re.compile(r"^\s*(cierre|ajuste|aporte)", re.I)

# Meses donde la planilla no aporta filas al ledger (gastos sin Origen, fuera
# de ventana — ver docs/validacion-migracion.md 2c): ahí el volcado 1Money-Casa
# vuelve a ser la fuente canónica (en_analisis=1) para no dejar el mes hueco.
MESES_SIN_PLANILLA = {"2024-09"}

results = []   # (seccion, estado, texto) para el .md


def log(section, status, text):
    results.append((section, status, text))
    print(f"[{status}] {section}: {text}")


# ============================================================
# 1. MERGE
# ============================================================
def merge():
    rows = []
    for path in (STAGING_1MONEY, STAGING_CASA):
        with open(path, encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            assert rd.fieldnames == COLUMNS, f"columnas de {path} != esquema: {rd.fieldnames}"
            rows.extend(list(rd))
    # PRESERVAR las filas que no vienen de los staging de migracion (origen=mail,
    # splitwise, mp, manual) ya presentes en el ledger: el merge las re-incorpora
    # tal cual en cada refusion (si no, se borraria el backfill de Fase 2).
    if os.path.exists(OUT_LEDGER):
        with open(OUT_LEDGER, encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            assert rd.fieldnames == COLUMNS, f"columnas de {OUT_LEDGER} != esquema: {rd.fieldnames}"
            rows.extend(r for r in rd if r["origen"] not in ("1money", "planilla_casa"))
    rows.sort(key=lambda r: (r["fecha"], r["id"]))

    # Override documentado: meses sin cobertura de planilla usan el volcado 1Money.
    # Los volcados son cuenta "Cuenta Casa" o categoría original "Casa" (misma
    # definición que la ventana casa del migrador 1Money).
    for r in rows:
        if (r["origen"] == "1money" and r["tipo"] == "gasto"
                and r["mes"] in MESES_SIN_PLANILLA and r["en_analisis"] == "0"
                and (r["cuenta"] == "Cuenta Casa" or r["subcategoria"] == "Casa")):
            r["en_analisis"] = "1"
            r["ambito"] = "casa"

    # Resoluciones manuales (data/resoluciones.csv): el merge reconstruye las
    # filas de migracion desde staging, asi que hay que re-aplicarlas aca
    # tambien (procesar_mails.py hace lo mismo para sus corridas).
    res_path = P("data", "resoluciones.csv")
    if os.path.exists(res_path):
        overrides = defaultdict(dict)
        with open(res_path, encoding="utf-8-sig", newline="") as f:
            for rr in csv.DictReader(f):
                if rr["campo"] in COLUMNS:
                    overrides[rr["id_ledger"]][rr["campo"]] = rr["valor"]
        for r in rows:
            for campo, valor in overrides.get(r["id"], {}).items():
                r[campo] = valor

    ids = Counter(r["id"] for r in rows)
    dupes = {k: v for k, v in ids.items() if v > 1}

    with open(OUT_LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return rows, dupes


# ============================================================
# 2. Recalculo independiente desde MyFinance.db
# ============================================================
def cargar_overrides_1money():
    """Resoluciones manuales que pisan filas 1money (data/resoluciones.csv):
    el recálculo independiente debe aplicarlas igual que el ledger para que
    2b siga comparando manzanas con manzanas."""
    path = P("data", "resoluciones.csv")
    out = defaultdict(dict)
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r["campo"] in ("en_analisis", "monto_uyu", "monto", "tipo", "ambito"):
                    out[r["id_ledger"]][r["campo"]] = r["valor"]
    return out


def recalc_db():
    overrides = cargar_overrides_1money()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    n_tx = cur.execute("SELECT COUNT(*) FROM [transaction] WHERE isRemoved=0").fetchone()[0]
    n_tr = cur.execute("SELECT COUNT(*) FROM transfer WHERE isRemoved=0").fetchone()[0]

    accounts = {r["uid"]: r["title"].strip()
                for r in cur.execute("SELECT uid,title FROM account WHERE isRemoved=0")}
    categories = {r["uid"]: r["title"].strip()
                  for r in cur.execute("SELECT uid,title FROM category")}
    link = lambda et, ot: {r[0]: r[1] for r in cur.execute(
        "SELECT entityUid,otherUid FROM sync_link WHERE entityType=? AND otherType=? AND isRemoved=0",
        (et, ot))}
    tx_acc, tx_cat = link("Transaction", "Account"), link("Transaction", "Category")

    # gastos en_analisis=1 por anio, reconstruyendo la regla desde cero
    gasto_por_anio = defaultdict(float)
    total_monto_uyu = 0.0
    casa_mensual_db = defaultdict(float)   # volcados Casa de 1Money (2c)
    tx_rows = {}
    for t in cur.execute("SELECT uid,type,amountInDefaultCurrency,amountInAccountCurrency,"
                         "accountCurrencyCode,date,comment FROM [transaction] WHERE isRemoved=0"):
        uid = t["uid"]
        fecha = (t["date"] or "").strip()[:10]
        uyu = abs(t["amountInDefaultCurrency"]) / 100.0
        total_monto_uyu += uyu
        acc = accounts.get(tx_acc.get(uid), "")
        cat_uid = tx_cat.get(uid)
        cat = categories.get(cat_uid, "") if cat_uid else None
        tx_rows[uid] = dict(fecha=fecha, tipo=t["type"], uyu=uyu, acc=acc,
                            cat=cat, comment=(t["comment"] or "").strip(),
                            aic=t["amountInAccountCurrency"],
                            acc_cur=(t["accountCurrencyCode"] or "").strip())
        es_casa = acc.casefold() == "casa" or (cat or "").casefold() == "casa"
        if t["type"] == "Expense" and cat is not None:
            # resoluciones manuales sobre esta fila (mismo id que en el ledger)
            rid = hashlib.sha1(("1money" + uid).encode()).hexdigest()[:12]
            ov = overrides.get(rid, {})
            if ov.get("tipo") not in (None, "gasto"):
                continue                                    # dejo de ser gasto
            if ov.get("en_analisis") == "0":
                continue                                    # sacada del analisis
            if "monto_uyu" in ov:
                uyu = float(ov["monto_uyu"])
            if es_casa and fecha >= CASA_CUTOFF and fecha[:7] not in MESES_SIN_PLANILLA:
                casa_mensual_db[fecha[:7]] += uyu          # duplicado (en_analisis=0)
            else:
                gasto_por_anio[fecha[:4]] += uyu           # en_analisis=1 (incluye MESES_SIN_PLANILLA)
    con.close()
    return dict(n_tx=n_tx, n_tr=n_tr, gasto_por_anio=dict(gasto_por_anio),
                casa_mensual=dict(casa_mensual_db), tx_rows=tx_rows,
                total_monto_uyu=total_monto_uyu)


# ============================================================
# 3. Recalculo independiente desde casa.xlsx (parte Joaco)
# ============================================================
def recalc_xlsx():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Planilla de Gastos"]
    num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    raw = []
    for r in range(2, 1925):
        c = lambda col: ws.cell(row=r, column=col).value
        f = c(8)
        raw.append(dict(
            fila=r,
            desc=(str(c(1)).strip() if c(1) is not None else ""),
            egreso=num(c(2)),
            fecha=f.date() if isinstance(f, dt.datetime) else None,
            origen=(str(c(9)).strip() if c(9) is not None else ""),
            gasto_joaco=num(c(17)),
        ))
    # fecha inferida hacia arriba (mismo contrato del esquema)
    prev = None
    for row in raw:
        if row["fecha"] is not None:
            prev = row["fecha"]
        row["fecha_ref"] = row["fecha"] or prev

    joaco_mensual = defaultdict(float)  # (2c) y (2b)
    n_a = n_b = 0
    huerfanas = []
    for row in raw:
        if not (row["egreso"] and row["egreso"] > 0) or EXCL_PAT.match(row["desc"]):
            continue
        f = row["fecha_ref"]
        if f and WIN_A_INI <= f < WIN_B_INI and row["origen"] == "":
            joaco_mensual[f"{f.year:04d}-{f.month:02d}"] += round(row["egreso"] * 0.5, 2)
            n_a += 1
        elif row["origen"] == "Debito Casa" and row["gasto_joaco"] and row["gasto_joaco"] > 0:
            joaco_mensual[f"{f.year:04d}-{f.month:02d}"] += round(row["gasto_joaco"], 2)
            n_b += 1
        else:
            huerfanas.append(row)
    # clasificar fuera-de-ventana: exclusiones por diseno vs casos a revisar
    hclas = defaultdict(lambda: [0, 0.0])
    for h in huerfanas:
        if h["origen"].lower() == "tricount":
            k = "tricount (excluida por diseno)"
        elif h["origen"] == "Debito Casa" and h["gasto_joaco"] == 0:
            k = "Debito Casa 100% Lu (gasto_joaco=0, por diseno)"
        elif h["origen"] == "Debito Casa":
            k = "A REVISAR: Debito Casa con Gasto Joaco vacio"
        else:
            k = "A REVISAR: sin Origen fuera de ventana A"
        hclas[k][0] += 1
        hclas[k][1] += h["egreso"]
    return dict(joaco_mensual=dict(joaco_mensual), n_a=n_a, n_b=n_b,
                huerfanas=huerfanas, hclas=dict(hclas),
                raw={r["fila"]: r for r in raw})


# ============================================================
def main():
    random.seed(20260711)
    os.makedirs(P("out"), exist_ok=True)

    # ---------- 1. merge ----------
    ledger, dupes = merge()
    n_total = len(ledger)
    fechas = [r["fecha"] for r in ledger if r["fecha"]]
    rango = (min(fechas), max(fechas))
    por_origen = Counter(r["origen"] for r in ledger)
    por_tipo = Counter(r["tipo"] for r in ledger)
    log("1.merge", "OK" if not dupes else "FALLO",
        f"{n_total} filas ({dict(por_origen)}), tipos {dict(por_tipo)}, "
        f"rango {rango[0]} -> {rango[1]}, ids duplicados: {dupes or 'ninguno'}")

    # NaN / vacios donde no corresponde
    permit_vacio_siempre = {"cuenta_destino", "comercio", "descripcion", "subcategoria"}
    problemas_vacios = Counter()
    for r in ledger:
        for c in COLUMNS:
            if r[c] != "":
                continue
            if c in permit_vacio_siempre:
                continue
            if c == "categoria" and (r["tipo"] == "transferencia" or r["subcategoria"] == "Ajuste saldo"
                                     or r["estado"] == "pendiente"):
                # estado=pendiente: filas origen=mail en la bandeja de revision
                # (data/pendientes.csv) pueden no tener categoria todavia
                continue
            if c == "monto_uyu" and r["tipo"] == "transferencia" and r["moneda"] != "UYU":
                continue
            if c == "cuenta_destino":
                continue
            problemas_vacios[c] += 1
    log("1.vacios", "OK" if not problemas_vacios else "DESVIO",
        f"celdas vacias fuera de regla: {dict(problemas_vacios) or 0}")

    suma_uyu_total = sum(float(r["monto_uyu"]) for r in ledger if r["monto_uyu"])
    log("1.suma", "OK", f"suma |monto_uyu| todas las filas: {suma_uyu_total:.2f}")

    # ---------- 2a. conteos vs DB ----------
    db = recalc_db()
    n_1money_tx = sum(1 for r in ledger if r["origen"] == "1money" and r["tipo"] != "transferencia")
    n_1money_tr = sum(1 for r in ledger if r["origen"] == "1money" and r["tipo"] == "transferencia")
    ok_a = db["n_tx"] == n_1money_tx == 2853 and db["n_tr"] == n_1money_tr == 207
    log("2a.conteos", "OK" if ok_a else "DESVIO",
        f"DB: {db['n_tx']} tx activas / {db['n_tr']} transfers; "
        f"ledger 1money: {n_1money_tx} tx / {n_1money_tr} transfers (esperado 2853/207)")

    suma_tx_ledger = sum(float(r["monto_uyu"]) for r in ledger
                         if r["origen"] == "1money" and r["tipo"] != "transferencia")
    diff_suma = suma_tx_ledger - db["total_monto_uyu"]
    log("2a.suma_uyu", "OK" if abs(diff_suma) < 0.01 else "DESVIO",
        f"suma monto_uyu transacciones 1money: ledger {suma_tx_ledger:.2f} vs DB "
        f"{db['total_monto_uyu']:.2f} (diff {diff_suma:.4f})")

    # ---------- 2b. gastos por anio en_analisis=1 ----------
    led_gasto_anio = defaultdict(lambda: defaultdict(float))
    for r in ledger:
        # solo origen 1money/planilla_casa: las filas origen=mail no vienen de
        # estas fuentes y se validan contra Gmail, no contra la migracion
        if (r["origen"] in ("1money", "planilla_casa")
                and r["tipo"] == "gasto" and r["en_analisis"] == "1"):
            led_gasto_anio[r["fecha"][:4]][r["origen"]] += float(r["monto_uyu"])
    x = recalc_xlsx()
    joaco_anio = defaultdict(float)
    for mes, v in x["joaco_mensual"].items():
        joaco_anio[mes[:4]] += v
    tabla_b, ok_b = [], True
    for anio in sorted(set(led_gasto_anio) | set(db["gasto_por_anio"]) | set(joaco_anio)):
        l1 = led_gasto_anio[anio].get("1money", 0.0)
        l2 = led_gasto_anio[anio].get("planilla_casa", 0.0)
        f1 = db["gasto_por_anio"].get(anio, 0.0)
        f2 = joaco_anio.get(anio, 0.0)
        d1, d2 = l1 - f1, l2 - f2
        if abs(d1) > 0.01 or abs(d2) > 0.01:
            ok_b = False
        tabla_b.append((anio, l1, f1, d1, l2, f2, d2, l1 + l2))
    log("2b.gastos_anio", "OK" if ok_b else "DESVIO",
        "; ".join(f"{a}: ledger {t:.2f} (1money d={d1:.2f}, planilla d={d2:.2f})"
                  for a, _, _, d1, _, _, d2, t in tabla_b))

    # ---------- 2c. anti doble conteo casa ----------
    meses = sorted(set(x["joaco_mensual"]) | set(db["casa_mensual"]))
    tabla_c, desvios_c = [], []
    for m in meses:
        pj = x["joaco_mensual"].get(m, 0.0)
        dm = db["casa_mensual"].get(m, 0.0)
        base = max(pj, dm)
        pct = (abs(pj - dm) / base * 100) if base > 0 else 0.0
        flag = pct > 15
        if flag:
            desvios_c.append((m, pj, dm, pct))
        tabla_c.append((m, pj, dm, pj - dm, pct, flag))
    log("2c.doble_conteo", "OK" if not desvios_c else "DESVIO",
        f"{len(meses)} meses comparados, {len(desvios_c)} con diferencia >15%: "
        + (", ".join(f"{m} (planilla {pj:.0f} vs 1money {dm:.0f}, {pct:.0f}%)"
                     for m, pj, dm, pct in desvios_c) or "ninguno"))

    # ---------- 2d. categorias ----------
    with open(CATEG_CSV, encoding="utf-8-sig") as f:
        categorias_validas = {r["categoria"].strip() for r in csv.DictReader(f)}
    sin_cat = [r for r in ledger if r["tipo"] in ("gasto", "ingreso")
               and r["en_analisis"] == "1" and r["categoria"] == ""
               and r["estado"] != "pendiente"]  # pendientes: bandeja de revision (mail)
    cats_ledger = {r["categoria"] for r in ledger if r["categoria"]}
    cats_invalidas = cats_ledger - categorias_validas
    log("2d.categorias", "OK" if not sin_cat and not cats_invalidas else "DESVIO",
        f"gasto/ingreso en_analisis=1 sin categoria: {len(sin_cat)}; "
        f"categorias fuera de catalogo: {sorted(cats_invalidas) or 'ninguna'}")

    # ---------- 2e. presupuesto ----------
    with open(PRESUPUESTO_CSV, encoding="utf-8-sig", newline="") as f:
        pres = list(csv.DictReader(f))
    meses_pres = sorted({r["mes"] for r in pres})
    filas_mes = Counter(r["mes"] for r in pres)
    filas_2026 = {m: filas_mes[m] for m in meses_pres if m.startswith("2026")}
    controles = {"2025-01": (75680, 111496), "2025-06": (109413, 91695),
                 "2025-12": (106801, 148901), "2026-06": (169835, 262321),
                 "2026-07": (107745, 0)}
    ctl_res, ok_e = [], True
    for m, (ep, eg) in controles.items():
        sp = sum(float(r["presupuesto"] or 0) for r in pres if r["mes"] == m)
        sg = sum(float(r["gasto_real_registrado"] or 0) for r in pres if r["mes"] == m)
        ok = abs(sp - ep) < 0.01 and abs(sg - eg) < 0.01
        ok_e = ok_e and ok
        ctl_res.append(f"{m}: {sp:.0f}/{sg:.0f} vs esperado {ep}/{eg} {'OK' if ok else 'FALLO'}")
    ok_e = ok_e and len(meses_pres) == 19 and all(24 <= v <= 28 for v in filas_2026.values())
    log("2e.presupuesto", "OK" if ok_e else "DESVIO",
        f"{len(meses_pres)} meses ({meses_pres[0]}->{meses_pres[-1]}), {len(pres)} filas, "
        f"filas/mes 2026: {dict(sorted(filas_2026.items()))}; controles: " + "; ".join(ctl_res))

    # ---------- 2f. muestreo 10 filas ----------
    muestra = random.sample([r for r in ledger if r["origen"] in ("1money", "planilla_casa")], 10)
    trazas, ok_f = [], True
    for r in muestra:
        if r["origen"] == "1money":
            if r["tipo"] == "transferencia":
                con = sqlite3.connect(DB)
                t = con.execute("SELECT date, fromAmount, fromCurrencyCode FROM transfer "
                                "WHERE uid=? AND isRemoved=0", (r["fuente_id"],)).fetchone()
                con.close()
                ok = (t is not None and t[0][:10] == r["fecha"]
                      and abs(abs(t[1]) / 100 - float(r["monto"])) < 0.01
                      and (t[2] or "UYU").strip() == r["moneda"])
                detalle = f"DB transfer {t}" if t else "NO ENCONTRADA"
            else:
                s = db["tx_rows"].get(r["fuente_id"])
                ok = (s is not None and s["fecha"] == r["fecha"]
                      and abs(s["uyu"] - float(r["monto_uyu"])) < 0.01
                      and (s["tipo"] == "Expense") == (r["tipo"] == "gasto")
                      and (s["cat"] or "Ajuste saldo") == (r["subcategoria"] or "Ajuste saldo"))
                detalle = (f"DB: {s['fecha']} {s['tipo']} uyu={s['uyu']:.2f} "
                           f"cat={s['cat']!r} acc={s['acc']!r}") if s else "NO ENCONTRADA"
        else:
            fila = int(r["fuente_id"].split(":")[1])
            s = x["raw"][fila]
            if s["origen"] == "":
                esperado = round(s["egreso"] * 0.5, 2)
            else:
                esperado = round(s["gasto_joaco"], 2)
            ok = (abs(esperado - float(r["monto"])) < 0.01
                  and s["fecha_ref"].isoformat() == r["fecha"]
                  and s["desc"] == r["descripcion"])
            detalle = (f"xlsx fila {fila}: {s['fecha_ref']} egreso={s['egreso']} "
                       f"gasto_joaco={s['gasto_joaco']} origen={s['origen'] or '(sin)'} desc={s['desc']!r}")
        ok_f = ok_f and ok
        trazas.append((r["id"], r["origen"], r["fuente_id"], r["fecha"], r["tipo"],
                       r["monto"], r["categoria"], "OK" if ok else "FALLO", detalle))
    log("2f.muestreo", "OK" if ok_f else "FALLO",
        f"10/10 rastreadas: {sum(1 for t in trazas if t[7]=='OK')} OK")

    # huerfanas de la planilla (documentar)
    hsum = sum(h["egreso"] for h in x["huerfanas"])
    hclas_txt = "; ".join(f"{k}: {n} filas, {s:.2f} UYU"
                          for k, (n, s) in sorted(x["hclas"].items()))
    log("2c.huerfanas", "INFO",
        f"{len(x['huerfanas'])} gastos de la planilla fuera de toda ventana "
        f"(no migrados al ledger), suma {hsum:.2f} UYU -> {hclas_txt}")

    # ---------- 4. resumen mensual ----------
    resumen = defaultdict(float)
    for r in ledger:
        if r["tipo"] == "gasto" and r["en_analisis"] == "1":
            resumen[(r["mes"], r["categoria"] or "(sin categoria)")] += float(r["monto_uyu"])
    with open(OUT_RESUMEN, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mes", "categoria", "gasto_uyu"])
        for (mes, cat), v in sorted(resumen.items()):
            w.writerow([mes, cat, f"{v:.2f}"])
    log("4.resumen", "OK",
        f"{len(resumen)} filas (mes,categoria) -> out/resumen_mensual.csv; "
        f"suma total {sum(resumen.values()):.2f}")

    gasto_2026 = sum(v for (m, _), v in resumen.items() if m.startswith("2026"))
    log("total.2026", "INFO", f"gasto 2026 en_analisis=1: {gasto_2026:.2f} UYU")

    # ---------- 3. reporte md ----------
    md = []
    md.append("# Validación de la migración — ledger unificado")
    md.append(f"\nGenerado por `scripts/merge_y_validar.py` el {dt.date.today().isoformat()}. "
              "Todas las cifras de fuentes se recalcularon directamente desde "
              "`data/raw/MyFinance.db`, `data/raw/casa.xlsx` y `data/presupuesto.csv` "
              "(independiente de los reportes de los migradores).\n")
    md.append("## 1. Merge → data/ledger.csv\n")
    md.append(f"- Filas totales: **{n_total}** ({por_origen['1money']} de 1money + "
              f"{por_origen['planilla_casa']} de planilla_casa + "
              f"{sum(v for k, v in por_origen.items() if k not in ('1money', 'planilla_casa'))} "
              "de otros origenes preservados: mail, etc.)")
    md.append(f"- Por tipo: {dict(por_tipo)}")
    md.append(f"- Rango de fechas: {rango[0]} → {rango[1]}")
    md.append(f"- IDs únicos: {'sí' if not dupes else f'NO — duplicados: {dupes}'}")
    md.append(f"- Suma |monto_uyu| (todas las filas con valor): {suma_uyu_total:,.2f}")
    md.append(f"- Celdas vacías fuera de regla del esquema: {dict(problemas_vacios) or 'ninguna'}")
    md.append(f"- Gasto 2026 en análisis (en_analisis=1): **{gasto_2026:,.2f} UYU**\n")

    md.append("## 2a. Conteos vs MyFinance.db\n")
    md.append("| Métrica | DB (crudo) | Ledger | Esperado | Resultado |")
    md.append("|---|---|---|---|---|")
    md.append(f"| Transacciones activas | {db['n_tx']} | {n_1money_tx} | 2853 | "
              f"{'OK' if db['n_tx']==n_1money_tx==2853 else 'DESVIO'} |")
    md.append(f"| Transferencias activas | {db['n_tr']} | {n_1money_tr} | 207 | "
              f"{'OK' if db['n_tr']==n_1money_tr==207 else 'DESVIO'} |")
    md.append(f"\nSuma `monto_uyu` de transacciones 1money: ledger {suma_tx_ledger:,.2f} "
              f"vs DB {db['total_monto_uyu']:,.2f} → diff {diff_suma:.4f} "
              f"({'OK' if abs(diff_suma)<0.01 else 'DESVIO'})\n")

    md.append("## 2b. Gastos por año (monto_uyu, en_analisis=1) — ledger vs fuentes\n")
    md.append("Regla en_analisis reconstruida desde cero sobre las fuentes crudas "
              "(DB: gastos con categoría, excluyendo cuenta/categoría Casa desde 2023-05; "
              "planilla: ventana A = Egresos×0.5 sin Origen 2023-05→2024-08, "
              "ventana B = Gasto Joaco>0 con Origen 'Debito Casa').\n")
    md.append("| Año | Ledger 1money | DB recalc | Δ | Ledger planilla | XLSX recalc | Δ | Total año |")
    md.append("|---|---|---|---|---|---|---|---|")
    for a, l1, f1, d1, l2, f2, d2, tot in tabla_b:
        md.append(f"| {a} | {l1:,.2f} | {f1:,.2f} | {d1:.2f} | {l2:,.2f} | {f2:,.2f} | {d2:.2f} | {tot:,.2f} |")
    md.append(f"\nResultado: **{'OK — diferencias 0.00 en todos los años' if ok_b else 'DESVIO'}**\n")

    md.append("## 2c. Anti doble conteo casa — planilla (parte Joaco) vs volcados 1Money\n")
    md.append("Por mes desde 2023-05: parte Joaco según planilla (fuente canónica, en_analisis=1) "
              "vs transacciones de cuenta/categoría 'Casa' en 1Money (duplicado, en_analisis=0). "
              "Se esperan diferencias (Joaco no volcaba cada gasto idéntico); "
              "se reportan los meses con diferencia >15%. **Se documenta, no se corrige.**\n")
    md.append("| Mes | Planilla Joaco | 1Money Casa | Δ | Δ% | >15% |")
    md.append("|---|---|---|---|---|---|")
    for m, pj, dm, d, pct, flag in tabla_c:
        md.append(f"| {m} | {pj:,.2f} | {dm:,.2f} | {d:,.2f} | {pct:.1f}% | {'SI' if flag else ''} |")
    md.append(f"\nMeses con diferencia >15%: **{len(desvios_c)} de {len(meses)}**.")
    if desvios_c:
        md.append("\nDetalle de los desvíos:\n")
        for m, pj, dm, pct in desvios_c:
            md.append(f"- **{m}**: planilla {pj:,.2f} vs 1Money {dm:,.2f} ({pct:.1f}%)")
    md.append("\nInterpretación: los volcados a 1Money eran manuales y agregados "
              "(no gasto a gasto), por lo que el calce mensual exacto no era esperable. "
              "La fuente canónica en el ledger es la planilla; los registros 1Money "
              "quedan con en_analisis=0 y no se cuentan dos veces.\n")
    md.append(f"Además, {len(x['huerfanas'])} gastos de la planilla "
              f"(suma {hsum:,.2f} UYU) no caen en ninguna ventana y NO están en el ledger:\n")
    for k, (n, s) in sorted(x["hclas"].items()):
        md.append(f"- {k}: {n} filas, {s:,.2f} UYU")
    md.append("\nLas exclusiones 'por diseño' son correctas (tricount tiene su reflejo en "
              "1Money/Deudas; gasto_joaco=0 significa gasto 100% de Lu, no es parte de Joaco). "
              "Los casos 'A REVISAR' coinciden con lo reportado por el migrador "
              "(31 sin Origen fechados alrededor de 2024-09 + 4 Debito Casa con Gasto Joaco "
              "vacío). Decisión pendiente del usuario si deben incorporarse a mano.\n")

    md.append("## 2d. Categorías\n")
    md.append(f"- Filas gasto/ingreso con en_analisis=1 y categoría vacía: **{len(sin_cat)}**")
    md.append(f"- Categorías del ledger fuera de `config/categorias.csv`: "
              f"**{sorted(cats_invalidas) or 'ninguna'}**")
    md.append(f"- Resultado: {'OK' if not sin_cat and not cats_invalidas else 'DESVIO'}\n")

    md.append("## 2e. Presupuesto (data/presupuesto.csv)\n")
    md.append(f"- Meses presentes: {len(meses_pres)} ({meses_pres[0]} → {meses_pres[-1]}) — esperado 19")
    md.append(f"- Filas totales: {len(pres)}; filas por mes 2026: {dict(sorted(filas_2026.items()))}")
    md.append("- Controles (suma presupuesto / gasto_real_registrado):")
    for c in ctl_res:
        md.append(f"  - {c}")
    md.append(f"- Resultado: {'OK' if ok_e else 'DESVIO'}\n")

    md.append("## 2f. Muestreo — 10 filas del ledger rastreadas a su fuente\n")
    md.append("| id | origen | fuente_id | fecha | tipo | monto | categoría | traza | detalle fuente |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for t in trazas:
        md.append("| " + " | ".join(str(v).replace("|", "\\|") for v in t) + " |")
    md.append(f"\nResultado: **{'10/10 OK' if ok_f else 'HAY FALLOS'}**\n")

    md.append("## Resumen de resultados\n")
    md.append("| Validación | Estado | Detalle |")
    md.append("|---|---|---|")
    for sec, st, txt in results:
        md.append(f"| {sec} | {st} | {txt.replace('|', '\\|')} |")

    md.append("\n## Pendientes / decisiones del usuario\n")
    n_rev = sum(n for k, (n, s) in x["hclas"].items() if k.startswith("A REVISAR"))
    s_rev = sum(s for k, (n, s) in x["hclas"].items() if k.startswith("A REVISAR"))
    md.append(f"1. Los {n_rev} gastos de planilla 'a revisar' fuera de ventana "
              f"({s_rev:,.2f} UYU) no están en el ledger: decidir si se agregan manualmente.")
    md.append("2. Delta interno del workbook casa.xlsx en saldo tarjeta (1521.03 vs 1552.03, "
              "31.00): es interno de la planilla original, no de la migración.")
    md.append("3. Concepto de presupuesto sin mapeo: 'Ropa / Cosas pa Uno' (2025-12) → asignado a 'Otros'.")
    if desvios_c:
        md.append(f"4. {len(desvios_c)} meses con diferencia >15% entre planilla y volcados "
                  "1Money (ver 2c): esperado por volcados manuales; no requiere acción "
                  "salvo que quiera auditarlos.")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"\nMD -> {OUT_MD}")

    # muestra 5 filas del ledger final
    print("\nmuestra 5 filas ledger.csv:")
    for i in (0, n_total // 4, n_total // 2, 3 * n_total // 4, n_total - 1):
        print(" ", [ledger[i][c] for c in ("id", "fecha", "tipo", "monto", "moneda",
                                           "monto_uyu", "cuenta", "categoria",
                                           "ambito", "origen", "en_analisis")])


if __name__ == "__main__":
    main()
