# -*- coding: utf-8 -*-
"""
Fase 2 — Procesador de mails financieros (docs/pipeline-mails.md).

Lee data/inbox_raw/<message_id>.json (dump crudo de Gmail, un JSON por mensaje)
y produce filas origen=mail en data/ledger.csv, respetando el contrato de 18
columnas de docs/esquema.md.

Salidas:
  - data/ledger.csv                  (append de filas origen=mail; reescrito ordenado)
  - data/staging/procesados.csv      (log por mail — idempotencia por message_id)
  - data/pendientes.csv              (bandeja de revisión)
  - data/facturas.csv                (facturas informativas Antel/DirecTV)
  - config/estado_sync.json          (watermark)

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/procesar_mails.py [--reprocesar]

Idempotente: un message_id ya presente en procesados.csv no se reprocesa.
--reprocesar: borra las filas origen=mail del ledger, sus pendientes y el log,
y regenera todo desde data/inbox_raw/.
"""
import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
from collections import Counter, defaultdict

BASE = r"C:\Users\joaco\Desktop\proyectos claude\gastos"
P = lambda *a: os.path.join(BASE, *a)

INBOX = P("data", "inbox_raw")
LEDGER = P("data", "ledger.csv")
PROCESADOS = P("data", "staging", "procesados.csv")
PENDIENTES = P("data", "pendientes.csv")
FACTURAS = P("data", "facturas.csv")
ESTADO_SYNC = P("config", "estado_sync.json")
CUENTAS_CSV = P("config", "cuentas.csv")
REGLAS_CSV = P("config", "reglas_comercio.csv")
PARAMETROS = P("config", "parametros.json")
SPLITS_CSV = P("config", "splits_recurrentes.csv")

# Propina y Envio de PedidosYa heredan categoria/ambito del pedido que los origino
PEYA_HIJO = re.compile(r"PedidosYa (Propina|Envio)", re.I)
PEYA_PADRE = re.compile(r"PedidosYa", re.I)
PEYA_VENTANA_SEG = 6 * 3600          # el pedido origina propina/envio hasta 6 h despues
CATS_SPLIT_HEURISTICA = ("Comer afuera / Delivery", "Salidas & ocio")

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]

PROC_COLS = ["message_id", "fecha_mail", "handler", "resultado", "id_ledger",
             "detalle", "dedupe_key", "procesado_en"]
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]
FACT_COLS = ["mes", "emisor", "monto", "vencimiento", "message_id"]

BACKFILL_DESDE = "2026-07-01"          # junio ya está en el ledger vía 1Money
DEDUPE_1M_INI, DEDUPE_1M_FIN = "2026-07-01", "2026-07-08"  # ventana dedupe cruzado

RECIBO_SENDERS = ("no_reply@email.apple.com", "googleplay-noreply@google.com",
                  "info@mercadolibre.com.uy", "info@mercadopago.com")
FACTURA_SENDERS = ("factura@antel.com.uy", "info_uy@email.directvla.com")

MES_EN = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MES_ES = {m: i + 1 for i, m in enumerate(
    ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])}


