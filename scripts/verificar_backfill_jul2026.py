# -*- coding: utf-8 -*-
"""Verificacion independiente del backfill de mails de julio 2026 (Fase 2).

Chequea, sin tocar nada:
  A. Conteos de procesados.csv vs ledger (origen=mail).
  B. Consistencia ledger<->inbox_raw: cada fila mail reparsea el JSON crudo y
     compara monto/moneda/fecha(cuerpo)/cuenta/tarjeta.
  C. ids duplicados / fuente_id duplicados en el ledger.
  D. Dedupe cruzado 1money: ninguna transferencia mail con match (±1 UYU, ±2d)
     contra transferencias 1money de julio.
  E. Pendientes: toda fila 8261 con ambito_a_confirmar; ids de pendientes existen.
  F. Agregados: gasto julio en_analisis=1 por origen; resumen_mensual 2026-07.
  G. Sospechosos de x100 / /100 (montos con decimales raros o enteros gigantes).
"""
import csv, json, re, sys, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

BASE = Path(__file__).resolve().parent.parent
fallas = []

def falla(msg):
    fallas.append(msg)
    print("FALLA:", msg)

def ok(msg):
    print("OK:", msg)

# --- carga ---
with open(BASE / "data" / "ledger.csv", encoding="utf-8") as f:
    ledger = list(csv.DictReader(f))
with open(BASE / "data" / "staging" / "procesados.csv", encoding="utf-8") as f:
    procesados = list(csv.DictReader(f))
with open(BASE / "data" / "pendientes.csv", encoding="utf-8") as f:
    pendientes = list(csv.DictReader(f))

mail_rows = [r for r in ledger if r["origen"] == "mail"]

# --- A: conteos procesados ---
cr = Counter(p["resultado"] for p in procesados)
print("procesados por resultado:", dict(cr))
esperado = {"insertado": 37, "ignorado_pre_backfill": 4, "recibo_matcheado": 1,
            "insertado_pendiente": 1, "estado_cuenta_pendiente_f3": 1}
if len(procesados) != 44:
    falla(f"procesados.csv tiene {len(procesados)} filas, se esperaban 44")
ins_ids = {p["id_ledger"] for p in procesados if p["resultado"] in ("insertado", "insertado_pendiente")}
ledger_mail_ids = {r["id"] for r in mail_rows}
if ins_ids != ledger_mail_ids:
    falla(f"ids insertados segun procesados != ids mail en ledger: solo_proc={ins_ids-ledger_mail_ids} solo_ledger={ledger_mail_ids-ins_ids}")
else:
    ok(f"{len(ins_ids)} ids insertados en procesados == {len(ledger_mail_ids)} filas origen=mail en ledger")

# --- B: reparsear inbox_raw y comparar ---
TARJETA_CUENTA = {"9071": ("Santander UYU", "personal"), "8261": ("Cuenta Casa", "casa"),
                  "6427": ("Santander Platinum", "personal")}
def parse_importe(s):
    s = s.strip().replace(".", "").replace(",", ".")
    return float(s)

