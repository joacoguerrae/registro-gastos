# -*- coding: utf-8 -*-
"""
Fase 3 — Sync Splitwise (cuenta Deudas), SOLO LECTURA (docs/fase3.md seccion 1).

Lee la API v3.0 de Splitwise (Bearer key en config/secrets.local.json,
clave splitwise_api_key — NUNCA se imprime ni loguea) y produce:

  - data/splitwise.csv        (crudo por gasto traido de la API)
  - data/ledger.csv           (filas origen=splitwise, fuente_id=sw:<id>,
                               dedupe por fuente_id; reescrito ordenado)
  - data/pendientes.csv       (sin_categoria / split_a_cargar /
                               transferencia_a_clasificar / tc_estimado)

Reglas (contrato fase3.md):
  1. otro pago y yo debo (owed>0, paid==0, payment=false) -> gasto cuenta=Deudas,
     monto=mi owed_share, ambito=split, categoria por reglas_comercio.csv.
  2. pague YO (paid>0, payment=false) -> NO se inserta (el cargo ya entro por el
     aviso de tarjeta); pendiente split_a_cargar si no hay fila hermana
     <fuente_id>-split para un cargo que matchee (total +-1%, fecha +-3 dias).
  3. payment=true: me pagaron -> transferencia Deudas -> Santander UYU
     (pendiente transferencia_a_clasificar); pague yo -> transferencia
     Santander UYU -> Deudas (dedupe vs transferencias mail/1money existentes
     con destino Deudas, +-1 en monto, +-2 dias).

Al final compara el saldo neto de get_friends contra el saldo calculado de la
cuenta Deudas (saldos_iniciales + movimientos posteriores al corte), por moneda.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/sync_splitwise.py
"""
import csv
import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter, defaultdict

import requests

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

LEDGER = P("data", "ledger.csv")
PENDIENTES = P("data", "pendientes.csv")
SPLITWISE_CSV = P("data", "splitwise.csv")
RESOLUCIONES = P("data", "resoluciones.csv")
SECRETS = P("config", "secrets.local.json")
REGLAS_CSV = P("config", "reglas_comercio.csv")
PARAMETROS = P("config", "parametros.json")
SALDOS_INICIALES = P("config", "saldos_iniciales.csv")

API = "https://secure.splitwise.com/api/v3.0"
DATED_AFTER = "2026-07-01T00:00:00Z"

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]
RES_COLS = ["id_ledger", "campo", "valor", "nota", "fecha"]
SW_COLS = ["id", "fecha", "descripcion", "grupo", "moneda", "costo_total",
           "mi_paid_share", "mi_owed_share", "creado_por", "es_pago"]


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


