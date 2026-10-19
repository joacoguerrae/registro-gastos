# -*- coding: utf-8 -*-
"""
Fase 3 — Sync Splitwise (cuenta Deudas), SOLO LECTURA (docs/fase3.md seccion 1).

FUENTE: la API v3.0 de Splitwise (Bearer key en config/secrets.local.json,
clave splitwise_api_key — NUNCA se imprime ni loguea). Desde el 1/9/2026 la API
devuelve 403 a cuentas sin Splitwise Pro: si da 401/403 el script cae solo a los
EXPORTS CSV por grupo en data/estados/<MM-AA>/splitwise/ (ver fuente_export).
`--fuente export` lo fuerza. Con cualquiera de las dos fuentes produce:

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
  3. payment=true: me pagaron -> transferencia Deudas -> mi cuenta en pesos
     (Santander UYU, Itau UYU desde el 17/10/2026: fuentes.cuenta_personal_uyu)
     (pendiente transferencia_a_clasificar); pague yo -> transferencia
     mi cuenta -> Deudas (dedupe vs transferencias mail/1money existentes
     con destino Deudas, +-1 en monto, +-2 dias).

Al final compara el saldo neto de get_friends contra el saldo calculado de la
cuenta Deudas (saldos_iniciales + movimientos posteriores al corte), por moneda.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/sync_splitwise.py
"""
import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

import requests

import fuentes

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
    # lineterminator="\n": csv.writer escribe CRLF en TODAS las plataformas y
    # .gitattributes fija *.csv eol=lf -> git avisa en cada add. Ver auditoria
    # 2026-09 SB1.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=cols)
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


def fuente_api(session):
    """-> (gastos normalizados, saldo de referencia por moneda, descripcion)."""
    me = api_get(session, "get_current_user")["user"]
    mi_id = me["id"]
    grupos = {g["id"]: g["name"]
              for g in api_get(session, "get_groups").get("groups", [])}
    gastos = []
    for e in traer_gastos(session):
        mi_paid = mi_owed = 0.0
        for u in e.get("users", []):
            if u.get("user_id") == mi_id or (u.get("user") or {}).get("id") == mi_id:
                mi_paid = float(u.get("paid_share") or 0)
                mi_owed = float(u.get("owed_share") or 0)
        gastos.append(dict(
            eid=str(e["id"]), fecha=(e.get("date") or "")[:10],
            desc=(e.get("description") or "").strip(),
            moneda=e.get("currency_code") or "", costo=float(e.get("cost") or 0),
            es_pago=bool(e.get("payment")),
            creado=((e.get("created_by") or {}).get("first_name") or "").strip(),
            grupo=grupos.get(e.get("group_id"), "") if e.get("group_id") else "",
            mi_paid=mi_paid, mi_owed=mi_owed,
            subcat=((e.get("category") or {}).get("name") or ""),
            borrado=bool(e.get("deleted_at"))))
    saldo = defaultdict(float)     # moneda -> neto (positivo = me deben)
    for fr in api_get(session, "get_friends").get("friends", []):
        for b in fr.get("balance", []):
            saldo[b["currency_code"]] += float(b["amount"])
    return gastos, saldo, "API get_friends"