by_fuente = {r["fuente_id"]: r for r in mail_rows}
n_aviso_check = 0
for p in procesados:
    if p["handler"] != "aviso_tarjeta" or p["resultado"] != "insertado":
        continue
    mid = p["message_id"]
    raw = json.loads((BASE / "data" / "inbox_raw" / f"{mid}.json").read_text(encoding="utf-8"))
    body = raw["body_text"]
    m_tar = re.search(r"terminada en\s+(\d{4})", body)
    m_imp = re.search(r"Importe\s+([A-Z]{3})\s+([\d.,]+)", body)
    m_est = re.search(r"Establecimento\s+(.+?)\s*Fecha y hora", body, re.S)
    m_fec = re.search(r"Fecha y hora\s+(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}:\d{2})", body)
    if not (m_tar and m_imp and m_est and m_fec):
        falla(f"{mid}: no pude reparsear el cuerpo"); continue
    tarjeta, moneda, monto = m_tar.group(1), m_imp.group(1), parse_importe(m_imp.group(2))
    fecha_body = datetime.strptime(m_fec.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
    row = by_fuente.get(mid)
    if row is None:
        falla(f"{mid}: sin fila en ledger"); continue
    n_aviso_check += 1
    if abs(float(row["monto"]) - monto) > 0.005:
        falla(f"{mid}: monto ledger {row['monto']} != cuerpo {monto}")
    if row["moneda"] != moneda:
        falla(f"{mid}: moneda ledger {row['moneda']} != cuerpo {moneda}")
    if row["fecha"] != fecha_body:
        falla(f"{mid}: fecha ledger {row['fecha']} != cuerpo {fecha_body}")
    cta, amb = TARJETA_CUENTA[tarjeta]
    if row["cuenta"] != cta or row["ambito"] != amb:
        falla(f"{mid}: cuenta/ambito {row['cuenta']}/{row['ambito']} != esperado {cta}/{amb}")
    if row["tipo"] != "gasto" or row["en_analisis"] != "1":
        falla(f"{mid}: tipo/en_analisis inesperado {row['tipo']}/{row['en_analisis']}")
    # monto_uyu
    if moneda == "UYU":
        if abs(float(row["monto_uyu"]) - monto) > 0.005:
            falla(f"{mid}: monto_uyu {row['monto_uyu']} != {monto}")
    elif moneda == "USD":
        esperado_uyu = round(monto * 40.33, 2)
        if abs(float(row["monto_uyu"]) - esperado_uyu) > 0.01:
            falla(f"{mid}: monto_uyu {row['monto_uyu']} != {esperado_uyu} (USD x 40.33)")
    # id estable
    if row["id"] != hashlib.sha1(("mail" + mid).encode()).hexdigest()[:12]:
        falla(f"{mid}: id {row['id']} no es sha1('mail'+message_id)[:12]")
ok(f"{n_aviso_check} avisos de tarjeta reparseados desde inbox_raw y comparados contra ledger")

# --- C: duplicados ---
cid = Counter(r["id"] for r in ledger)
dups = {k: v for k, v in cid.items() if v > 1}
if dups:
    falla(f"ids duplicados en ledger: {dups}")
else:
    ok(f"sin ids duplicados en ledger ({len(ledger)} filas)")
cfid = Counter((r["origen"], r["fuente_id"]) for r in ledger if r["fuente_id"])
dupf = {k: v for k, v in cfid.items() if v > 1}
if dupf:
    falla(f"(origen,fuente_id) duplicados: {dupf}")

# --- D: dedupe cruzado 1money ---
t1m = [r for r in ledger if r["origen"] == "1money" and r["tipo"] == "transferencia" and r["mes"] == "2026-07"]
tmail = [r for r in mail_rows if r["tipo"] == "transferencia"]
print(f"transferencias jul: 1money={len(t1m)} mail={len(tmail)}")
for m in tmail:
    fm = datetime.strptime(m["fecha"], "%Y-%m-%d")
    for o in t1m:
        fo = datetime.strptime(o["fecha"], "%Y-%m-%d")
        if abs(float(m["monto"]) - float(o["monto"])) <= 1 and abs((fm - fo).days) <= 2:
            falla(f"transferencia mail {m['id']} ({m['monto']} {m['fecha']}) matchea 1money {o['id']} ({o['monto']} {o['fecha']}) y NO fue dedupeada")
ok("ninguna transferencia mail matchea una 1money (±1 UYU, ±2 dias) sin dedupear")
# ademas: aportes a Cuenta Casa contados dos veces?
aportes = [r for r in ledger if r["mes"] == "2026-07" and r["tipo"] == "transferencia" and r["cuenta_destino"] == "Cuenta Casa"]
print("aportes jul a Cuenta Casa:", [(r["origen"], r["fecha"], r["monto"], r["descripcion"]) for r in aportes])

# --- E: pendientes ---
pend_ids = {p["id_ledger"] for p in pendientes if p["id_ledger"]}
faltan = pend_ids - {r["id"] for r in ledger}
if faltan:
    falla(f"pendientes con id_ledger inexistente: {faltan}")
r8261 = [r for r in mail_rows if r["cuenta"] == "Cuenta Casa" and r["tipo"] == "gasto"]
amb_conf = {p["id_ledger"] for p in pendientes if p["motivo"] == "ambito_a_confirmar"}
sin_flag = [r["id"] for r in r8261 if r["id"] not in amb_conf]
if sin_flag:
    falla(f"filas 8261 sin ambito_a_confirmar: {sin_flag}")
else:
    ok(f"las {len(r8261)} filas 8261 tienen ambito_a_confirmar")
estado_pend = {r["id"] for r in mail_rows if r["estado"] == "pendiente"}
no_res = {p["id_ledger"] for p in pendientes if not p["resuelto"]}
if estado_pend != no_res:
    falla(f"estado=pendiente en ledger != pendientes.csv sin resolver: solo_ledger={estado_pend-no_res} solo_pend={no_res-estado_pend}")
else:
    ok(f"{len(no_res)} pendientes sin resolver == filas mail estado=pendiente")

# --- F: agregados ---
def s(rows):
    return round(sum(float(r["monto_uyu"]) for r in rows if r["monto_uyu"]), 2)
gasto_jul = [r for r in ledger if r["mes"] == "2026-07" and r["tipo"] == "gasto" and r["en_analisis"] == "1"]
print(f"gasto jul en_analisis=1: total={s(gasto_jul)} (n={len(gasto_jul)}), por origen:",
      {o: s([r for r in gasto_jul if r['origen'] == o]) for o in {r['origen'] for r in gasto_jul}})
with open(BASE / "out" / "resumen_mensual.csv", encoding="utf-8") as f:
    resumen = [r for r in csv.DictReader(f) if r.get("mes") == "2026-07"]
print("resumen_mensual 2026-07:", resumen)

# --- G: sospechosos x100 ---
for r in mail_rows:
    m = float(r["monto"])
    if m >= 100000 or (0 < m < 1):
        falla(f"monto sospechoso (x100 o /100?): {r['id']} {r['monto']} {r['moneda']} {r['comercio']}")
ok("sin montos sospechosos de x100 / /100 en filas mail")

print()
print("RESULTADO:", "FALLAS=%d" % len(fallas) if fallas else "TODO OK")
sys.exit(1 if fallas else 0)
