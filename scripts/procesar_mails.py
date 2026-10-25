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

import fuentes

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

INBOX = P("data", "inbox_raw")
LEDGER = P("data", "ledger.csv")
PROCESADOS = P("data", "staging", "procesados.csv")
PENDIENTES = P("data", "pendientes.csv")
FACTURAS = P("data", "facturas.csv")
ESTADO_SYNC = P("config", "estado_sync.json")
CUENTAS_CSV = P("config", "cuentas.csv")
CUENTAS_BANCARIAS = P("config", "cuentas_bancarias.csv")
REGLAS_CSV = P("config", "reglas_comercio.csv")
PARAMETROS = P("config", "parametros.json")
SPLITS_CSV = P("config", "splits_recurrentes.csv")

# Propina y Envio de PedidosYa heredan categoria/ambito del pedido que los origino
PEYA_HIJO = re.compile(r"PedidosYa[ *](Propina|Envio)", re.I)   # Santander: espacio; Itau: *
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

# data/resoluciones.csv: correcciones manuales persistentes (sobreviven a
# --reprocesar). campo = columna del ledger a sobreescribir, o el especial
# "motivo_resuelto" (valor = motivo de pendientes que queda cerrado y no se
# vuelve a generar para ese id).
RESOLUCIONES = P("data", "resoluciones.csv")
RES_COLS = ["id_ledger", "campo", "valor", "nota", "fecha"]
# Pendientes que crean OTROS scripts (aunque apunten a filas mail): --reprocesar
# no los puede regenerar, asi que no los toca.
MOTIVOS_AJENOS = {"sin_respaldo_estado", "ingreso_a_clasificar", "splitwise_revisar",
                  "carga_manual_invalida", "carga_manual_duplicado"}

BACKFILL_DESDE = "2026-07-01"          # junio ya está en el ledger vía 1Money
DEDUPE_1M_INI, DEDUPE_1M_FIN = "2026-07-01", "2026-07-08"  # ventana dedupe cruzado

RECIBO_SENDERS = ("no_reply@email.apple.com", "googleplay-noreply@google.com",
                  "info@mercadolibre.com.uy", "info@mercadopago.com")
FACTURA_SENDERS = ("factura@antel.com.uy", "info_uy@email.directvla.com")

# "Tu transferencia fue enviada" (Mercado Pago -> Santander): retorno del
# fondo de casa que estuvo parqueado en MP. Se rutea aparte de "recibo"
# porque no es un gasto sino un movimiento de fondos.
MP_TRANSFER_PAT = re.compile(r"ya enviamos tu transferencia de\s*\$?\s*([\d.,]+)", re.I)

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