# ------------------------------------------------------------- fuente: export
# Desde el 1/9/2026 la API devuelve 403 en cuentas sin Splitwise Pro, y Joaco
# no va a pagarlo. La alternativa es el export CSV por grupo de la web
# (grupo -> ajustes -> "Export as spreadsheet"), que Joaco deja en
# data/estados/<MM-AA>/splitwise/<slug-del-grupo>_<AAAA-MM-DD>_export.csv.
#
# Diferencias con la API que este adaptador resuelve:
#  - el export NO trae id de gasto: para lo que la API ya trajo se recupera el
#    id real cruzando contra data/splitwise.csv (grupo, moneda, costo, fecha
#    +-1 dia, descripcion); para lo nuevo se genera un id estable ("x"+sha1)
#    desde el contenido. Ambos terminan en fuente_id=sw:<id> como siempre.
#  - trae el NETO por persona (pago - le toca), no los dos por separado. Con un
#    solo pagador se deriva exacto: si mi neto>0 pague yo el total y me toca
#    costo-neto; si <0 me toca -neto. Con VARIOS pagadores no se puede
#    separar: se marca pendiente splitwise_revisar.
#  - solo trae GRUPOS: los gastos sueltos con un amigo y los grupos que no se
#    exportaron (Casita, Baby Shower) se conservan tal cual del cache.
EXPORTS_GLOB = P("data", "estados", "*", "splitwise", "*_export.csv")
YO_EXPORT = "Joaquin Guerra"
MAX_DIAS_EXPORT = 35
RE_EXPORT = re.compile(r"^(?P<slug>.+)_(?P<fecha>\d{4}-\d{2}-\d{2})_export\.csv$")


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def exports_vigentes():
    """-> {slug_grupo: (fecha_export, path)} con el export mas nuevo de cada grupo."""
    out = {}
    for p in glob.glob(EXPORTS_GLOB):
        m = RE_EXPORT.match(os.path.basename(p))
        if not m:
            continue
        g, f = m["slug"], m["fecha"]
        if g not in out or f > out[g][0]:
            out[g] = (f, p)
    return out