# ------------------------------------------------------------------ config
def cargar_tarjetas():
    """tarjeta (4 dígitos) -> (cuenta canónica, ambito)."""
    out = {}
    with open(CUENTAS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            for t in (r["tarjetas"] or "").split(";"):
                t = t.strip()
                if t:
                    ambito = "casa" if r["cuenta"] == "Cuenta Casa" else "personal"
                    out[t] = (r["cuenta"], ambito)
    return out


def cargar_reglas():
    reglas = []
    with open(REGLAS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            reglas.append((re.compile(r["patron"], re.I),
                           r["categoria"].strip(),
                           (r.get("ambito_override") or "").strip()))
    return reglas


def categorizar(texto, reglas):
    """Primera regla que matchea gana. -> (categoria, ambito_override, hubo_match)."""
    for pat, cat, amb in reglas:
        if pat.search(texto):
            return cat, amb, True
    return "", "", False


# ------------------------------------------------------------------ parsing
def parse_importe(s):
    """'UYU 28.170,00' / 'USD 97,47' / 'UYU 20000,00' -> (moneda, float).
    Punto = miles, coma = decimal."""
    m = re.search(r"(UYU|USD|EUR)\s*([\d.]+(?:,\d+)?)", s)
    if not m:
        raise ValueError(f"importe no parseable: {s!r}")
    num = m.group(2).replace(".", "").replace(",", ".")
    return m.group(1), float(num)


def limpiar_establecimiento(est):
    """Sin prefijos MONTEVIDEO/ (con basura tipo N07/FKD adelante), INTERNET/,
    FKD, 9829089080/, sufijo VIS3."""
    s = est.strip()
    s = re.sub(r"VIS3\s*$", "", s)
    s = re.sub(r"^9829089080/", "", s)
    s = re.sub(r"^(?:FKD)?[A-Z0-9]{0,6}?(?:MONTEVIDEO|INTERNET)/", "", s)
    s = re.sub(r"^FKD", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def campo_bloque(body, label, siguiente):
    """Texto entre la línea `label` y la línea `siguiente` (avisos de tarjeta)."""
    m = re.search(re.escape(label) + r"\s*\n(.*?)\n\s*" + re.escape(siguiente),
                  body, re.S)
    return m.group(1).replace("\n", " ").strip() if m else ""


def campos_transferencia(body):
    """Parser por líneas: una etiqueta termina en ':'; el valor es la línea
    siguiente si no es otra etiqueta."""
    lines = [ln.strip() for ln in body.splitlines()]
    vals = defaultdict(str)
    for i, ln in enumerate(lines):
        if ln.endswith(":"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            vals[ln[:-1].strip().lower()] = "" if nxt.endswith(":") else nxt
    return vals


def parse_fecha_valor(s):
    """'Jul 03, 2026' (meses en inglés; tolera español) -> date."""
    m = re.search(r"([A-Za-z]{3})\w*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if not m:
        raise ValueError(f"fecha valor no parseable: {s!r}")
    mes = MES_EN.get(m.group(1).lower()) or MES_ES.get(m.group(1).lower())
    return dt.date(int(m.group(3)), mes, int(m.group(2)))


def hid(message_id):
    return hashlib.sha1(("mail" + message_id).encode()).hexdigest()[:12]


def fila_base(message_id):
    r = {c: "" for c in COLUMNS}
    r.update(id=hid(message_id), origen="mail", fuente_id=message_id,
             estado="confirmado", en_analisis="1")
    return r


def fmt(x):
    return f"{x:.2f}"


# ------------------------------------------------------------------ estado en disco
def leer_csv(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        assert rd.fieldnames == cols, f"columnas de {path} != esperadas: {rd.fieldnames}"
        return list(rd)


def escribir_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reprocesar", action="store_true",
                    help="regenera desde cero las filas origen=mail")
    args = ap.parse_args()

    tarjetas = cargar_tarjetas()
    reglas = cargar_reglas()
    with open(PARAMETROS, encoding="utf-8") as f:
        params = json.load(f)
    tc = {"USD": params["tc_usd_uyu"], "EUR": params["tc_eur_uyu"]}
    umbral_split = float(params.get("umbral_split_uyu", 0) or 0)
    splits_pat = []
    if os.path.exists(SPLITS_CSV):
        with open(SPLITS_CSV, encoding="utf-8-sig") as f:
            for r0 in csv.DictReader(f):
                splits_pat.append((re.compile(r0["patron"], re.I),
                                   (r0.get("mi_fraccion") or "").strip(),
                                   (r0.get("contraparte") or "").strip()))

    def motivo_split(texto, categoria, monto_uyu):
        """-> (motivo, detalle) si el gasto debe recordarse para Splitwise."""
        for pat, frac, quien in splits_pat:
            if pat.search(texto):
                extra = f", mi fraccion {frac}" if frac else ""
                return ("split_a_cargar",
                        f"split recurrente con {quien or '?'}{extra}: {texto!r}")
        if umbral_split and categoria in CATS_SPLIT_HEURISTICA \
                and float(monto_uyu or 0) >= umbral_split:
            return ("split_a_cargar",
                    f"{categoria} por {monto_uyu} UYU (>= umbral {umbral_split:.0f}): cargar a Splitwise?")
        return None

    ledger = leer_csv(LEDGER, COLUMNS)
    procesados = leer_csv(PROCESADOS, PROC_COLS)
    pendientes = leer_csv(PENDIENTES, PEND_COLS)
    facturas = leer_csv(FACTURAS, FACT_COLS)

    if args.reprocesar:
        ids_mail = {r["id"] for r in ledger if r["origen"] == "mail"}
        ledger = [r for r in ledger if r["origen"] != "mail"]
        pendientes = [p for p in pendientes if p["id_ledger"] not in ids_mail]
        mids = {r["message_id"] for r in procesados}
        facturas = [fa for fa in facturas if fa["message_id"] not in mids]
        procesados = []

    ya_procesados = {r["message_id"] for r in procesados}

    # transferencias 1money para el dedupe cruzado
    transfers_1m = [r for r in ledger
                    if r["origen"] == "1money" and r["tipo"] == "transferencia"]

    # claves de dedupe heurístico de corridas anteriores
    claves_previas = []  # (tarjeta, monto, establecimiento, datetime, message_id)
    for r in procesados:
        if r["dedupe_key"]:
            t, m, e, ts = r["dedupe_key"].split("||")
            claves_previas.append((t, m, e, dt.datetime.fromisoformat(ts),
                                   r["message_id"]))

    # pedidos PedidosYa de corridas anteriores (padres para herencia propina/envio)
    filas_mail_por_id = {r["id"]: r for r in ledger if r["origen"] == "mail"}
    padres_peya = []  # (tarjeta, fh, categoria, ambito)
    for p in procesados:
        if p["handler"] == "aviso_tarjeta" and p["resultado"] == "insertado" \
                and p["dedupe_key"]:
            t0, _, e0, ts0 = p["dedupe_key"].split("||")
            if PEYA_PADRE.search(e0) and not PEYA_HIJO.search(e0):
                r0 = filas_mail_por_id.get(p["id_ledger"])
                if r0:
                    padres_peya.append((t0, dt.datetime.fromisoformat(ts0),
                                        r0["categoria"], r0["ambito"]))
    padres_peya.sort(key=lambda x: x[1])

    mails = []
    for path in sorted(glob.glob(os.path.join(INBOX, "*.json"))):
        with open(path, encoding="utf-8") as f:
            mails.append(json.load(f))
    mails.sort(key=lambda m: m["fecha_mail"])

    ahora = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    hoy = dt.date.today().isoformat()

    nuevos, pend_nuevos, proc_nuevos, fact_nuevas = [], [], [], []
    filas_mail_existentes = [r for r in ledger if r["origen"] == "mail"]
    ledger_modificado = False

    def log_proc(mail, handler, resultado, id_ledger="", detalle="", dedupe_key=""):
        proc_nuevos.append(dict(message_id=mail["message_id"],
                                fecha_mail=mail["fecha_mail"], handler=handler,
                                resultado=resultado, id_ledger=id_ledger,
                                detalle=detalle, dedupe_key=dedupe_key,
                                procesado_en=ahora))

    def pend(id_ledger, motivo, detalle):
        pend_nuevos.append(dict(id_ledger=id_ledger, motivo=motivo,
                                detalle=detalle, fecha_alta=hoy, resuelto=""))

    def router(mail):
        frm, subj = mail["from"].lower(), mail["subject"]
        if "notificaciones@santander.com.uy" in frm:
            if subj.startswith("Aviso: uso de tarjeta"):
                return "aviso_tarjeta"
            if subj.lower().startswith("aviso de transferencia"):
                return "transferencia"
            return "ignorado"
        if any(s in frm for s in RECIBO_SENDERS):
            return "recibo"
        if any(s in frm for s in FACTURA_SENDERS):
            return "factura"
        if "estado de cuenta" in subj.lower() or "bbva" in frm:
            return "estado_cuenta"
        return "ignorado"

    # -------------------------------------------------- 1. avisos de tarjeta
    claves_corrida = list(claves_previas)
    for mail in mails:
        if mail["message_id"] in ya_procesados or router(mail) != "aviso_tarjeta":
            continue
        b = mail["body_text"]
        tarjeta = campo_bloque(b, "VISA terminada en", "Importe")
        moneda, monto = parse_importe(campo_bloque(b, "Importe", "Establecimento"))
        est_crudo = campo_bloque(b, "Establecimento", "Fecha y hora")
        fh_txt = campo_bloque(b, "Fecha y hora", "Para ver")
        fh = dt.datetime.strptime(fh_txt[:19], "%d/%m/%Y %H:%M:%S")
        fecha = fh.date().isoformat()

        if fecha < BACKFILL_DESDE:
            log_proc(mail, "aviso_tarjeta", "ignorado_pre_backfill",
                     detalle=f"{tarjeta} {moneda} {fmt(monto)} {est_crudo} ({fecha} < {BACKFILL_DESDE})")
            continue

        cuenta, ambito = tarjetas.get(tarjeta, ("", "personal"))
        if not cuenta:
            log_proc(mail, "aviso_tarjeta", "ignorado",
                     detalle=f"tarjeta desconocida {tarjeta}")
            continue

        categoria, amb_override, _ = categorizar(est_crudo, reglas)
        if amb_override:
            ambito = amb_override

        # herencia: Propina/Envio de PedidosYa toman la categoria y el ambito
        # del pedido que los origino (misma tarjeta, hasta 6 h antes)
        if PEYA_HIJO.search(est_crudo):
            for (t0, fh0, cat0, amb0) in reversed(padres_peya):
                if t0 == tarjeta and 0 <= (fh - fh0).total_seconds() <= PEYA_VENTANA_SEG:
                    if cat0:
                        categoria, ambito = cat0, amb0
                    break

        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], tipo="gasto", monto=fmt(monto),
                 moneda=moneda, cuenta=cuenta, categoria=categoria,
                 comercio=est_crudo,
                 descripcion=limpiar_establecimiento(est_crudo), ambito=ambito)
        motivos = []
        if moneda == "UYU":
            r["monto_uyu"] = fmt(monto)
        else:
            r["monto_uyu"] = fmt(round(monto * tc[moneda], 2))
            motivos.append(("tc_estimado",
                            f"{moneda} {fmt(monto)} x tc {tc[moneda]} = {r['monto_uyu']} UYU (corrige Fase 3)"))
        if not categoria:
            motivos.append(("sin_categoria", f"establecimiento {est_crudo!r} sin regla"))
        if tarjeta == "8261":
            motivos.append(("ambito_a_confirmar",
                            "transicion jul-2026: confirmar vuelta de la casa a Santander (8261)"))
        ms = motivo_split(est_crudo, categoria, r["monto_uyu"])
        if ms:
            motivos.append(ms)

        # dedupe heurístico +-10 min
        clave = (tarjeta, fmt(monto), est_crudo)
        for (t0, m0, e0, fh0, mid0) in claves_corrida:
            if (t0, m0, e0) == clave and mid0 != mail["message_id"] \
                    and abs((fh - fh0).total_seconds()) <= 600:
                motivos.append(("posible_duplicado",
                                f"mismo (tarjeta,monto,establecimiento) que mail {mid0} a las {fh0}"))
                break
        claves_corrida.append((tarjeta, fmt(monto), est_crudo, fh, mail["message_id"]))
        if PEYA_PADRE.search(est_crudo) and not PEYA_HIJO.search(est_crudo):
            padres_peya.append((tarjeta, fh, categoria, ambito))

        if motivos:
            r["estado"] = "pendiente"
            for mo, de in motivos:
                pend(r["id"], mo, de)
        nuevos.append(r)
        log_proc(mail, "aviso_tarjeta", "insertado", r["id"],
                 f"{tarjeta} {moneda} {fmt(monto)} {est_crudo} -> {cuenta}/{ambito}/{categoria or '(sin categoria)'}",
                 dedupe_key="||".join([tarjeta, fmt(monto), est_crudo, fh.isoformat()]))

    # -------------------------------------------------- 2. transferencias
    for mail in mails:
        if mail["message_id"] in ya_procesados or router(mail) != "transferencia":
            continue
        v = campos_transferencia(mail["body_text"])
        titular = v["titular"]
        concepto = " ".join(x for x in (v["concepto origen"], v["concepto destino"]) if x).strip()
        moneda, monto = parse_importe(v["monto"])
        fecha = parse_fecha_valor(v["fecha valor"]).isoformat()

        if fecha < BACKFILL_DESDE:
            log_proc(mail, "transferencia", "ignorado_pre_backfill",
                     detalle=f"{titular} {moneda} {fmt(monto)} ({fecha})")
            continue

        # dedupe cruzado con 1money (solo ventana 2026-07-01 -> 2026-07-08)
        dup = None
        if DEDUPE_1M_INI <= fecha <= DEDUPE_1M_FIN:
            f0 = dt.date.fromisoformat(fecha)
            for t in transfers_1m:
                if abs(float(t["monto"]) - monto) <= 1.0 and t["moneda"] == moneda \
                        and abs((dt.date.fromisoformat(t["fecha"]) - f0).days) <= 2:
                    dup = t
                    break
        if dup:
            log_proc(mail, "transferencia", "duplicado_1money",
                     detalle=f"{titular} {moneda} {fmt(monto)} {fecha} ~ 1money "
                             f"{dup['id']} ({dup['fecha']} {dup['monto']} {dup['descripcion']})")
            continue

        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], monto=fmt(monto), moneda=moneda,
                 cuenta="Santander UYU", descripcion=concepto)
        if moneda == "UYU":
            r["monto_uyu"] = fmt(monto)
        texto = f"{titular} {concepto}"
        motivos = []

        if re.search(r"mercado pago", titular, re.I) or re.search(r"\bcasa\b", concepto, re.I):
            # regla 1: aporte a la cuenta casa
            r.update(tipo="transferencia", cuenta_destino="Cuenta Casa",
                     ambito="interno", en_analisis="0",
                     descripcion=concepto or "Aporte")
        elif re.search(r"\blu\b", titular, re.I):
            # regla 2: transferencia a Lu — a clasificar (puede ser cierre de casa)
            r.update(tipo="transferencia", cuenta_destino=titular,
                     ambito="interno", en_analisis="0")
            motivos.append(("transferencia_a_clasificar",
                            f"a {titular!r}, concepto {concepto!r}, {moneda} {fmt(monto)}"))
        else:
            # regla 3: tercero -> gasto
            ambito = "casa" if re.search(r"garage|vazquez ledesma", texto, re.I) else "personal"
            categoria, amb_override, _ = categorizar(texto, reglas)
            if amb_override:
                ambito = amb_override
            r.update(tipo="gasto", comercio=titular, categoria=categoria, ambito=ambito)
            if not categoria:
                motivos.append(("transferencia_a_clasificar",
                                f"a {titular!r}, concepto {concepto!r}, {moneda} {fmt(monto)}: sin categoria"))
        if motivos:
            r["estado"] = "pendiente"
            for mo, de in motivos:
                pend(r["id"], mo, de)
        nuevos.append(r)
        log_proc(mail, "transferencia", "insertado", r["id"],
                 f"{mail['subject']} | {titular} {concepto!r} {moneda} {fmt(monto)} {fecha} "
                 f"-> {r['tipo']}/{r['ambito']}/{r['categoria'] or '(sin categoria)'}")

    # -------------------------------------------------- 3. recibos (matching)
    def parse_recibo(mail):
        """-> dict(monto, moneda, producto, tarjeta, fecha, es_mp) o None."""
        b, frm = mail["body_text"], mail["from"].lower()
        fecha_mail = dt.datetime.fromisoformat(mail["fecha_mail"].replace("Z", "+00:00"))
        fecha_uy = (fecha_mail - dt.timedelta(hours=3)).date()
        out = dict(tarjeta="", fecha=fecha_uy, es_mp="mercadopago" in frm)
        num = lambda s: float(s.replace(".", "").replace(",", "."))
        mon = lambda simbolo: {"US$": "USD", "USD": "USD", "€": "EUR"}.get(simbolo, "UYU")

        if "googleplay-noreply@google.com" in frm:
            m = re.search(r"Total:\s*(US\$|\$|€)\s*([\d.,]+)", b)
            if not m:
                return None
            out.update(moneda=mon(m.group(1)), monto=num(m.group(2)))
            mp = re.search(r"(?m)^(.+?)\s*\(de .+?\)\s*(?:US\$|\$|€)", b)
            out["producto"] = f"Google Play – {mp.group(1).strip()}" if mp else "Google Play"
            mt = re.search(r"Visa[- ]?(\d{4})", b)
            out["tarjeta"] = mt.group(1) if mt else ""
            mf = re.search(r"Fecha del pedido:\s*(\d{1,2})\s+(\w{3})\w*\.?\s+(\d{4})", b)
            if mf and MES_ES.get(mf.group(2).lower()):
                out["fecha"] = dt.date(int(mf.group(3)), MES_ES[mf.group(2).lower()],
                                       int(mf.group(1)))
            return out
        if "info@mercadolibre.com.uy" in frm:
            m = re.search(r"\$\s*([\d.,]+)\s*/\s*mes", b)
            if not m:
                return None
            out.update(moneda="UYU", monto=num(m.group(1)),
                       producto="Meli+" if "Meli+" in b else mail["subject"])
            mt = re.search(r"\*{4}\s*(\d{4})", b)
            out["tarjeta"] = mt.group(1) if mt else ""
            return out
        # Apple / Mercado Pago "Pago aprobado" — genérico
        m = re.search(r"(?:Total|Pagaste|Pago)\D{0,20}(US\$|\$|€|UYU|USD)\s*([\d.,]+)", b)
        if not m:
            return None
        out.update(moneda=mon(m.group(1)), monto=num(m.group(2)),
                   producto=mail["subject"])
        return out

    candidatos = filas_mail_existentes + nuevos   # avisos ya insertados
    for mail in mails:
        if mail["message_id"] in ya_procesados or router(mail) != "recibo":
            continue
        rec = parse_recibo(mail)
        if rec is None:
            log_proc(mail, "recibo", "ignorado", detalle="recibo no parseable")
            continue
        cuenta_t = tarjetas.get(rec["tarjeta"], ("", ""))[0]
        match = None
        for r in candidatos:
            if r["tipo"] != "gasto" or r["moneda"] != rec["moneda"]:
                continue
            if cuenta_t and r["cuenta"] != cuenta_t:
                continue
            if abs(float(r["monto"]) - rec["monto"]) > rec["monto"] * 0.005:
                continue
            if abs((dt.date.fromisoformat(r["fecha"]) - rec["fecha"]).days) > 3:
                continue
            match = r
            break
        if match:
            match["comercio"] = rec["producto"]
            ms = motivo_split(rec["producto"], match["categoria"], match["monto_uyu"])
            if ms:
                pend(match["id"], *ms)
                match["estado"] = "pendiente"
            if match in filas_mail_existentes:
                ledger_modificado = True
            log_proc(mail, "recibo", "recibo_matcheado", match["id"],
                     f"{rec['producto']} {rec['moneda']} {fmt(rec['monto'])} "
                     f"-> enriquece comercio de {match['id']}")
        else:
            cuenta = "Cuenta Casa" if rec["es_mp"] else "Santander UYU"
            categoria, _, _ = categorizar(rec["producto"], reglas)
            r = fila_base(mail["message_id"])
            fecha = rec["fecha"].isoformat()
            r.update(fecha=fecha, mes=fecha[:7], tipo="gasto",
                     monto=fmt(rec["monto"]), moneda=rec["moneda"],
                     cuenta=cuenta, categoria=categoria,
                     comercio=rec["producto"], descripcion=rec["producto"],
                     ambito="casa" if cuenta == "Cuenta Casa" else "personal",
                     estado="pendiente")
            if rec["moneda"] == "UYU":
                r["monto_uyu"] = fmt(rec["monto"])
            else:
                r["monto_uyu"] = fmt(round(rec["monto"] * tc[rec["moneda"]], 2))
                pend(r["id"], "tc_estimado",
                     f"{rec['moneda']} {fmt(rec['monto'])} x tc {tc[rec['moneda']]}")
            pend(r["id"], "recibo_sin_matchear",
                 f"{rec['producto']} {rec['moneda']} {fmt(rec['monto'])} {fecha}: "
                 "sin aviso de tarjeta que matchee (posible pago con saldo MP)")
            ms = motivo_split(rec["producto"], categoria, r["monto_uyu"])
            if ms:
                pend(r["id"], *ms)
            nuevos.append(r)
            candidatos.append(r)
            log_proc(mail, "recibo", "insertado_pendiente", r["id"],
                     f"{rec['producto']} {rec['moneda']} {fmt(rec['monto'])} sin match")

    # -------------------------------------------------- 4. facturas + ignorados
    for mail in mails:
        if mail["message_id"] in ya_procesados:
            continue
        h = router(mail)
        if h == "factura":
            b = mail["body_text"]
            emisor = "Antel" if "antel" in mail["from"].lower() else "DirecTV"
            mm = re.search(r"\$?\s*([\d.]+,\d{2})", b)
            monto = mm.group(1).replace(".", "").replace(",", ".") if mm else ""
            mv = re.search(r"[Vv]encimiento\D{0,20}(\d{2}/\d{2}/\d{4})", b)
            venc = ("-".join(reversed(mv.group(1).split("/")))) if mv else ""
            mes = mail["fecha_mail"][:7]
            fact_nuevas.append(dict(mes=mes, emisor=emisor, monto=monto,
                                    vencimiento=venc, message_id=mail["message_id"]))
            log_proc(mail, "factura", "factura",
                     detalle=f"{emisor} {monto} venc {venc} (informativo, no toca ledger)")
        elif h == "estado_cuenta":
            log_proc(mail, "ignorado", "estado_cuenta_pendiente_f3",
                     detalle=f"{mail['from']} | {mail['subject']}")
        elif h == "ignorado":
            log_proc(mail, "ignorado", "ignorado",
                     detalle=f"{mail['from']} | {mail['subject']}")

    # -------------------------------------------------- 5. persistir
    ledger_final = ledger + nuevos
    ledger_final.sort(key=lambda r: (r["fecha"], r["id"]))
    if nuevos or ledger_modificado or args.reprocesar:
        escribir_csv(LEDGER, COLUMNS, ledger_final)
    escribir_csv(PROCESADOS, PROC_COLS, procesados + proc_nuevos)
    escribir_csv(PENDIENTES, PEND_COLS, pendientes + pend_nuevos)
    escribir_csv(FACTURAS, FACT_COLS, facturas + fact_nuevas)

    if mails:
        ultima = max(m["fecha_mail"] for m in mails)
        estado = {"ultimo_sync": ahora, "ultima_fecha_mail": ultima}
        with open(ESTADO_SYNC, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)

    # -------------------------------------------------- 6. reporte
    print(f"mails en inbox_raw: {len(mails)}; ya procesados (skip): "
          f"{sum(1 for m in mails if m['message_id'] in ya_procesados)}")
    cnt = Counter((p["handler"], p["resultado"]) for p in proc_nuevos)
    for (h, res), n in sorted(cnt.items()):
        print(f"  {h:15s} {res:28s} {n}")
    print(f"filas nuevas en ledger: {len(nuevos)}")
    for k, n in sorted(Counter((r["tipo"], r["cuenta"]) for r in nuevos).items()):
        print(f"  {k[0]:14s} {k[1]:20s} {n}")
    print(f"pendientes nuevos: {len(pend_nuevos)}")
    for k, n in sorted(Counter(p["motivo"] for p in pend_nuevos).items()):
        print(f"  {k:26s} {n}")
    jul = defaultdict(float)
    for r in ledger_final:
        if r["mes"] == "2026-07" and r["tipo"] == "gasto" and r["en_analisis"] == "1":
            jul[r["categoria"] or "(sin categoria)"] += float(r["monto_uyu"])
    print("gasto 2026-07 por categoria (en_analisis=1, todos los origenes):")
    for c, v in sorted(jul.items(), key=lambda kv: -kv[1]):
        print(f"  {c:26s} {v:12.2f}")
    print(f"  {'TOTAL':26s} {sum(jul.values()):12.2f}")


if __name__ == "__main__":
    main()