def cargar_cuentas_propias():
    """numero de cuenta (sin ceros a la izquierda) -> (cuenta, moneda), de
    config/cuentas_bancarias.csv. Sirve para reconocer en un aviso de
    transferencia que el destino es una cuenta de Joaco (ej. Santander -> Itau,
    oct-2026): eso es un movimiento interno, no un gasto. Incluye Cuenta Casa:
    en los avisos de Santander la saltea la regla 0 (la casa tiene su propia
    regla, el aporte); los de Itau solo traen el numero, asi que la necesitan."""
    out = {}
    if not os.path.exists(CUENTAS_BANCARIAS):
        return out
    with open(CUENTAS_BANCARIAS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            num = (r["numero"] or "").strip().lstrip("0")
            cuenta = (r["cuenta_ledger"] or "").strip()
            if num and cuenta:
                out[num] = (cuenta, (r["moneda"] or "").strip())
    return out


def cargar_cuentas_validas():
    with open(CUENTAS_CSV, encoding="utf-8-sig") as f:
        return {r["cuenta"].strip() for r in csv.DictReader(f)}


def cuenta_en_moneda(cuenta, moneda, validas):
    """La misma cuenta pero en otra moneda, si existe.

    Una tarjeta de débito sirve a las cuentas del titular en TODAS las monedas:
    la 9071 debita de la caja de ahorro en pesos o de la de dólares según en qué
    moneda sea el consumo. El mapeo tarjeta->cuenta es uno solo, así que un
    consumo en USD quedaba imputado a la cuenta en pesos (lo destapó el pago del
    estado de la Platinum, USD 118,34 del 3/8/2026, que salió de la caja USD).

    Convención de nombres ya usada en config/cuentas.csv: el par es
    "Santander UYU"/"Santander USD" (sufijo de moneda que se reemplaza). Para las
    TARJETAS no hay contraparte en el catálogo a propósito: catalogar un
    "Santander Platinum USD" rompe el cálculo de saldos (los pagos en USD de la
    Platinum van al nombre BASE de la cuenta). Al no existir el candidato, la
    función devuelve la cuenta original, que es lo correcto para conciliar_saldos.
    Si la contraparte no existe se deja la original: mejor la cuenta conocida que
    una inventada.
    """
    if moneda == "UYU":
        return cuenta
    cand = cuenta[:-4] + " " + moneda if cuenta.endswith(" UYU") else f"{cuenta} {moneda}"
    return cand if cand in validas else cuenta


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
    """Parser de pares etiqueta/valor de los avisos de transferencia. Tolera
    los DOS layouts en que Claude renderiza el mail (varían entre corridas):
      A) etiqueta sola en su línea, valor en la línea siguiente:
           Monto:
           UYU 1700,00
      B) etiqueta y valor en la MISMA línea:
           Monto: UYU 1700,00
    """
    lines = [ln.strip() for ln in body.splitlines()]
    vals = defaultdict(str)
    for i, ln in enumerate(lines):
        if ":" not in ln:
            continue
        etiqueta, _, resto = ln.partition(":")
        clave = etiqueta.strip().lower()
        resto = resto.strip()
        if resto:                       # layout B: valor en la misma línea
            vals[clave] = resto
        else:                           # layout A: valor en la línea siguiente
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            # la línea siguiente es un valor sólo si no es a su vez otra etiqueta
            # "sola" (termina en ':' sin nada después)
            vals[clave] = "" if nxt.endswith(":") else nxt
    return vals


def parse_fecha_valor(s):
    """'Jul 03, 2026' (meses en inglés; tolera español) -> date."""
    m = re.search(r"([A-Za-z]{3})\w*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if not m:
        raise ValueError(f"fecha valor no parseable: {s!r}")
    mes = MES_EN.get(m.group(1).lower()) or MES_ES.get(m.group(1).lower())
    return dt.date(int(m.group(3)), mes, int(m.group(2)))


# Estas dos funciones son el UNICO punto donde un cambio de layout del banco
# puede romper el pipeline: extraen campos por etiqueta literal. Estan aisladas
# para que quien las llama pueda cazar la excepcion y dejar ESE mail como
# pendiente `formato_nuevo`, en vez de frenar la corrida entera (auditoria
# 2026-07 M1). Lanzan a proposito: el que llama decide que hacer.
def parse_aviso(body):
    """Aviso de uso de tarjeta y aviso de devolucion (mismo layout, cambia solo
    el encabezado). -> (tarjeta, moneda, monto, establecimiento_crudo, fecha_hora)"""
    tarjeta = campo_bloque(body, "VISA terminada en", "Importe")
    moneda, monto = parse_importe(campo_bloque(body, "Importe", "Establecimento"))
    est_crudo = campo_bloque(body, "Establecimento", "Fecha y hora")
    fh_txt = campo_bloque(body, "Fecha y hora", "Para ver")
    fh = dt.datetime.strptime(fh_txt[:19], "%d/%m/%Y %H:%M:%S")
    return tarjeta, moneda, monto, est_crudo, fh


def debito_origen_transferencia(body):
    """(moneda, monto) de lo que REALMENTE se debita, si el aviso lo declara.

    En una transferencia CRUZADA (sale USD, llega UYU) el campo 'Monto:' del
    aviso es lo que RECIBE el destinatario, no lo que sale de la cuenta. El
    bloque 'Cuenta Origen' trae 'Moneda: USD 163,61' -> moneda Y monto del
    debito real. campos_transferencia() no sirve para esto: hay dos etiquetas
    'Moneda:' (origen y destino) y la del destino pisa a la del origen.

    Lo destapo la transferencia a la escribana del 22/7 (auditoria: salieron
    USD 163,61 y se habian registrado 6.430 UYU de la cuenta en pesos).
    Devuelve None si el aviso no declara monto en el bloque origen (layouts
    viejos): ahi se sigue usando el 'Monto:' como antes.
    """
    m = re.search(r"Cuenta Origen(.*?)Cuenta Destino", body, re.S)
    if not m:
        return None
    m2 = re.search(r"Moneda:\s*\n?\s*(UYU|USD|EUR)\s*([\d.]+(?:,\d+)?)", m.group(1))
    if not m2:
        return None
    return m2.group(1), float(m2.group(2).replace(".", "").replace(",", "."))


def numero_cuenta_destino(body):
    """'Número de cuenta' del bloque 'Cuenta Destino' de un aviso de
    transferencia de Santander, sin ceros a la izquierda ('' si no esta)."""
    m = re.search(r"Cuenta Destino.*?N.mero de cuenta:\s*(\d+)", body, re.S)
    return m.group(1).lstrip("0") if m else ""


# Avisos de Itau (comunicaciones@itau.com.uy, desde oct-2026). Dos layouts, sin
# fecha ni hora en el cuerpo (se usa la del mail, en hora de Uruguay):
#   credito: "VISA nro. *** 5023 / Importe: 3451.0 UYU / Comercio: EBN*SHEIN MONTE"
#   debito:  "...tarjeta Visa terminada en 6136 . Realizado en DELIVIANDA*HANDY*0PJHVIS3
#             Monto: 2,962.00 Pesos."
# Importes en formato US (coma = miles, punto = decimal), al reves que Santander.
RE_ITAU_CREDITO = re.compile(
    r"VISA nro\.\s*\**\s*(\d{4})\s*Importe:\s*([\d.,]+)\s*(UYU|USD|EUR)\s*"
    r"Comercio:\s*(.+?)\s*(?:\n|Adem.s|$)", re.S)
RE_ITAU_DEBITO = re.compile(
    r"terminada en\s*(\d{4})\s*\.?\s*Realizado en\s*(.+?)\s+Monto:\s*([\d.,]+)\s*"
    r"(Pesos|D.lares|Euros)", re.S)
MONEDA_ITAU = {"pesos": "UYU", "euros": "EUR"}


def num_itau(s):
    """'2,962.00' / '3451.0' -> float. Tolera '1.234,56' por si cambian el formato."""
    s = s.strip().rstrip(".,")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    return float(s)


def parse_aviso_itau(mail):
    """Aviso de consumo aprobado de Itau (credito o debito).
    -> (tarjeta, moneda, monto, establecimiento_crudo, fecha_hora), como parse_aviso.
    Lanza si no reconoce el layout (el que llama lo deja como formato_nuevo)."""
    body = mail["body_text"]
    m = RE_ITAU_CREDITO.search(body)
    if m:
        tarjeta, monto, moneda, est = m.group(1), num_itau(m.group(2)), m.group(3), m.group(4)
    else:
        m = RE_ITAU_DEBITO.search(body)
        if not m:
            raise ValueError("aviso Itau: layout desconocido (ni credito ni debito)")
        tarjeta, est, monto = m.group(1), m.group(2), num_itau(m.group(3))
        moneda = MONEDA_ITAU.get(m.group(4).lower(), "USD")
    fh = (dt.datetime.fromisoformat(mail["fecha_mail"].replace("Z", "+00:00"))
          - dt.timedelta(hours=3)).replace(tzinfo=None, microsecond=0)
    return tarjeta, moneda, monto, est.strip(), fh


RE_ITAU_TRANSF = re.compile(
    r"desde la cuenta\s*\**\s*(\d{3,})\s*Importe:\s*([\d.,]+)\s*(UYU|USD|EUR)\s*"
    r"Cuenta destino:\s*(\d+)\s*(?:Banco/Instituci.n destino:\s*(.+?)\s*(?:\n|Adem.s|$))?", re.S)
RE_ITAU_PAGO = re.compile(
    r"Cuenta origen terminada en\s*\.*\s*(\d{3,})\s*\.?\s*Monto:\s*(U\$S|US\$|USD|UYU|\$)\s*"
    r"([\d.,]+)\s*\.?\s*Pago a\s*(.+?)\s*\.?\s*(?:\n|Si no|$)", re.S)


def parse_transferencia_itau(body):
    """'Aviso transferencia realizada' de Itau. No trae el nombre del destinatario,
    solo su numero de cuenta y el banco.
    -> (sufijo_cuenta_origen, moneda, monto, numero_destino, banco_destino)"""
    m = RE_ITAU_TRANSF.search(body)
    if not m:
        raise ValueError("transferencia Itau: layout desconocido")
    return (m.group(1), m.group(3), num_itau(m.group(2)), m.group(4).lstrip("0"),
            (m.group(5) or "").strip())


def parse_pago_itau(body):
    """'Pago realizado' de Itau (pago de servicios desde la cuenta, ej. SISTARBANC).
    -> (sufijo_cuenta_origen, moneda, monto, a_quien)"""
    m = RE_ITAU_PAGO.search(body)
    if not m:
        raise ValueError("pago Itau: layout desconocido")
    moneda = "UYU" if m.group(2) in ("$", "UYU") else "USD"
    return m.group(1), moneda, num_itau(m.group(3)), m.group(4).strip()


def parse_transferencia(body):
    """Aviso de transferencia.
    -> (titular, concepto, moneda, monto, fecha_iso, tipo_cuenta_origen)"""
    v = campos_transferencia(body)
    titular = v["titular"]
    concepto = " ".join(x for x in (v["concepto origen"], v["concepto destino"]) if x).strip()
    moneda, monto = parse_importe(v["monto"])
    fecha = parse_fecha_valor(v["fecha valor"]).isoformat()
    return titular, concepto, moneda, monto, fecha, (v["tipo"] or "").lower()


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


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reprocesar", action="store_true",
                    help="regenera desde cero las filas origen=mail")
    args = ap.parse_args()

    tarjetas = cargar_tarjetas()
    cuentas_validas = cargar_cuentas_validas()
    cuentas_propias = cargar_cuentas_propias()
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
                                   (r0.get("frac_casa") or "").strip(),
                                   (r0.get("frac_deudas") or "").strip(),
                                   (r0.get("contrapartes") or "").strip()))

    def motivo_split(texto, categoria, monto_uyu, ambito_actual=""):
        """-> (motivo, detalle) si el gasto debe recordarse para Splitwise.
        Solo marca: la division efectiva SIEMPRE requiere OK de Joaco en la revision."""
        for pat, fcasa, fdeudas, quienes in splits_pat:
            if pat.search(texto):
                partes = []
                if fdeudas:
                    partes.append(f"Deudas {fdeudas} ({quienes or '?'})")
                if fcasa:
                    partes.append(f"casa {fcasa}")
                return ("split_a_cargar",
                        f"split recurrente propuesto: {' + '.join(partes) or quienes}: {texto!r} — aplicar con OK en revision")
        if umbral_split and categoria in CATS_SPLIT_HEURISTICA \
                and float(monto_uyu or 0) >= umbral_split \
                and ambito_actual != "casa":
            return ("split_a_cargar",
                    f"{categoria} por {monto_uyu} UYU (>= umbral {umbral_split:.0f}): cargar a Splitwise?")
        return None

    ledger = leer_csv(LEDGER, COLUMNS)
    procesados = leer_csv(PROCESADOS, PROC_COLS)
    pendientes = leer_csv(PENDIENTES, PEND_COLS)
    facturas = leer_csv(FACTURAS, FACT_COLS)

    if args.reprocesar:
        # Antes de tirar las filas mail, guardar sus marcas `conciliado` en
        # data/fuentes/conciliados.csv: las que un conciliador puso DESPUES del
        # ultimo merge solo existen aca y se perdian (medido el 2/10/2026).
        fuentes.aplicar_conciliados(ledger)
        ids_mail = {r["id"] for r in ledger if r["origen"] == "mail"}
        ledger = [r for r in ledger if r["origen"] != "mail"]
        # Solo se tiran los pendientes que ESTE script regenera. Los que crean
        # otros (conciliadores, Splitwise, la planilla) sobre filas mail no
        # vuelven a nacer aca: tirarlos era perderlos en silencio (medido el
        # 2/10/2026 con 5 sin_respaldo_estado abiertos de la Platinum).
        pendientes = [p for p in pendientes
                      if p["id_ledger"] not in ids_mail or p["motivo"] in MOTIVOS_AJENOS]
        mids = {r["message_id"] for r in procesados}
        facturas = [fa for fa in facturas if fa["message_id"] not in mids]
        procesados = []

    ya_procesados = {r["message_id"] for r in procesados}
    # Red de seguridad (auditoria 2026-07 B3): el ledger y procesados.csv se
    # escriben en dos pasos. Si una corrida muere entre medio, el mail queda en el
    # ledger pero NO en el log -> la corrida siguiente lo reprocesa e inserta la
    # fila de nuevo. Como el id es determinista (hid(message_id)), serian dos filas
    # con el MISMO id: merge_y_validar lo caza como FALLO y frena el pipeline hasta
    # arreglo a mano. Se saltea todo mail cuya fila ya existe en el ledger.
    # fuente_id de una fila origen=mail ES su message_id. Va despues del filtro de
    # --reprocesar, que ya vacio las filas mail: ahi el set queda vacio y no
    # interfiere con la regeneracion.
    ya_procesados |= {r["fuente_id"] for r in ledger
                      if r["origen"] == "mail" and r["fuente_id"]}

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

    def clasificar_destino(r, titular, concepto, moneda, monto):
        """Reglas 1-3 de una transferencia a alguien que no es una cuenta propia
        (las comparten los avisos de Santander y los de Itau). Completa `r` y
        devuelve los motivos de pendiente."""
        texto = f"{titular} {concepto}"
        motivos = []
        if re.search(r"mercado pago", titular, re.I) or re.search(r"\bcasa\b", concepto, re.I):
            # regla 1: aporte a la cuenta casa
            r.update(tipo="transferencia", cuenta_destino="Cuenta Casa",
                     ambito="interno", en_analisis="0",
                     descripcion=concepto or "Aporte")
        elif re.search(r"\blu\b", titular, re.I):
            # regla 2: transferencia a Lu. Si el concepto matchea una regla de
            # comercio (ej. "bigua" -> Club) es un gasto pagado via Lu; si no,
            # queda a clasificar (puede ser movimiento de casa).
            categoria, amb_override, hubo = categorizar(concepto, reglas)
            if hubo and categoria:
                r.update(tipo="gasto", comercio=f"{categoria} (via Lu)",
                         categoria=categoria, ambito=amb_override or "personal")
            else:
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
        return motivos

    def formato_nuevo(mail, handler, exc):
        """Un mail que el parser no supo leer NO frena la corrida: queda como
        pendiente y el resto sigue. Antes una excepcion aca cortaba el pipeline
        entero -- Splitwise, dashboard y Sheets incluidos -- hasta arreglo a mano
        (auditoria 2026-07 M1). El contrato de diseño siempre fue este: formato
        desconocido -> pendiente, nunca perdido en silencio.

        El id_ledger es el que la fila HABRIA tenido (no hay fila que apuntar):
        misma convencion que los pendientes de Splitwise que nunca insertan fila.
        Se loguea en procesados.csv para no reintentar en cada corrida; para
        recuperarlo: arreglar el parser y correr con --reprocesar."""
        detalle = (f"{mail['from']} | {mail['subject']!r} -> "
                   f"{type(exc).__name__}: {exc}")
        pend(hid(mail["message_id"]), "formato_nuevo", detalle)
        log_proc(mail, handler, "formato_nuevo", hid(mail["message_id"]), detalle)

    def router(mail):
        frm, subj = mail["from"].lower(), mail["subject"]
        if "notificaciones@santander.com.uy" in frm:
            if subj.startswith("Aviso: uso de tarjeta"):
                return "aviso_tarjeta"
            if subj.lower().startswith("aviso de transferencia"):
                return "transferencia"
            if "devoluci" in subj.lower() or "reintegro" in subj.lower():
                return "devolucion"
            return "ignorado"
        if "itau.com.uy" in frm:
            s = subj.lower()
            if "rechaz" in s:
                return "ignorado"
            if s.startswith("aviso de consumo aprobado"):
                return "aviso_itau"
            if s.startswith("aviso de consumo reembolsado") or s.startswith("aviso de consumo revers"):
                return "devolucion_itau"
            if "transferencia realizada" in s:
                return "transferencia_itau"
            if s.startswith("pago realizado"):
                return "pago_itau"
            # algo que parece un movimiento pero no conocemos (devolucion,
            # transferencia...): pendiente formato_nuevo, nunca perdido en silencio
            if re.search(r"consumo|compra|devoluci|reintegro|anulaci|transferencia|"
                         r"d.bito|cr.dito|pago", s) \
                    and not re.search(r"solicitud|aprobada|entrega|en camino|inicio de sesi|millas", s):
                return "itau_desconocido"
            return "ignorado"
        if "mercadopago.com" in frm and "transferencia fue enviada" in subj.lower():
            return "transferencia_mp"
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
        h = router(mail)
        if mail["message_id"] in ya_procesados or h not in ("aviso_tarjeta", "aviso_itau"):
            continue
        try:
            if h == "aviso_itau":
                tarjeta, moneda, monto, est_crudo, fh = parse_aviso_itau(mail)
            else:
                tarjeta, moneda, monto, est_crudo, fh = parse_aviso(mail["body_text"])
        except Exception as e:
            formato_nuevo(mail, "aviso_tarjeta", e)
            continue
        fecha = fh.date().isoformat()
        if h == "aviso_itau" and monto == 0:
            # Itau avisa con importe 0 los usos sin cargo (ej. Dragonpass, sala
            # VIP de la Infinite): no es un movimiento
            log_proc(mail, "aviso_tarjeta", "ignorado",
                     detalle=f"{tarjeta} {moneda} 0 {est_crudo} (importe 0, sin cargo)")
            continue

        if fecha < BACKFILL_DESDE:
            log_proc(mail, "aviso_tarjeta", "ignorado_pre_backfill",
                     detalle=f"{tarjeta} {moneda} {fmt(monto)} {est_crudo} ({fecha} < {BACKFILL_DESDE})")
            continue

        cuenta, ambito = tarjetas.get(tarjeta, ("", "personal"))
        cuenta = cuenta_en_moneda(cuenta, moneda, cuentas_validas)
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
        if tarjeta == "8261" and not params.get("ambito_8261_confirmado"):
            motivos.append(("ambito_a_confirmar",
                            "transicion jul-2026: confirmar vuelta de la casa a Santander (8261)"))
        ms = motivo_split(est_crudo, categoria, r["monto_uyu"], r["ambito"])
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

    # -------------------------------------------------- 1b. devoluciones / reintegros
    # Mismo formato de campos que un aviso de uso de tarjeta (VISA/Importe/
    # Establecimento/Fecha y hora); cambia solo el encabezado. Se registra como
    # gasto con monto NEGATIVO (revierte el cargo): así resta de la categoria en
    # el resumen (que suma monto_uyu de tipo=gasto) y netea a 0 en la tarjeta para
    # la conciliacion. Linkea al cargo original si lo encuentra.
    for mail in mails:
        h = router(mail)
        if mail["message_id"] in ya_procesados or h not in ("devolucion", "devolucion_itau"):
            continue
        try:
            if h == "devolucion_itau":
                tarjeta, moneda, monto, est_crudo, fh = parse_aviso_itau(mail)
            else:
                tarjeta, moneda, monto, est_crudo, fh = parse_aviso(mail["body_text"])
        except Exception as e:
            formato_nuevo(mail, "devolucion", e)
            continue
        fecha = fh.date().isoformat()

        if fecha < BACKFILL_DESDE:
            log_proc(mail, "devolucion", "ignorado_pre_backfill",
                     detalle=f"{tarjeta} {moneda} {fmt(monto)} {est_crudo} ({fecha} < {BACKFILL_DESDE})")
            continue

        cuenta, ambito = tarjetas.get(tarjeta, ("", "personal"))
        cuenta = cuenta_en_moneda(cuenta, moneda, cuentas_validas)
        if not cuenta:
            log_proc(mail, "devolucion", "ignorado", detalle=f"tarjeta desconocida {tarjeta}")
            continue

        categoria, amb_override, _ = categorizar(est_crudo, reglas)
        if amb_override:
            ambito = amb_override

        # buscar el cargo original que revierte: mismo cuenta + importe + comercio,
        # gasto positivo, fecha <= la del reintegro; el mas reciente gana. Si el
        # cargo tenia categoria/ambito resueltos, el reintegro los hereda.
        link = ""
        candidatos_rev = [c for c in (nuevos + filas_mail_existentes)
                          if c["tipo"] == "gasto" and c["cuenta"] == cuenta
                          and c["monto"] == fmt(monto)
                          and float(c["monto_uyu"] or 0) > 0
                          and limpiar_establecimiento(c["comercio"]) == limpiar_establecimiento(est_crudo)
                          and c["fecha"] <= fecha]
        if candidatos_rev:
            orig = max(candidatos_rev, key=lambda c: c["fecha"])
            link = orig["id"]
            if not categoria:
                categoria, ambito = orig["categoria"], orig["ambito"]

        monto_neg = -monto
        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], tipo="gasto", monto=fmt(monto_neg),
                 moneda=moneda, cuenta=cuenta, categoria=categoria, comercio=est_crudo,
                 descripcion="Reintegro " + limpiar_establecimiento(est_crudo)
                             + (f" (revierte {link})" if link else " (sin cargo original identificado)"),
                 ambito=ambito)
        if moneda == "UYU":
            r["monto_uyu"] = fmt(monto_neg)
        else:
            r["monto_uyu"] = fmt(round(monto_neg * tc[moneda], 2))
        motivos = []
        if moneda != "UYU":
            motivos.append(("tc_estimado",
                            f"reintegro {moneda} {fmt(monto)} x tc {tc[moneda]} (corrige Fase 3)"))
        if not categoria:
            motivos.append(("sin_categoria",
                            f"reintegro {est_crudo!r} sin regla ni cargo original"))
        if motivos:
            r["estado"] = "pendiente"
            for mo, de in motivos:
                pend(r["id"], mo, de)
        nuevos.append(r)
        log_proc(mail, "devolucion", "insertado", r["id"],
                 f"{tarjeta} {moneda} -{fmt(monto)} {est_crudo} -> {cuenta}/{ambito}/{categoria or '(sin cat)'}"
                 + (f" revierte {link}" if link else " (sin cargo original)"))

    # -------------------------------------------------- 2. transferencias
    for mail in mails:
        if mail["message_id"] in ya_procesados or router(mail) != "transferencia":
            continue
        try:
            titular, concepto, moneda, monto, fecha, tipo_origen = \
                parse_transferencia(mail["body_text"])
        except Exception as e:
            formato_nuevo(mail, "transferencia", e)
            continue

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

        # Transferencia CRUZADA: si el bloque 'Cuenta Origen' declara otra moneda,
        # ESA es la que se debita ('Monto:' es lo que recibe el destinatario).
        # Se guarda el importe del destino: si es UYU, es el monto_uyu EXACTO de
        # la operacion (mejor que estimarlo por tc).
        moneda_dst, monto_dst = moneda, monto
        deb = debito_origen_transferencia(mail["body_text"])
        if deb and deb[0] != moneda:
            moneda, monto = deb

        # cuenta origen segun el mail: la corriente es el fondo de la casa,
        # la caja de ahorro es la plata personal (confirmado 12/7); si el debito
        # es en USD, es la caja de ahorro en dolares.
        if moneda == "USD":
            cuenta_origen = "Santander USD"
        elif "corriente" in tipo_origen:
            cuenta_origen = "Cuenta Casa"
        else:
            cuenta_origen = "Santander UYU"

        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], monto=fmt(monto), moneda=moneda,
                 cuenta=cuenta_origen, descripcion=concepto)
        if moneda == "UYU":
            r["monto_uyu"] = fmt(monto)
        elif moneda_dst == "UYU":
            r["monto_uyu"] = fmt(monto_dst)   # exacto: lo declara el aviso
        texto = f"{titular} {concepto}"
        motivos = []
        propia, moneda_propia = cuentas_propias.get(numero_cuenta_destino(mail["body_text"]), ("", ""))

        if propia and propia not in (cuenta_origen, "Cuenta Casa"):
            # regla 0: a otra cuenta de Joaco (config/cuentas_bancarias.csv), ej.
            # Santander -> Itau desde oct-2026: movimiento interno, no gasto
            r.update(tipo="transferencia", cuenta_destino=propia, comercio=titular,
                     ambito="interno", en_analisis="0",
                     descripcion=concepto or f"Transferencia a {propia}")
            if not r["monto_uyu"]:
                # informativo (en_analisis=0, no suma en ningun agregado)
                r["monto_uyu"] = fmt(round(monto * tc[moneda], 2))
            if moneda_propia and moneda_propia != moneda:
                motivos.append(("transferencia_a_clasificar",
                                f"a {propia!r} ({moneda_propia}) pero se debitaron {moneda} "
                                f"{fmt(monto)}: transferencia cruzada, revisar los montos de cada lado"))
        else:
            motivos += clasificar_destino(r, titular, concepto, moneda, monto)
        # Una transferencia clasificada como GASTO en moneda extranjera necesita
        # monto_uyu: los agregados suman monto_uyu de tipo=gasto, y sin el la fila
        # queda invisible o crashea el reporte. El monto_uyu se setea arriba solo
        # para UYU (antes de saber el tipo); aca, ya decidido el tipo, se completa
        # para el resto de las monedas -- igual que el handler de avisos. Lo
        # destapo una seña de auto de USD 2000 el 21/7. tc estimado: corrige Fase 3.
        if r["tipo"] == "gasto" and r["moneda"] != "UYU" and not r["monto_uyu"]:
            r["monto_uyu"] = fmt(round(monto * tc[moneda], 2))
            motivos.append(("tc_estimado",
                            f"transferencia {moneda} {fmt(monto)} x tc {tc[moneda]} = "
                            f"{r['monto_uyu']} UYU (corrige Fase 3)"))
        if motivos:
            r["estado"] = "pendiente"
            for mo, de in motivos:
                pend(r["id"], mo, de)
        nuevos.append(r)
        log_proc(mail, "transferencia", "insertado", r["id"],
                 f"{mail['subject']} | {titular} {concepto!r} {moneda} {fmt(monto)} {fecha} "
                 f"-> {r['tipo']}/{r['ambito']}/{r['categoria'] or '(sin categoria)'}")

    # ------------------------------- 2c. transferencias y pagos desde Itau
    # Los avisos de Itau solo traen los ultimos digitos de la cuenta origen y el
    # NUMERO de la cuenta destino (sin nombre). La cuenta origen sale de
    # config/cuentas_bancarias.csv por sufijo; el destino, si es propio, de la
    # misma tabla, y si no, se le pone el alias con que aparece en los avisos de
    # Santander (Joaco agenda los mismos contactos: 8155077 = 'Lucho Royal
    # Barber'), para que las reglas de siempre lo clasifiquen.
    alias_por_numero = {}
    for mail in mails:
        if router(mail) == "transferencia":
            num = numero_cuenta_destino(mail["body_text"])
            tit = campos_transferencia(mail["body_text"]).get("titular", "")
            if num and tit:
                alias_por_numero[num] = tit     # mails en orden: gana el mas reciente

    def cuenta_itau(sufijo, moneda):
        cands = {c for n, (c, _) in cuentas_propias.items() if n.endswith(sufijo)}
        if len(cands) != 1:
            raise ValueError(f"cuenta origen Itau terminada en {sufijo}: "
                             f"{'no esta' if not cands else 'ambigua'} en config/cuentas_bancarias.csv")
        return cuenta_en_moneda(cands.pop(), moneda, cuentas_validas)

    for mail in mails:
        h = router(mail)
        if mail["message_id"] in ya_procesados or h not in ("transferencia_itau", "pago_itau"):
            continue
        try:
            if h == "transferencia_itau":
                suf, moneda, monto, num_dst, banco = parse_transferencia_itau(mail["body_text"])
            else:
                suf, moneda, monto, a_quien = parse_pago_itau(mail["body_text"])
            cuenta_origen = cuenta_itau(suf, moneda)
        except Exception as e:
            formato_nuevo(mail, h, e)
            continue
        fecha = (dt.datetime.fromisoformat(mail["fecha_mail"].replace("Z", "+00:00"))
                 - dt.timedelta(hours=3)).date().isoformat()
        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], monto=fmt(monto), moneda=moneda, cuenta=cuenta_origen)
        if moneda == "UYU":
            r["monto_uyu"] = fmt(monto)
        motivos = []
        if h == "pago_itau":
            # pago de servicios (factura). Si fuera el pago de una tarjeta seria
            # una transferencia: eso lo decide la revision.
            categoria, amb_override, _ = categorizar(a_quien, reglas)
            r.update(tipo="gasto", comercio=a_quien, descripcion=f"Pago a {a_quien}",
                     categoria=categoria, ambito=amb_override or "personal")
            if not categoria:
                motivos.append(("sin_categoria",
                                f"pago Itau a {a_quien!r} {moneda} {fmt(monto)}: que factura es? "
                                "(si es el pago de una tarjeta, va como transferencia)"))
            detalle = f"{suf} {moneda} {fmt(monto)} pago a {a_quien}"
        else:
            propia, moneda_propia = cuentas_propias.get(num_dst, ("", ""))
            if propia and propia != cuenta_origen:
                r.update(tipo="transferencia", cuenta_destino=propia, ambito="interno",
                         en_analisis="0", comercio=f"cuenta {num_dst}",
                         descripcion="Aporte" if propia == "Cuenta Casa" else f"Transferencia a {propia}")
                if moneda_propia and moneda_propia != moneda:
                    motivos.append(("transferencia_a_clasificar",
                                    f"a {propia!r} ({moneda_propia}) pero salieron {moneda} "
                                    f"{fmt(monto)}: transferencia cruzada, revisar los montos"))
            else:
                titular = alias_por_numero.get(num_dst) or f"cuenta {num_dst} ({banco or 'banco ?'})"
                r["descripcion"] = f"a cuenta {num_dst} {banco}".strip()
                motivos += clasificar_destino(r, titular, "", moneda, monto)
            detalle = f"{suf} {moneda} {fmt(monto)} -> {num_dst} {banco}"
        if r["tipo"] in ("gasto", "transferencia") and moneda != "UYU" and not r["monto_uyu"]:
            r["monto_uyu"] = fmt(round(monto * tc[moneda], 2))
            if r["tipo"] == "gasto":
                motivos.append(("tc_estimado", f"{moneda} {fmt(monto)} x tc {tc[moneda]} = "
                                               f"{r['monto_uyu']} UYU (corrige Fase 3)"))
        if motivos:
            r["estado"] = "pendiente"
            for mo, de in motivos:
                pend(r["id"], mo, de)
        nuevos.append(r)
        log_proc(mail, h, "insertado", r["id"],
                 f"{detalle} -> {r['cuenta']}/{r['tipo']}/{r['ambito']}/{r['categoria'] or '(sin categoria)'}")

    # ------------------------------------- 2b. retorno de fondo casa desde MP
    # "Tu transferencia fue enviada" (Mercado Pago -> Santander): la cuenta
    # casa estuvo parqueada en MP y este mail es el fondo volviendo a
    # Santander. Es un movimiento de fondos, no un aporte nuevo de nadie
    # (Mercado Pago se trata como CUENTAS_CASA en scripts/cierre_casa.py).
    for mail in mails:
        if mail["message_id"] in ya_procesados or router(mail) != "transferencia_mp":
            continue
        b = mail["body_text"]
        m = MP_TRANSFER_PAT.search(b)
        if not m:
            log_proc(mail, "transferencia_mp", "ignorado",
                     detalle="patron de monto no encontrado en el cuerpo")
            continue
        monto = float(m.group(1).replace(".", "").replace(",", "."))
        fecha_mail = dt.datetime.fromisoformat(mail["fecha_mail"].replace("Z", "+00:00"))
        fecha = (fecha_mail - dt.timedelta(hours=3)).date().isoformat()
        if fecha < BACKFILL_DESDE:
            log_proc(mail, "transferencia_mp", "ignorado_pre_backfill",
                     detalle=f"UYU {fmt(monto)} ({fecha})")
            continue
        r = fila_base(mail["message_id"])
        r.update(fecha=fecha, mes=fecha[:7], tipo="transferencia",
                 monto=fmt(monto), moneda="UYU", monto_uyu=fmt(monto),
                 cuenta="Mercado Pago", cuenta_destino="Cuenta Casa",
                 descripcion="Retorno fondo casa desde Mercado Pago",
                 ambito="interno", en_analisis="0")
        nuevos.append(r)
        log_proc(mail, "transferencia_mp", "insertado", r["id"],
                 f"MP -> Cuenta Casa UYU {fmt(monto)} ({fecha})")

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
            # \s tolera el espacio ("Total :") y el salto de línea que introduce
            # html_a_texto al partir la tabla en celdas (fetch determinístico).
            m = re.search(r"Total\s*:\s*(US\$|\$|€)\s*([\d.,]+)", b)
            if not m:
                return None
            out.update(moneda=mon(m.group(1)), monto=num(m.group(2)))
            mp = re.search(r"([^\n]+?)\s*\(de [^\n]+?\)\s*(?:US\$|\$|€)", b)
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
            ms = motivo_split(rec["producto"], match["categoria"], match["monto_uyu"], match["ambito"])
            if ms:
                pend(match["id"], *ms)
                match["estado"] = "pendiente"
            if match in filas_mail_existentes:
                ledger_modificado = True
            log_proc(mail, "recibo", "recibo_matcheado", match["id"],
                     f"{rec['producto']} {rec['moneda']} {fmt(rec['monto'])} "
                     f"-> enriquece comercio de {match['id']}")
        else:
            fecha = rec["fecha"].isoformat()
            cuenta = "Cuenta Casa" if rec["es_mp"] else fuentes.cuenta_personal_uyu(fecha)
            categoria, _, _ = categorizar(rec["producto"], reglas)
            r = fila_base(mail["message_id"])
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
            ms = motivo_split(rec["producto"], categoria, r["monto_uyu"], r["ambito"])
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
        elif h == "itau_desconocido":
            formato_nuevo(mail, "itau", ValueError(
                "mail de Itau que parece un movimiento pero no tiene parser"))
        elif h == "ignorado":
            log_proc(mail, "ignorado", "ignorado",
                     detalle=f"{mail['from']} | {mail['subject']}")

    # -------------------------------------------------- 5. resoluciones manuales
    # Correcciones persistentes por id: se re-aplican en TODA corrida, asi las
    # decisiones de la revision sobreviven a --reprocesar.
    resoluciones = leer_csv(RESOLUCIONES, RES_COLS)
    overrides = defaultdict(dict)      # id -> {columna: valor}
    resueltos = set()                  # (id, motivo) que no deben regenerarse
    for res in resoluciones:
        if res["campo"] == "motivo_resuelto":
            resueltos.add((res["id_ledger"], res["valor"]))
        elif res["campo"] in COLUMNS:
            overrides[res["id_ledger"]][res["campo"]] = res["valor"]

    # Bajas registradas (data/fuentes/borrados.csv, via `gastos.py borrar`): un
    # mail borrado se vuelve a parsear en cada --reprocesar. Se descarta aca para
    # que no vuelvan ni su fila ni sus pendientes (merge() tambien lo filtra).
    borrados = fuentes.leer_borrados()
    nuevos = [r for r in nuevos if r["id"] not in borrados]
    pend_nuevos = [p for p in pend_nuevos if p["id_ledger"] not in borrados]
    ledger = [r for r in ledger if r["id"] not in borrados]

    ledger_final = ledger + nuevos
    n_overrides = 0
    for r in ledger_final:
        for campo, valor in overrides.get(r["id"], {}).items():
            if r[campo] != valor:
                r[campo] = valor
                n_overrides += 1
                ledger_modificado = True
    pend_nuevos = [p for p in pend_nuevos
                   if (p["id_ledger"], p["motivo"]) not in resueltos]
    for p in pendientes:               # cerrar tambien los ya existentes
        if (p["id_ledger"], p["motivo"]) in resueltos and not p["resuelto"]:
            p["resuelto"] = "1"

    # estado coherente con la bandeja: una fila mail/manual queda pendiente
    # solo si tiene algun pendiente sin resolver (conciliado no se toca)
    ids_con_pendiente = {p["id_ledger"] for p in pendientes if not p["resuelto"]}
    ids_con_pendiente |= {p["id_ledger"] for p in pend_nuevos}
    for r in ledger_final:
        if r["origen"] in ("mail", "manual", "splitwise", "carga_manual") \
                and r["estado"] != "conciliado":
            deseado = "pendiente" if r["id"] in ids_con_pendiente else "confirmado"
            if r["estado"] != deseado:
                r["estado"] = deseado
                ledger_modificado = True

    # -------------------------------------------------- 6. persistir
    if args.reprocesar:
        # las filas regeneradas vuelven sin la marca: se re-aplica ya, sin
        # esperar al merge
        fuentes.aplicar_conciliados(ledger_final)
    ledger_final.sort(key=lambda r: (r["fecha"], r["id"]))
    if nuevos or ledger_modificado or args.reprocesar:
        escribir_csv(LEDGER, COLUMNS, ledger_final)
    escribir_csv(PROCESADOS, PROC_COLS, procesados + proc_nuevos)
    escribir_csv(PENDIENTES, PEND_COLS, pendientes + pend_nuevos)
    escribir_csv(FACTURAS, FACT_COLS, facturas + fact_nuevas)

    if mails:
        ultima = max(m["fecha_mail"] for m in mails)
        # `ultimo_sync` es informativo (nadie lo lee: el watermark que usa
        # fetch_mails.py es `ultima_fecha_mail`) y cambia en CADA corrida. En
        # local ensuciaba el arbol y hacia abortar `git pull` -- paso 3 veces
        # (auditoria 2026-09 SM7). Se preserva el valor que dejo Actions en vez
        # de borrar la clave, para que el archivo commiteado no oscile.
        estado = {}
        if os.path.exists(ESTADO_SYNC):
            with open(ESTADO_SYNC, encoding="utf-8") as f:
                estado = json.load(f)
        if os.environ.get("GITHUB_ACTIONS"):
            estado["ultimo_sync"] = ahora
        estado["ultima_fecha_mail"] = ultima
        # newline="\n": sin esto una corrida local escribe CRLF aunque
        # .gitattributes declare eol=lf (comprobado).
        with open(ESTADO_SYNC, "w", encoding="utf-8", newline="\n") as f:
            json.dump(estado, f, indent=2)

    # -------------------------------------------------- 6. reporte
    print(f"mails en inbox_raw: {len(mails)}; ya procesados (skip): "
          f"{sum(1 for m in mails if m['message_id'] in ya_procesados)}")
    print(f"resoluciones aplicadas: {n_overrides} campos; "
          f"pendientes suprimidos por resolucion: {len(resueltos)} (id,motivo)")
    cnt = Counter((p["handler"], p["resultado"]) for p in proc_nuevos)
    for (h, res), n in sorted(cnt.items()):
        print(f"  {h:15s} {res:28s} {n}")
    print(f"filas nuevas en ledger: {len(nuevos)}")
    for k, n in sorted(Counter((r["tipo"], r["cuenta"]) for r in nuevos).items()):
        print(f"  {k[0]:14s} {k[1]:20s} {n}")
    print(f"pendientes nuevos: {len(pend_nuevos)}")
    for k, n in sorted(Counter(p["motivo"] for p in pend_nuevos).items()):
        print(f"  {k:26s} {n}")
    # Mes del reporte: el ultimo presente en el ledger, no hardcodeado (antes
    # decia "2026-07" fijo; en agosto habria mostrado julio -- misma bomba que la
    # Fase 2). Y tolera monto_uyu vacio: este es un RESUMEN, no debe crashear el
    # pipeline (lo hizo el 21/7 con una seña de auto en USD). El guard de verdad
    # es merge_y_validar, que sale !=0 si un gasto no tiene monto_uyu.
    mes_rep = max((r["mes"] for r in ledger_final if r["mes"]), default="")
    jul, sin_uyu = defaultdict(float), 0
    for r in ledger_final:
        if r["mes"] == mes_rep and r["tipo"] == "gasto" and r["en_analisis"] == "1":
            try:
                jul[r["categoria"] or "(sin categoria)"] += float(r["monto_uyu"])
            except ValueError:
                sin_uyu += 1
    print(f"gasto {mes_rep} por categoria (en_analisis=1, todos los origenes):")
    for c, v in sorted(jul.items(), key=lambda kv: -kv[1]):
        print(f"  {c:26s} {v:12.2f}")
    print(f"  {'TOTAL':26s} {sum(jul.values()):12.2f}")
    if sin_uyu:
        print(f"  OJO: {sin_uyu} fila(s) gasto sin monto_uyu -- las bloquea merge_y_validar")


if __name__ == "__main__":
    main()