def cargar_reglas():
    reglas = []
    with open(REGLAS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            reglas.append((re.compile(r["patron"], re.I),
                           r["categoria"].strip(),
                           (r.get("ambito_override") or "").strip()))
    return reglas


def categorizar(texto, reglas):
    for pat, cat, amb in reglas:
        if pat.search(texto):
            return cat, amb, True
    return "", "", False


def hid(fuente_id):
    return hashlib.sha1(("splitwise" + fuente_id).encode()).hexdigest()[:12]


def fmt(x):
    return f"{x:.2f}"


def api_get(session, path, **params):
    r = session.get(f"{API}/{path}", params=params or None, timeout=30)
    r.raise_for_status()
    return r.json()


def traer_gastos(session):
    """Pagina get_expenses con offset. Devuelve TODOS (incluye borrados,
    que se filtran despues para poder loguearlos)."""
    out, offset, limit = [], 0, 100
    while True:
        data = api_get(session, "get_expenses",
                       dated_after=DATED_AFTER, limit=limit, offset=offset)
        chunk = data.get("expenses", [])
        out.extend(chunk)
        if len(chunk) < limit:
            return out
        offset += limit


def main():
    # Env var (GitHub Actions) tiene prioridad; si no, archivo local (corridas en la PC).
    api_key = os.environ.get("SPLITWISE_API_KEY")
    if not api_key:
        with open(SECRETS, encoding="utf-8") as f:
            api_key = json.load(f)["splitwise_api_key"]
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"

    reglas = cargar_reglas()
    with open(PARAMETROS, encoding="utf-8") as f:
        params = json.load(f)
    tc = {"USD": params["tc_usd_uyu"], "EUR": params["tc_eur_uyu"]}

    ledger = leer_csv(LEDGER, COLUMNS)
    pendientes = leer_csv(PENDIENTES, PEND_COLS)

    # resoluciones: (id, motivo) resueltos no se regeneran; overrides de columnas
    resoluciones = leer_csv(RESOLUCIONES, RES_COLS)
    resueltos = {(r["id_ledger"], r["valor"]) for r in resoluciones
                 if r["campo"] == "motivo_resuelto"}
    overrides = defaultdict(dict)
    for r in resoluciones:
        if r["campo"] in COLUMNS:
            overrides[r["id_ledger"]][r["campo"]] = r["valor"]

    fuente_ids_existentes = {r["fuente_id"] for r in ledger}
    pend_existentes = {(p["id_ledger"], p["motivo"]) for p in pendientes}
    hoy = dt.date.today().isoformat()

    # --- API ---
    me = api_get(session, "get_current_user")["user"]
    mi_id = me["id"]
    grupos = {g["id"]: g["name"]
              for g in api_get(session, "get_groups").get("groups", [])}
    crudos = traer_gastos(session)

    stats = Counter()
    sw_rows, nuevos, pend_nuevos = [], [], []

    def pend(id_ledger, motivo, detalle):
        if (id_ledger, motivo) in pend_existentes or (id_ledger, motivo) in resueltos:
            return
        pend_existentes.add((id_ledger, motivo))
        pend_nuevos.append(dict(id_ledger=id_ledger, motivo=motivo,
                                detalle=detalle, fecha_alta=hoy, resuelto=""))

    # cargos (gastos) del ledger y sus filas hermanas -split, para la regla 2
    hermanas = {r["fuente_id"]: r for r in ledger
                if r["fuente_id"].endswith("-split")}
    cargos = [r for r in ledger
              if r["tipo"] == "gasto" and r["origen"] in ("mail", "1money", "manual")
              and r["fecha"] >= "2026-06-15"]
    # transferencias existentes con destino Deudas (dedupe de pagos, regla 3)
    transf_deudas = [r for r in ledger
                     if r["tipo"] == "transferencia" and r["origen"] in ("mail", "1money")
                     and r["cuenta_destino"] == "Deudas"]

    for e in crudos:
        eid = str(e["id"])
        fecha = (e.get("date") or "")[:10]
        desc = (e.get("description") or "").strip()
        moneda = e.get("currency_code") or ""
        costo = float(e.get("cost") or 0)
        es_pago = bool(e.get("payment"))
        creado = ((e.get("created_by") or {}).get("first_name") or "").strip()
        grupo = grupos.get(e.get("group_id"), "") if e.get("group_id") else ""
        mi_paid = mi_owed = 0.0
        for u in e.get("users", []):
            if u.get("user_id") == mi_id or (u.get("user") or {}).get("id") == mi_id:
                mi_paid = float(u.get("paid_share") or 0)
                mi_owed = float(u.get("owed_share") or 0)

        if e.get("deleted_at"):
            stats["omitido_borrado"] += 1
            continue

        sw_rows.append(dict(id=eid, fecha=fecha, descripcion=desc, grupo=grupo,
                            moneda=moneda, costo_total=fmt(costo),
                            mi_paid_share=fmt(mi_paid), mi_owed_share=fmt(mi_owed),
                            creado_por=creado, es_pago="1" if es_pago else "0"))

        fuente_id = f"sw:{eid}"
        rid = hid(fuente_id)

        if mi_paid == 0 and mi_owed == 0:
            stats["omitido_no_me_involucra"] += 1
            continue

        def fila_base():
            r = {c: "" for c in COLUMNS}
            r.update(id=rid, fecha=fecha, mes=fecha[:7], moneda=moneda,
                     origen="splitwise", fuente_id=fuente_id,
                     estado="confirmado", en_analisis="1")
            return r

        if not es_pago and mi_owed > 0 and mi_paid == 0:
            # ---- regla 1: otro pago, yo debo -> gasto cuenta=Deudas
            if fuente_id in fuente_ids_existentes:
                stats["omitido_ya_en_ledger"] += 1
                continue
            categoria, _, _ = categorizar(desc, reglas)
            r = fila_base()
            r.update(tipo="gasto", monto=fmt(mi_owed), cuenta="Deudas",
                     categoria=categoria, descripcion=desc, ambito="split",
                     subcategoria=((e.get("category") or {}).get("name") or ""))
            if moneda == "UYU":
                r["monto_uyu"] = fmt(mi_owed)
            else:
                r["monto_uyu"] = fmt(round(mi_owed * tc.get(moneda, 0), 2))
                pend(rid, "tc_estimado",
                     f"{moneda} {fmt(mi_owed)} x tc {tc.get(moneda)} = {r['monto_uyu']} UYU")
            if not categoria:
                pend(rid, "sin_categoria",
                     f"gasto Splitwise {desc!r} ({grupo or 'sin grupo'}) sin regla")
            nuevos.append(r)
            stats["insertado_gasto_debo"] += 1

        elif not es_pago and mi_paid > 0:
            # ---- regla 2: pague YO -> NO se inserta (ya entro por tarjeta)
            stats["omitido_pague_yo"] += 1
            f0 = dt.date.fromisoformat(fecha)
            match, con_hermana = None, False
            for c in cargos:
                if c["moneda"] != moneda:
                    continue
                if abs((dt.date.fromisoformat(c["fecha"]) - f0).days) > 3:
                    continue
                h = hermanas.get(c["fuente_id"] + "-split")
                total = float(c["monto"]) + (float(h["monto"]) if h else 0.0)
                if abs(total - costo) <= costo * 0.01:
                    match, con_hermana = c, h is not None
                    break
            if match and con_hermana:
                stats["pague_yo_split_ya_cargado"] += 1
            else:
                stats["pague_yo_pendiente_split"] += 1
                if match:
                    pend(match["id"], "split_a_cargar",
                         f"Splitwise sw:{eid} {desc!r} {moneda} {fmt(costo)} ({fecha}): "
                         f"yo pague {fmt(mi_paid)}, mi parte {fmt(mi_owed)} -> "
                         f"ajustar cargo {match['id']} y crear fila hermana -split")
                else:
                    pend(rid, "split_a_cargar",
                         f"Splitwise sw:{eid} {desc!r} {moneda} {fmt(costo)} ({fecha}): "
                         f"yo pague {fmt(mi_paid)}, mi parte {fmt(mi_owed)} — "
                         "SIN cargo del ledger que matchee (total +-1%, +-3 dias)")

        elif es_pago:
            # ---- regla 3: pagos (settle)
            if fuente_id in fuente_ids_existentes:
                stats["omitido_ya_en_ledger"] += 1
                continue
            if mi_owed > 0:      # me pagaron (el pago "me lo debe" = baja lo que me deben)
                r = fila_base()
                r.update(tipo="transferencia", monto=fmt(mi_owed),
                         cuenta="Deudas", cuenta_destino="Santander UYU",
                         descripcion=f"Pago Splitwise de {creado or '?'}: {desc}",
                         ambito="interno", en_analisis="0")
                if moneda == "UYU":
                    r["monto_uyu"] = fmt(mi_owed)
                pend(rid, "transferencia_a_clasificar",
                     f"pago Splitwise recibido {moneda} {fmt(mi_owed)} ({fecha}, "
                     f"{creado or '?'}): confirmar a que cuenta entro")
                nuevos.append(r)
                stats["insertado_pago_recibido"] += 1
            else:                # pague yo
                f0 = dt.date.fromisoformat(fecha)
                dup = None
                for t in transf_deudas:
                    if t["moneda"] == moneda and abs(float(t["monto"]) - mi_paid) <= 1.0 \
                            and abs((dt.date.fromisoformat(t["fecha"]) - f0).days) <= 2:
                        dup = t
                        break
                if dup:
                    stats["omitido_pago_duplicado"] += 1
                    continue
                r = fila_base()
                r.update(tipo="transferencia", monto=fmt(mi_paid),
                         cuenta="Santander UYU", cuenta_destino="Deudas",
                         descripcion=f"Pago Splitwise a {desc}" if desc else "Pago Splitwise",
                         ambito="interno", en_analisis="0")
                if moneda == "UYU":
                    r["monto_uyu"] = fmt(mi_paid)
                nuevos.append(r)
                stats["insertado_pago_hecho"] += 1
        else:
            stats["omitido_sin_regla"] += 1

    # --- aplicar resoluciones (overrides) a las filas nuevas y persistir ---
    for r in nuevos:
        for campo, valor in overrides.get(r["id"], {}).items():
            r[campo] = valor
    ids_pend = {p["id_ledger"] for p in pendientes if not p["resuelto"]}
    ids_pend |= {p["id_ledger"] for p in pend_nuevos}
    for r in nuevos:
        if r["id"] in ids_pend:
            r["estado"] = "pendiente"

    sw_rows.sort(key=lambda r: (r["fecha"], r["id"]))
    escribir_csv(SPLITWISE_CSV, SW_COLS, sw_rows)
    if nuevos:
        ledger_final = ledger + nuevos
        ledger_final.sort(key=lambda r: (r["fecha"], r["id"]))
        escribir_csv(LEDGER, COLUMNS, ledger_final)
    else:
        ledger_final = ledger
    if pend_nuevos:
        escribir_csv(PENDIENTES, PEND_COLS, pendientes + pend_nuevos)

    # --- validacion de saldos: get_friends vs cuenta Deudas calculada ---
    api_saldo = defaultdict(float)     # moneda -> neto (positivo = me deben)
    for fr in api_get(session, "get_friends").get("friends", []):
        for b in fr.get("balance", []):
            api_saldo[b["currency_code"]] += float(b["amount"])

    corte, ini = None, defaultdict(float)
    for r in leer_csv(SALDOS_INICIALES,
                      ["cuenta", "fecha_corte", "saldo", "moneda", "fuente", "nota"]):
        if r["cuenta"].startswith("Deudas"):
            ini[r["moneda"]] += float(r["saldo"])
            corte = r["fecha_corte"]
    calc = defaultdict(float, ini)
    for r in ledger_final:
        if not corte or r["fecha"] <= corte:
            continue
        m = r["moneda"]
        if r["tipo"] == "gasto" and r["cuenta"] == "Deudas":
            calc[m] -= float(r["monto"])
        elif r["tipo"] == "ingreso" and r["cuenta"] == "Deudas":
            calc[m] += float(r["monto"])
        elif r["tipo"] == "transferencia":
            if r["cuenta_destino"] == "Deudas":
                calc[m] += float(r["monto"])
            if r["cuenta"] == "Deudas":
                calc[m] -= float(r["monto"])

    # --- reporte ---
    print(f"gastos traidos de la API (dated_after={DATED_AFTER}): {len(crudos)}")
    print(f"filas en data/splitwise.csv: {len(sw_rows)}")
    for k, n in sorted(stats.items()):
        print(f"  {k:32s} {n}")
    print(f"filas nuevas en ledger: {len(nuevos)}")
    for k, n in sorted(Counter((r['tipo'], r['cuenta'], r['cuenta_destino'])
                               for r in nuevos).items()):
        print(f"  {k[0]:14s} {k[1]}{(' -> ' + k[2]) if k[2] else '':24s} {n}")
    print(f"pendientes nuevos: {len(pend_nuevos)}")
    for k, n in sorted(Counter(p["motivo"] for p in pend_nuevos).items()):
        print(f"  {k:28s} {n}")
    print(f"saldo Deudas — API get_friends vs calculado (inicial {corte} + movimientos):")
    for m in sorted(set(api_saldo) | {k for k, v in calc.items() if ini or v}):
        a, c = api_saldo.get(m, 0.0), calc.get(m, 0.0)
        print(f"  {m}: API {a:>12.2f} | calculado {c:>12.2f} | diff {a - c:>10.2f}"
              f" {'OK' if abs(a - c) < 0.01 else 'DIFERENCIA'}")


if __name__ == "__main__":
    main()
