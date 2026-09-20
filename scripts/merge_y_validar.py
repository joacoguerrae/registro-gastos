# -*- coding: utf-8 -*-
"""
Fusiona los staging (ledger_1money.csv + ledger_casa.csv) en data/ledger.csv,
valida los INVARIANTES del ledger y produce out/resumen_mensual.csv.

DOS MODOS (el motivo del split, ver docs/auditoria-2026-07.md M2):

  sin flags (lo que corre el pipeline 2x/dia):
    merge -> invariantes -> resumen_mensual.csv. Valida SOLO propiedades que
    deben cumplirse siempre, sin importar cuantos meses de datos haya:
    ids unicos, esquema, categorias del catalogo. EXIT != 0 si alguna falla.

  --migracion (a mano, cuando se toca un migrador):
    ademas revalida la migracion CONTRA LAS FUENTES CRUDAS (MyFinance.db,
    casa.xlsx, data/presupuesto.csv) recalculando todo de forma independiente,
    y escribe docs/validacion-migracion.md.

Por que separados: las validaciones de migracion comparan contra un mundo
CONGELADO (2853 tx / 207 transfers en la DB, exactamente 19 meses de
presupuesto). Correrlas en cada sync hacia que agregar el presupuesto de un mes
nuevo pusiera la corrida en rojo "por diseño". La migracion se valida cuando se
toca un migrador; los invariantes, siempre.

Salidas:
  - data/ledger.csv                (merge ordenado por fecha, id)     [siempre]
  - out/resumen_mensual.csv        (mes, categoria, gasto en_analisis=1) [siempre]
  - docs/validacion-migracion.md   (reporte completo)              [--migracion]

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/merge_y_validar.py [--migracion]
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict

import openpyxl

warnings.filterwarnings("ignore")

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
CUENTAS_CSV = P("config", "cuentas.csv")
RESOLUCIONES_CSV = P("data", "resoluciones.csv")
EXCLUSIONES_CSV = P("config", "cierre_exclusiones.csv")
PARTICIPACIONES_CSV = P("data", "participaciones.csv")
# Conteo por origen de la ultima corrida sana (ver chequear_conteo_por_origen).
# Va en data/staging/ porque es un artefacto del pipeline que igual se
# commitea, como data/staging/salud_pasos.json.
LEDGER_STATS = P("data", "staging", "ledger_stats.json")

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

# Secciones cuyo DESVIO es esperado y esta documentado: NO deben hacer fallar la
# corrida. 2c compara la planilla de casa (fuente canonica) contra los volcados
# manuales y agregados a 1Money: nunca calzaron mes a mes, por eso los volcados
# quedan en en_analisis=0. Ver docs/validacion-migracion.md seccion 2c.
DESVIOS_ESPERADOS = {"2c.doble_conteo"}

# Origenes que NO se pueden regenerar desde ninguna fuente si el ledger se
# pierde: 'mail' se podria rebajar de data/inbox_raw/, pero estado_cuenta
# (PDFs ya conciliados), manual (altas a mano) y splitwise (los exports viejos
# no quedan) no tienen vuelta. Ver chequear_conteo_por_origen().
ORIGENES_NO_REGENERABLES = ("mail", "estado_cuenta", "manual", "splitwise")

# Transferencias sin cuenta_destino que YA existen y no se pueden arreglar desde
# una rama de codigo (las ramas no tocan data/). Son los dos avisos duplicados
# del pago Proyecto PAS USD 530,47 del 1/7/2026: el pago real es e89f07151e87.
# En_analisis=0 NO alcanza -- conciliar_saldos.py:183 no filtra en_analisis, asi
# que sin destino la plata sale de Santander USD y no entra a ningun lado.
# VACIAR esta lista cuando la Fase 1 borre las filas (docs/auditoria-2026-09.md 1.2).
TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS = {
    "cc9d9b3463de": "aviso duplicado del pago Proyecto PAS 1/7/2026; el real es e89f07151e87",
    "e2a0018c20eb": "idem cc9d9b3463de (segundo aviso del mismo pago)",
}

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
        # lineterminator="\n": csv.writer escribe CRLF en TODAS las plataformas y
        # .gitattributes fija *.csv eol=lf -> git avisa en cada add. Ver auditoria
        # 2026-09 SB1.
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=COLUMNS)
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
def filas_planilla_excluidas():
    """Nros de fila de casa.xlsx sacadas del analisis por resolucion (en_analisis=0).

    El recalculo desde el xlsx tiene que aplicar las mismas resoluciones que el
    ledger. Si no, 2b compara un ledger corregido contra una planilla cruda y
    reporta DESVIO por una correccion DELIBERADA. Mismo criterio que
    cargar_overrides_1money() para las filas de 1Money.

    Caso real (auditoria 2026-07 M4): la planilla tiene 7 grupos de gastos con
    fecha/importe/descripcion identicos el mismo dia (ej. Nafta 2.765 dos veces
    el 29/10, Productos Limpieza 545 tres veces el 30/11). Revisados con Joaco el
    16/7: son cargas repetidas, no compras repetidas. Se conserva la primera fila
    de cada grupo y las demas quedan en_analisis=0.
    """
    path = P("data", "resoluciones.csv")
    if not os.path.exists(path):
        return set()
    fuera = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["campo"] == "en_analisis" and r["valor"] == "0":
                fuera.add(r["id_ledger"])
    return fuera


def recalc_xlsx():
    excluidas = filas_planilla_excluidas()
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
    n_a = n_b = n_excl = 0
    huerfanas = []
    for row in raw:
        if not (row["egreso"] and row["egreso"] > 0) or EXCL_PAT.match(row["desc"]):
            continue
        rid = hashlib.sha1(("planilla_casa" + f"fila:{row['fila']}").encode()).hexdigest()[:12]
        if rid in excluidas:
            n_excl += 1      # sacada del analisis por resolucion: no es huerfana
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
                n_excl=n_excl, huerfanas=huerfanas, hclas=dict(hclas),
                raw={r["fila"]: r for r in raw})


# ============================================================
# 4. Invariantes nuevos (auditoria 2026-09, fase 0.2)
# ============================================================
def guardar_stats(datos):
    """Escribe LEDGER_STATS solo si el contenido cambio.

    El archivo esta commiteado y el pipeline hace `git add data` en cada
    corrida: reescribirlo identico generaria un commit de ruido 2x/dia.
    newline="\\n" porque .gitattributes fija LF y en Windows json.dump sobre un
    open() sin newline escribe CRLF (mismo caso que SB1).
    """
    nuevo = json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.path.exists(LEDGER_STATS):
        with open(LEDGER_STATS, encoding="utf-8") as f:
            if f.read() == nuevo:
                return False
    os.makedirs(os.path.dirname(LEDGER_STATS), exist_ok=True)
    with open(LEDGER_STATS, "w", encoding="utf-8", newline="\n") as f:
        f.write(nuevo)
    return True


def chequear_conteo_por_origen(ledger, aceptar_bajas):
    """Gate anti-perdida silenciosa del ledger (auditoria 2026-09 SA1).

    merge() reconstruye las filas de migracion desde data/staging/, pero las de
    mail/estado_cuenta/manual/splitwise las RELEE de su propia salida
    (:103-107). Si data/ledger.csv falta o se trunca, el merge devuelve un
    ledger sin ellas y todo lo demas valida en verde: medido el 20/9, borrar
    data/ledger.csv dejaba 4.525 filas y "validacion OK" con exit 0.
    Desaparecian 436 filas en silencio y 150 de ellas no se regeneran desde
    ninguna fuente.

    La defensa es acordarse de cuantas habia: si un origen no regenerable baja,
    esto sale DESVIO -> exit != 0 -> el workflow corta ANTES del commit
    (sync.yml:96-102) y el ledger mutilado nunca llega a master.
    """
    actual = dict(sorted(Counter(r["origen"] for r in ledger).items()))
    datos = {"por_origen": actual, "total": len(ledger)}
    resumen = ", ".join(f"{o}={actual.get(o, 0)}" for o in ORIGENES_NO_REGENERABLES)

    previo = None
    if os.path.exists(LEDGER_STATS):
        with open(LEDGER_STATS, encoding="utf-8") as f:
            previo = json.load(f).get("por_origen", {})

    if not previo:
        # Bootstrap: el invariante tiene que poder nacer sin commitear datos
        # desde una rama de codigo (las ramas no tocan data/). La primera
        # corrida siembra el archivo; de la segunda en adelante el gate esta
        # armado. Es INFO a proposito: sembrar no es un problema.
        # `not previo` y no `previo is None` para que un archivo vacio o sin
        # `por_origen` (editado a mano) se siembre DICIENDOLO, en vez de
        # comparar contra {} y dar OK con el gate desarmado.
        #
        # ...pero NO se siembra desde un ledger que ya esta roto. Sin esto, el
        # gate se desarma borrando el archivo: la corrida siguiente lo siembra
        # con lo que haya (medido el 20/9: con ledger.csv borrado sembraba
        # mail=0/manual=0 y pasaba en verde) y la perdida queda bendecida como
        # la nueva referencia. Un ledger real SIEMPRE tiene filas de mail.
        if not any(actual.get(o, 0) for o in ORIGENES_NO_REGENERABLES):
            log("1.conteo_origen", "DESVIO",
                f"me pediste sembrar data/staging/ledger_stats.json desde un ledger de "
                f"{len(ledger)} filas SIN NINGUNA fila de {list(ORIGENES_NO_REGENERABLES)} "
                f"({resumen}). Eso no es un ledger sano: son las filas que no se "
                "regeneran desde ninguna fuente. QUE HACER: recupera el ledger "
                "(`git checkout -- data/ledger.csv`) y volve a correr. Si de verdad "
                "queres sembrar asi, corre con --aceptar-bajas")
            if not aceptar_bajas:
                return
        guardar_stats(datos)
        log("1.conteo_origen", "INFO",
            f"data/staging/ledger_stats.json sembrado: {len(ledger)} filas, "
            f"origenes no regenerables {resumen}. Desde la proxima corrida, que "
            "alguno de esos baje es DESVIO")
        return

    bajas = [(o, previo.get(o, 0), actual.get(o, 0))
             for o in ORIGENES_NO_REGENERABLES
             if actual.get(o, 0) < previo.get(o, 0)]
    detalle = "; ".join(f"{o}: {p} -> {a} (faltan {p - a} filas)" for o, p, a in bajas)

    if bajas and not aceptar_bajas:
        log("1.conteo_origen", "DESVIO",
            f"bajo el conteo de origenes que no se pueden regenerar: {detalle}"
            ". QUE HACER: (a) si la baja fue deliberada (borraste filas en una "
            "revision), volve a correr con --aceptar-bajas: el conteo nuevo queda "
            "como referencia. (b) si no la esperabas, data/ledger.csv se trunco o "
            "se corrompio -- merge() conserva esas filas releyendo el ledger "
            "anterior (no hay otra fuente), asi que lo que falta ahi se pierde "
            "para siempre. Recuperalo con `git checkout -- data/ledger.csv`, "
            "volve a correr, y NO commitees hasta que esto vuelva a dar OK")
        return

    cambio = guardar_stats(datos)
    if bajas:
        log("1.conteo_origen", "INFO",
            f"--aceptar-bajas: se aceptan las bajas ({detalle}) y "
            f"data/staging/ledger_stats.json queda con {resumen}")
    else:
        log("1.conteo_origen", "OK",
            f"origenes no regenerables: {resumen} "
            f"(referencia {'actualizada' if cambio else 'sin cambios'})")


def chequear_cuentas(ledger):
    """cuenta/cuenta_destino tienen que estar en config/cuentas.csv (SM5).

    Un typo entra callado y deforma saldos: la cuenta desconocida simplemente no
    matchea y la plata "se pierde" (caso real: 'Santander Platinum USD', 118,34 +
    530,47 USD que nunca bajaron la deuda de la tarjeta). Se exime
    estado=pendiente: son filas de la bandeja (data/pendientes.csv) que todavia
    no paso nadie -- procesar_mails.py:676 escribe el titular CRUDO del mail en
    cuenta_destino ('Lu pesos') y eso pasa ~2 veces por mes. Mismo criterio que
    1.vacios y 2d.categorias.
    """
    with open(CUENTAS_CSV, encoding="utf-8-sig") as f:
        cuentas_validas = {r["cuenta"].strip() for r in csv.DictReader(f)
                           if (r["cuenta"] or "").strip()}

    # Guard anti-"arreglo" equivocado: config/cuentas.csv lleva la cuenta BASE y
    # la moneda va en la columna `moneda` del ledger. conciliar_saldos.py resuelve
    # los nombres multi-moneda de config/saldos_iniciales.csv ('Santander Platinum
    # USD', 'Deudas USD', 'Efectivo USD') sacandoles el sufijo CONTRA este catalogo
    # (cuenta_ledger, :108-119): si el sufijo existe aca, deja de traducir y la
    # cuenta base pierde todos sus movimientos. Verificado 20/9 sobre una copia:
    # catalogar 'Santander Platinum USD' deja la tarjeta con 2 movimientos de 20.
    # ' EUR' no lo traduce nadie todavia, pero seria igual de ambiguo.
    sufijos_prohibidos = sorted(
        c for c in cuentas_validas
        if c.rsplit(" ", 1)[-1] in ("USD", "EUR") and c.rsplit(" ", 1)[0] in cuentas_validas)
    log("1.cuentas_catalogo", "OK" if not sufijos_prohibidos else "FALLO",
        f"config/cuentas.csv: {len(cuentas_validas)} cuentas; nombres '<cuenta base> "
        f"<moneda>' prohibidos: {sufijos_prohibidos or 'ninguno'}"
        + ("" if not sufijos_prohibidos else
           ". QUE HACER: borra esas filas de config/cuentas.csv. El catalogo lleva "
           "solo cuentas BASE; la moneda vive en la columna `moneda` del ledger y "
           "config/saldos_iniciales.csv usa el sufijo como clave de saldo "
           "multi-moneda -- esa asimetria es deliberada (conciliar_saldos.py:16-19)."))

    fuera = Counter()
    ejemplo = {}
    for r in ledger:
        if r["estado"] == "pendiente":
            continue
        for campo in ("cuenta", "cuenta_destino"):
            v = (r[campo] or "").strip()
            if v and v not in cuentas_validas:
                fuera[(campo, v)] += 1
                ejemplo.setdefault((campo, v), r["id"])
    detalle = "; ".join(
        f"{campo}={valor!r} en {n} fila(s) (ej. id {ejemplo[(campo, valor)]})"
        for (campo, valor), n in sorted(fuera.items(), key=lambda kv: -kv[1]))
    log("1.cuentas", "OK" if not fuera else "DESVIO",
        f"cuenta/cuenta_destino fuera de config/cuentas.csv: {detalle or 'ninguna'}"
        + ("" if not fuera else
           ". QUE HACER: (a) si es una cuenta real nueva, agregale una fila a "
           "config/cuentas.csv con el formato "
           "cuenta,alias_1money,tarjetas,moneda,tipo,activa,notas -- `cuenta` tiene "
           "que ser el string EXACTO del ledger, `tipo` uno de "
           "banco/conjunta/virtual/tarjeta_credito/efectivo/inversion, y `notas` una "
           "linea que explique que es. (b) si es un typo o el nombre de una persona "
           "(el parser de transferencias escribe el titular crudo del mail), "
           "corregilo en data/resoluciones.csv: id_ledger,campo,valor,nota,fecha. "
           "NUNCA agregues un nombre con sufijo de moneda ('Santander Platinum USD'): "
           "va la cuenta BASE, conciliar_saldos.py resuelve el sufijo solo."))


def chequear_transferencias(ledger):
    """Toda transferencia necesita cuenta_destino, con allowlist de legado (SB7)."""
    sin_destino = [r for r in ledger
                   if r["tipo"] == "transferencia"
                   and not (r["cuenta_destino"] or "").strip()
                   and r["estado"] != "pendiente"
                   and r["id"] not in TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS]
    log("1.transfer_destino", "OK" if not sin_destino else "DESVIO",
        f"transferencias sin cuenta_destino: {len(sin_destino)}"
        + ("" if not sin_destino else
           " -> " + ", ".join(f"{r['id']} ({r['fecha']} {r['monto']} {r['moneda']} "
                              f"{r['cuenta']} {r['descripcion'][:40]!r})"
                              for r in sin_destino[:10])
           + ". QUE HACER: una transferencia sin destino resta de su cuenta origen "
             "y no suma en ningun lado -- conciliar_saldos.py NO filtra en_analisis, "
             "asi que deforma el saldo aunque este en 0. Pone el destino en "
             "data/resoluciones.csv (id_ledger,cuenta_destino,<cuenta>,nota,fecha); "
             "si en realidad es un gasto, cambiale el `tipo` en la misma resolucion."))

    # la allowlist no se puede pudrir: si una fila dejo de existir o ya tiene
    # destino, hay que sacarla de TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS
    por_id = {r["id"]: r for r in ledger}
    obsoletos = [i for i in TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS
                 if i not in por_id
                 or por_id[i]["tipo"] != "transferencia"
                 or (por_id[i]["cuenta_destino"] or "").strip()]
    log("1.transfer_destino_legado", "OK" if not obsoletos else "DESVIO",
        f"{len(TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS)} transferencias sin destino "
        f"toleradas (se borran en la Fase 1): "
        + ", ".join(f"{i}: {m}" for i, m in sorted(TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS.items()))
        + ("" if not obsoletos else
           f". YA NO APLICAN: {obsoletos} -- sacalos de "
           "TRANSFERENCIAS_SIN_DESTINO_CONOCIDAS en scripts/merge_y_validar.py"))


def lint_resoluciones(ledger):
    """Lint de data/resoluciones.csv y de los dos archivos que indexan por id
    de ledger (auditoria 2026-09 SM4).

    Son 756 decisiones humanas irrecuperables que nadie valida: hoy una
    resolucion con un `campo` mal escrito se descarta en SILENCIO
    (merge_y_validar.py:128 y procesar_mails.py:877 filtran por COLUMNS sin
    avisar), y quien la escribio cree que corrigio la fila.
    """
    ids_ledger = {r["id"] for r in ledger}
    with open(RESOLUCIONES_CSV, encoding="utf-8-sig", newline="") as f:
        resoluciones = list(csv.DictReader(f))

    # (a) campo desconocido: es un ERROR real, la correccion no se aplica nunca
    desconocidos = Counter(r["campo"] for r in resoluciones
                           if r["campo"] not in COLUMNS and r["campo"] != "motivo_resuelto")
    log("1.resoluciones_campo", "OK" if not desconocidos else "DESVIO",
        f"{len(resoluciones)} resoluciones; `campo` fuera del esquema: "
        f"{dict(desconocidos) or 'ninguno'}"
        + ("" if not desconocidos else
           ". QUE HACER: esas filas NO se aplican -- el merge las descarta sin "
           "avisar. Corregi el `campo` a una columna del ledger ("
           + ", ".join(COLUMNS) + ") o a 'motivo_resuelto' (que cierra un "
           "pendiente por (id_ledger, motivo)). Si era un experimento, borra la fila."))

    # (b) claves repetidas: gana la ULTIMA por orden de archivo (merge_y_validar
    # :126-132 y procesar_mails.py:874-880 arman un dict). Hoy es deliberado
    # (correcciones sucesivas de la misma fila), por eso es informativo y no
    # corta. motivo_resuelto queda afuera a proposito: no es un override sino un
    # SET de (id, motivo), y repetirlo con motivos distintos es lo normal.
    valores = defaultdict(list)
    for r in resoluciones:
        if r["campo"] == "motivo_resuelto":
            continue
        valores[(r["id_ledger"], r["campo"])].append(r["valor"])
    repetidas = {k: v for k, v in valores.items() if len(set(v)) > 1}
    ejemplos = "; ".join(f"{i}.{c}: {' -> '.join(v)}"
                         for (i, c), v in sorted(repetidas.items())[:3])
    log("1.resoluciones_repetidas", "OK" if not repetidas else "INFO",
        f"claves (id_ledger, campo) con valores distintos: {len(repetidas)}"
        + (f" (gana la ultima del archivo) -> {ejemplos}" if repetidas else ""))

    # (c) ids que no existen en el ledger. Informativo: hay legado de ids
    # recalculados (un cambio de fuente_id cambia el hash) y hacer fallar la
    # corrida por eso dejaria el pipeline rojo 2x/dia sin que nadie pueda
    # arreglarlo desde una rama de codigo. La consecuencia real es solo que la
    # fila no hace nada.
    huerfanos = []
    for path, etiqueta in ((RESOLUCIONES_CSV, "data/resoluciones.csv"),
                           (EXCLUSIONES_CSV, "config/cierre_exclusiones.csv"),
                           (PARTICIPACIONES_CSV, "data/participaciones.csv")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            filas = [r for r in csv.DictReader(f) if r["id_ledger"] not in ids_ledger]
        huerfanos.append((etiqueta, len(filas), len({r["id_ledger"] for r in filas})))
    total_huerfanos = sum(n for _, n, _ in huerfanos)
    log("1.ids_huerfanos", "OK" if not total_huerfanos else "INFO",
        "; ".join(f"{et}: {n} fila(s) sobre {k} id(s) inexistente(s)"
                  for et, n, k in huerfanos)
        + ("" if not total_huerfanos else
           ". QUE HACER: no hacen nada (se aplican por id y ese id ya no esta). "
           "Si el id cambio porque cambio su fuente_id, re-apuntala al id nuevo; "
           "si la fila se borro, borra tambien su resolucion. Ojo en "
           "participaciones.csv: un id huerfano ahi significa que un gasto de casa "
           "se esta repartiendo por coeficiente en vez de por la fraccion pactada."))


# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--migracion", action="store_true",
                    help="ademas revalida la migracion contra las fuentes crudas "
                         "(MyFinance.db, casa.xlsx) y escribe docs/validacion-migracion.md")
    ap.add_argument("--aceptar-bajas", action="store_true",
                    help="acepta que bajen las filas de un origen no regenerable "
                         "(mail/estado_cuenta/manual/splitwise) y re-siembra "
                         "data/staging/ledger_stats.json. Solo despues de un "
                         "borrado DELIBERADO: si la baja no la hiciste vos, el "
                         "ledger se corrompio y esto la da por buena")
    args = ap.parse_args()

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

    # Va JUSTO despues del merge y antes de todo lo demas: merge() ya reescribio
    # data/ledger.csv, asi que el archivo de conteos tiene que quedar describiendo
    # exactamente ese ledger. Si hay baja no escribe nada -- la referencia es la
    # memoria de lo que TENIA que estar y debe sobrevivir a la corrida mala.
    chequear_conteo_por_origen(ledger, args.aceptar_bajas)

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

    # ---------- 1.cuentas / 1.transfer_destino / resoluciones (INVARIANTES) ----------
    # Corren sobre `ledger`, que ya tiene las resoluciones aplicadas por merge().
    chequear_cuentas(ledger)
    chequear_transferencias(ledger)
    lint_resoluciones(ledger)

    # ---------- 2d. categorias (INVARIANTE: corre siempre) ----------
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

    # ---------- 4. resumen mensual (PERSONAL: solo la parte de Joaco) ----------
    # out/resumen_mensual.csv alimenta el presupuesto PERSONAL de Joaco (Sheet
    # gastos_resumen_mensual y presupuesto_vs_real.csv). Un gasto ambito=casa
    # es compartido con Lu: contar el monto_uyu completo duplicaria su parte
    # dentro del presupuesto de Joaco. Se aplica el mismo coeficiente/
    # participacion que usa el dashboard de casa (out/casa_mensual.csv) para
    # quedarse solo con la porcion de Joaco.
    ingresos_mes = {}
    with open(P("config", "ingresos.csv"), encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            tot_ing = float(r["ingreso_lu"]) + float(r["ingreso_joaco"])
            ingresos_mes[r["mes"]] = float(r["ingreso_joaco"]) / tot_ing

    def coef_joaco(mes):
        if mes in ingresos_mes:
            return ingresos_mes[mes]
        previos = sorted(m for m in ingresos_mes if m <= mes)
        return ingresos_mes[previos[-1]] if previos else 0.5

    participaciones = {}
    part_path = P("data", "participaciones.csv")
    if os.path.exists(part_path):
        with open(part_path, encoding="utf-8-sig", newline="") as f:
            participaciones = {r["id_ledger"]: float(r["frac_joaco"])
                               for r in csv.DictReader(f)}

    resumen = defaultdict(float)
    for r in ledger:
        if r["tipo"] != "gasto" or r["en_analisis"] != "1":
            continue
        monto = float(r["monto_uyu"])
        # OJO: las filas origen=planilla_casa YA son la parte de Joaco -- el
        # migrador guardo "Gasto Joaco" (o Egresos/2 en la ventana vieja), no el
        # egreso total. Aplicarles el coeficiente las divide DOS VECES y
        # subestima todo su gasto historico hasta jun-2026 (ej. Vivienda de junio:
        # total 36.582, su parte 23.958, y el resumen mostraba 15.691).
        # Lo detecto Joaco el 2/8/2026 al notar que los gastos comunes "saltaban"
        # de 15.617 a 23.768 en julio: no era un salto, era comparar su parte
        # contra el total. Las filas de mail/estado/manual SI traen el total de la
        # casa y hay que dividirlas.
        if r["ambito"] == "casa" and r["origen"] != "planilla_casa":
            frac = participaciones.get(r["id"], coef_joaco(r["mes"]))
            monto *= frac
        resumen[(r["mes"], r["categoria"] or "(sin categoria)")] += monto
    with open(OUT_RESUMEN, "w", encoding="utf-8", newline="") as f:
        # lineterminator="\n": ver el comentario del writer del ledger (SB1).
        w = csv.writer(f, lineterminator="\n")
        # "fecha" (YYYY-MM-01, ISO) ademas de "mes" (YYYY-MM): Looker Studio
        # reconoce fechas ISO con guion como tipo Fecha automaticamente, pero
        # "YYYY-MM" con guion NO calza con su formato "Año Mes" (que exige
        # YYYYMM sin separador) -> "mes" quedaba ilegible como fecha en Looker.
        w.writerow(["mes", "fecha", "categoria", "gasto_uyu"])
        for (mes, cat), v in sorted(resumen.items()):
            w.writerow([mes, f"{mes}-01", cat, f"{v:.2f}"])
    log("4.resumen", "OK",
        f"{len(resumen)} filas (mes,categoria) -> out/resumen_mensual.csv "
        f"(PERSONAL: casa dividida por coeficiente/participacion); "
        f"suma total {sum(resumen.values()):.2f}")

    gasto_2026 = sum(v for (m, _), v in resumen.items() if m.startswith("2026"))
    log("total.2026", "INFO", f"gasto 2026 en_analisis=1: {gasto_2026:.2f} UYU")

    # ---------- validacion de la migracion (SOLO --migracion) ----------
    if args.migracion:
        validar_migracion(ledger, dict(
            n_total=n_total, por_origen=por_origen, por_tipo=por_tipo, rango=rango,
            dupes=dupes, problemas_vacios=problemas_vacios,
            suma_uyu_total=suma_uyu_total, gasto_2026=gasto_2026,
            sin_cat=sin_cat, cats_invalidas=cats_invalidas))

    # muestra 5 filas del ledger final
    print("\nmuestra 5 filas ledger.csv:")
    for i in (0, n_total // 4, n_total // 2, 3 * n_total // 4, n_total - 1):
        print(" ", [ledger[i][c] for c in ("id", "fecha", "tipo", "monto", "moneda",
                                           "monto_uyu", "cuenta", "categoria",
                                           "ambito", "origen", "en_analisis")])

    # ---------- exit code: los invariantes son BLOQUEANTES ----------
    # Antes este script salia SIEMPRE con 0 y el workflow decidia por grep sobre
    # el stdout -- que ademas no miraba FALLO, el estado reservado para ids
    # duplicados (auditoria 2026-07 A1): un ledger corrupto se commiteaba en
    # verde. Ahora el codigo de salida es la verdad y el pipeline se detiene solo.
    fallos = [(s, t) for s, st, t in results if st == "FALLO"]
    desvios = [(s, t) for s, st, t in results if st == "DESVIO"
               and s not in DESVIOS_ESPERADOS]
    if fallos or desvios:
        print("\n=== VALIDACION NO PASA ===")
        for s, t in fallos:
            print(f"  [FALLO]  {s}: {t}")
        for s, t in desvios:
            print(f"  [DESVIO] {s}: {t}")
        return 1
    esperados = sorted({s for s, st, _ in results
                        if st == "DESVIO" and s in DESVIOS_ESPERADOS})
    nota = f"; desvios documentados: {', '.join(esperados)}" if esperados else ""
    print(f"\nvalidacion OK ({len(results)} chequeos{nota})")
    return 0


def validar_migracion(ledger, ctx):
    """Revalida la migracion contra las fuentes crudas. NO corre en el pipeline:
    compara contra un mundo congelado (conteos de MyFinance.db, cantidad de meses
    de presupuesto) que agregar datos nuevos rompe por diseño."""
    n_total = ctx["n_total"]
    por_origen, por_tipo, rango, dupes = ctx["por_origen"], ctx["por_tipo"], ctx["rango"], ctx["dupes"]
    problemas_vacios, suma_uyu_total = ctx["problemas_vacios"], ctx["suma_uyu_total"]
    gasto_2026, sin_cat, cats_invalidas = ctx["gasto_2026"], ctx["sin_cat"], ctx["cats_invalidas"]

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
    # ajuste esperado: resoluciones que modifican a mano monto_uyu de una fila
    # origen=1money (ej. splits que reducen el gasto a la parte propia) restan
    # deliberadamente del total frente a la DB cruda — no es un desvio real.
    ajuste_resoluciones = 0.0
    ids_1money = {r["fuente_id"]: r["id"] for r in ledger if r["origen"] == "1money"}
    res_path = P("data", "resoluciones.csv")
    if os.path.exists(res_path):
        with open(res_path, encoding="utf-8-sig", newline="") as f:
            for res in csv.DictReader(f):
                if res["campo"] != "monto_uyu":
                    continue
                fila_actual = next((r for r in ledger if r["id"] == res["id_ledger"]
                                    and r["origen"] == "1money"), None)
                if not fila_actual:
                    continue
                original = db["tx_rows"].get(fila_actual["fuente_id"], {}).get("uyu")
                if original is not None:
                    ajuste_resoluciones += float(res["valor"]) - original
    diff_ajustado = diff_suma - ajuste_resoluciones
    log("2a.suma_uyu", "OK" if abs(diff_ajustado) < 0.01 else "DESVIO",
        f"suma monto_uyu transacciones 1money: ledger {suma_tx_ledger:.2f} vs DB "
        f"{db['total_monto_uyu']:.2f} (diff crudo {diff_suma:.4f}, ajuste por "
        f"resoluciones {ajuste_resoluciones:.4f}, diff neto {diff_ajustado:.4f})")

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
        f"({x['n_excl']} filas de planilla excluidas por resolucion, aplicadas a los dos lados) "
        + "; ".join(f"{a}: ledger {t:.2f} (1money d={d1:.2f}, planilla d={d2:.2f})"
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

    # newline="\n": el .md esta commiteado y .gitattributes lo fija en LF; sin
    # esto una corrida en Windows lo reescribe entero en CRLF (SB1).
    with open(OUT_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")
    print(f"\nMD -> {OUT_MD}")


if __name__ == "__main__":
    sys.exit(main())