def leer_export(path):
    """-> (movimientos crudos, saldo total por moneda de mi columna)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.reader(f))
    personas = [c.strip() for c in filas[0][5:] if c.strip()]
    if YO_EXPORT not in personas:
        raise SystemExit(f"{path}: no encuentro la columna {YO_EXPORT!r} ({personas})")
    yo = personas.index(YO_EXPORT)
    movs, saldo = [], defaultdict(float)
    for r in filas[1:]:
        if len(r) < 5 or not r[0].strip():
            continue
        fecha, desc, cat, costo, mon = (c.strip() for c in r[:5])
        netos = [float(x or 0) for x in r[5:5 + len(personas)]]
        if desc == "Saldo total":
            saldo[mon] += netos[yo]
            continue
        movs.append(dict(fecha=fecha, desc=desc, cat=cat, costo=float(costo),
                         moneda=mon, netos=netos, personas=personas, yo=yo))
    return movs, saldo


def fuente_export(cache):
    """Arma los gastos normalizados desde los exports + el cache de la API.
    -> (gastos, saldo de referencia, descripcion, fecha del export mas viejo en uso, avisos)."""
    vig = exports_vigentes()
    if not vig:
        raise SystemExit("la API de Splitwise no responde y no hay exports en "
                         "data/estados/*/splitwise/ -- exportar los grupos desde la web")
    desde = DATED_AFTER[:10]
    corte_cache = max((c["fecha"] for c in cache), default="")
    # indice del cache por (grupo, moneda, costo) para recuperar ids reales
    idx = defaultdict(list)
    for c in cache:
        idx[(slug(c["grupo"]), c["moneda"], round(float(c["costo_total"]), 2))].append(c)
    usados = set()
    gastos, avisos, saldo = [], [], defaultdict(float)
    nombres_cache = {slug(c["grupo"]): c["grupo"] for c in cache if c["grupo"]}

    for g, (f_exp, path) in sorted(vig.items()):
        movs, s = leer_export(path)
        for m, v in s.items():
            saldo[m] += v
        grupo = nombres_cache.get(g, g.replace("-", " ").title())
        vistos = Counter()
        for mv in movs:
            if mv["fecha"] < desde:
                continue
            netos, yo = mv["netos"], mv["yo"]
            mio = round(netos[yo], 2)
            positivos = [i for i, v in enumerate(netos) if v > 0.004]
            es_pago = mv["cat"] == "Pago"
            multi = False
            if es_pago:
                paid, owed = (mio, 0.0) if mio > 0 else (0.0, -mio if mio < 0 else 0.0)
            elif mio > 0:
                if len(positivos) == 1:
                    paid, owed = mv["costo"], round(mv["costo"] - mio, 2)
                else:
                    paid, owed, multi = mio, 0.0, True
            elif mio < 0:
                paid, owed = 0.0, -mio
            else:
                paid = owed = 0.0
            pagador = mv["personas"][positivos[0]] if positivos else ""

            # id: el real del cache si ya lo trajo la API; si no, uno estable
            clave = (g, mv["moneda"], round(mv["costo"], 2))
            eid = None
            f0 = dt.date.fromisoformat(mv["fecha"])
            for c in idx.get(clave, []):
                if c["id"] in usados:
                    continue
                if abs((dt.date.fromisoformat(c["fecha"]) - f0).days) > 1:
                    continue
                if c["descripcion"].lower() == mv["desc"].lower() or \
                        (es_pago and c["es_pago"] == "1"):
                    eid = c["id"]
                    break
            desconocido = eid is None     # ni la API ni un export anterior lo vieron
            if eid:
                usados.add(eid)
            else:
                k = f"{g}|{mv['fecha']}|{mv['desc']}|{mv['moneda']}|{mv['costo']:.2f}"
                vistos[k] += 1
                eid = "x" + hashlib.sha1(f"{k}|{vistos[k]}".encode()).hexdigest()[:10]
                if corte_cache and mv["fecha"] < corte_cache and (paid or owed):
                    avisos.append(("sin_match", eid, mv, grupo))
            if multi:
                avisos.append(("multi_pagador", eid, mv, grupo))
            gastos.append(dict(
                eid=eid, fecha=mv["fecha"], desc=mv["desc"], moneda=mv["moneda"],
                costo=mv["costo"], es_pago=es_pago, creado=pagador, grupo=grupo,
                mi_paid=paid, mi_owed=owed, subcat=mv["cat"], borrado=False, neto=mio,
                # solo lo DESCONOCIDO y viejo: un gasto con id sintetico que ya
                # esta en el cache lo trajo un export anterior y sigue el flujo
                # normal (el dedupe por fuente_id evita duplicarlo)
                no_insertar=bool(desconocido and corte_cache and mv["fecha"] < corte_cache)))

    # Reorganizacion de deudas ENTRE GRUPOS (paso el 18/9/2026 con el viaje de
    # Guasones): el grupo A "salda" con pagos ficticios y el grupo B recrea las
    # mismas deudas como gastos, mismo dia y mismo monto. Para mi saldo neto es
    # cero y no se movio plata real, pero las reglas inventarian un ingreso a la
    # caja de ahorro, un gasto contado dos veces y transferencias fantasma. Par =
    # un gasto (no pago) y un pago, en grupos distintos, misma moneda, +-1 dia, y
    # mis netos que se anulan. Solo se buscan entre gastos NUEVOS (id sintetico).
    candidatos = [x for x in gastos if x["eid"].startswith("x") and abs(x["neto"]) > 0.004
                  and not x["no_insertar"]]
    for a in candidatos:
        if a["es_pago"] or a.get("reorg"):
            continue
        fa = dt.date.fromisoformat(a["fecha"])
        for b in candidatos:
            if not b["es_pago"] or b.get("reorg") or b["grupo"] == a["grupo"] \
                    or b["moneda"] != a["moneda"]:
                continue
            if abs((dt.date.fromisoformat(b["fecha"]) - fa).days) > 1:
                continue
            if abs(a["neto"] + b["neto"]) < 0.005:
                for x in (a, b):
                    x["reorg"] = x["no_insertar"] = True
                avisos.append(("reorganizacion", a["eid"],
                               dict(a, desc=f"{a['desc']} ({a['grupo']}) <-> {b['desc']} ({b['grupo']})"),
                               a["grupo"]))
                break

    # grupos no exportados y gastos sin grupo: se conservan del cache
    for c in cache:
        if slug(c["grupo"]) in vig:
            if c["id"] not in usados and c["fecha"] >= desde:
                avisos.append(("solo_en_cache", c["id"], c, c["grupo"]))
            continue
        gastos.append(dict(
            eid=c["id"], fecha=c["fecha"], desc=c["descripcion"], moneda=c["moneda"],
            costo=float(c["costo_total"]), es_pago=c["es_pago"] == "1",
            creado=c["creado_por"], grupo=c["grupo"],
            mi_paid=float(c["mi_paid_share"]), mi_owed=float(c["mi_owed_share"]),
            subcat="", borrado=False, no_insertar=False))
    viejo = min(f for f, _ in vig.values())
    desc = (f"export CSV de {len(vig)} grupo(s) ({', '.join(sorted(vig))}); "
            f"export mas viejo {viejo}. Referencia de saldo PARCIAL: solo los grupos "
            f"exportados, sin gastos sueltos ni grupos no exportados")
    return gastos, saldo, desc, viejo, avisos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuente", choices=("auto", "api", "export"), default="auto",
                    help="auto: API y, si da 401/403, los exports CSV (default)")
    args = ap.parse_args()

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

    # --- fuente: API, o exports CSV si la API esta cerrada (Pro-gating) ---
    cache = leer_csv(SPLITWISE_CSV, SW_COLS)
    export_viejo, avisos = None, []
    fuente = args.fuente
    if fuente in ("auto", "api"):
        try:
            api_key = os.environ.get("SPLITWISE_API_KEY")
            if not api_key:
                with open(SECRETS, encoding="utf-8") as f:
                    api_key = json.load(f)["splitwise_api_key"]
            session = requests.Session()
            session.headers["Authorization"] = f"Bearer {api_key}"
            gastos, saldo_ref, desc_ref = fuente_api(session)
            fuente = "api"
        except requests.HTTPError as e:
            codigo = e.response.status_code if e.response is not None else None
            if args.fuente == "api" or codigo not in (401, 403):
                raise
            print(f"API de Splitwise: HTTP {codigo} (acceso cerrado sin Pro) -> uso los exports CSV")
            fuente = "export"
    if fuente == "export":
        gastos, saldo_ref, desc_ref, export_viejo, avisos = fuente_export(cache)

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
    # estado_cuenta: la Mastercard y la BBVA no mandan aviso por transaccion, el
    # cargo solo entra por el PDF -- sin esto un split pagado con ellas nunca
    # apunta a su cargo (auditoria 2026-09 SM6).
    # en_analisis=1: las filas anuladas por doble conteo NO son candidatas. Sin
    # este guard, el split 'Burger' (sw:4544350903), resuelto el 12/7, reabria
    # como pendiente nuevo sobre 78c84a7dd33f (en_analisis=0) -- medido.
    cargos = [r for r in ledger
              if r["tipo"] == "gasto" and r["en_analisis"] == "1"
              and r["origen"] in ("mail", "1money", "manual", "estado_cuenta")
              and r["fecha"] >= "2026-06-15"]
    # transferencias existentes con destino Deudas (dedupe de pagos, regla 3)
    transf_deudas = [r for r in ledger
                     if r["tipo"] == "transferencia" and r["origen"] in ("mail", "1money")
                     and r["cuenta_destino"] == "Deudas"]

    for tipo_aviso, eid_av, mv, grupo_av in avisos:
        if tipo_aviso in ("solo_en_cache", "reorganizacion"):
            continue      # se reportan abajo; no generan pendiente
        detalle = (f"export Splitwise {grupo_av}: {mv['fecha']} {mv['desc']!r} "
                   f"{mv['moneda']} {fmt(mv['costo'])} -- ")
        detalle += ("varios pagadores: el export trae solo el neto por persona y no se "
                    "puede separar cuanto pague yo; cargarlo a mano"
                    if tipo_aviso == "multi_pagador" else
                    "anterior al ultimo sync de la API y sin gasto equivalente en el "
                    "cache: NO se inserto. Si es un gasto cargado tarde, cargarlo a mano")
        pend(hid(f"sw:{eid_av}"), "splitwise_revisar", detalle)

    for e in gastos:
        eid, fecha, desc = e["eid"], e["fecha"], e["desc"]
        moneda, costo, es_pago = e["moneda"], e["costo"], e["es_pago"]
        creado, grupo = e["creado"], e["grupo"]
        mi_paid, mi_owed = e["mi_paid"], e["mi_owed"]

        if e["borrado"]:
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
        if e.get("reorg"):
            stats["reorganizacion_entre_grupos"] += 1
            continue
        if e.get("no_insertar"):
            stats["export_sin_match_no_insertado"] += 1
            continue
        cuenta_moneda = {"USD": "Santander USD"}.get(moneda) or fuentes.cuenta_personal_uyu(fecha)

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
                     subcategoria=e["subcat"])
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
                # la hermana se guarda con fuente_id "<id del ledger>-split"
                # (convencion de /revisar-pendientes y de las 10 hermanas que ya
                # existen), NO con el fuente_id del cargo: buscarla por fuente_id
                # no la encontraba nunca y cada split ya resuelto volvia a
                # proponerse en cada corrida (falso positivo del 1/9/2026).
                h = hermanas.get(c["id"] + "-split")
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
                         cuenta="Deudas", cuenta_destino=cuenta_moneda,
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
                         cuenta=cuenta_moneda, cuenta_destino="Deudas",
                         descripcion=f"Pago Splitwise ({grupo or 'sin grupo'}): {desc}",
                         ambito="interno", en_analisis="0")
                if moneda == "UYU":
                    r["monto_uyu"] = fmt(mi_paid)
                # sin transferencia por mail que lo respalde: pudo salir de otra
                # cuenta o en efectivo -- que lo confirme Joaco
                pend(rid, "transferencia_a_clasificar",
                     f"pago Splitwise hecho {moneda} {fmt(mi_paid)} ({fecha}, {desc}): "
                     f"sin transferencia por mail que lo respalde; asumido desde {cuenta_moneda}, "
                     "confirmar de que cuenta salio")
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

    # --- validacion de saldos: referencia (API o exports) vs cuenta Deudas calculada ---
    api_saldo = saldo_ref

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
    print(f"FUENTE: {fuente.upper()} -- {desc_ref}")
    print(f"gastos leidos (dated_after={DATED_AFTER}): {len(gastos)}")
    for tipo_aviso, eid_av, mv, grupo_av in avisos:
        if tipo_aviso == "solo_en_cache":
            print(f"  [!] en el cache pero NO en el export de {grupo_av}: "
                  f"{mv['fecha']} {mv['descripcion']!r} {mv['moneda']} {mv['costo_total']} "
                  f"(sw:{eid_av}) -- borrado en Splitwise o no matcheo")
        elif tipo_aviso == "reorganizacion":
            print(f"  reorganizacion entre grupos (neto cero, sin plata real, no se inserta): "
                  f"{mv['fecha']} {mv['moneda']} {abs(mv['neto']):.2f}  {mv['desc']}")
        else:
            print(f"  [!] {tipo_aviso}: {grupo_av} {mv['fecha']} {mv['desc']!r} "
                  f"{mv['moneda']} {fmt(mv['costo'])} -> pendiente splitwise_revisar")
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
    print(f"saldo Deudas — {desc_ref} vs calculado (inicial {corte} + movimientos):")
    for m in sorted(set(api_saldo) | {k for k, v in calc.items() if ini or v}):
        a, c = api_saldo.get(m, 0.0), calc.get(m, 0.0)
        print(f"  {m}: referencia {a:>12.2f} | calculado {c:>12.2f} | diff {a - c:>10.2f}"
              f" {'OK' if abs(a - c) < 0.01 else 'DIFERENCIA'}")

    # Un export viejo no avisa solo: los gastos nuevos simplemente no entran.
    # Fallar el paso hace que salud_pasos escale a Issue a las 3 corridas.
    if export_viejo:
        dias = (dt.date.today() - dt.date.fromisoformat(export_viejo)).days
        if dias > MAX_DIAS_EXPORT:
            print(f"EXPORT VENCIDO: el export mas viejo en uso es de {export_viejo} "
                  f"({dias} dias > {MAX_DIAS_EXPORT}). Re-exportar los grupos desde la web "
                  "de Splitwise a data/estados/<MM-AA>/splitwise/.")
            sys.exit(1)


if __name__ == "__main__":
    main()
