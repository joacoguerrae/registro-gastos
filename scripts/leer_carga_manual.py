# -*- coding: utf-8 -*-
"""
Planilla "Carga manual" (auditoria 2026-09, Fase 1.3 / decision D3).

Lo que el banco no ve -- efectivo, gastos de casa que paga Lu, aportes a la
Cuenta Casa, saldos de efectivo e inversiones -- lo cargan Joaco o Lu desde el
celular en una Google Sheet. Este paso la baja y la convierte en datos:

  data/fuentes/carga_manual.csv     SNAPSHOT de la planilla (commiteado): es la
                                    fuente reproducible. --offline arma todo
                                    desde aca sin tocar la red.
  data/staging/ledger_carga_manual.csv   filas del ledger (origen=carga_manual)
                                    que merge_y_validar incorpora como staging.
  data/staging/aportes_lu_carga.csv      aportes de Lu (los lee cierre_casa).
  data/staging/saldos_carga.csv          saldos de efectivo/inversiones (Fase 5).
  data/pendientes.csv               lo invalido o dudoso, con detalle "[planilla]".

Pestañas (se matchean por NOMBRE de columna, sin importar mayusculas/acentos):
  Gastos:  fecha | monto | moneda | que fue | quien pago | casa/personal | categoria | nota
           quien pago: Joaco efectivo -> cuenta Efectivo; Lu tarjeta / Lu efectivo
           -> cierre_casa lo cuenta como aporte de Lu.
  Aportes: fecha | quien | monto | nota
           Lu -> aporte de Lu. Joaco -> transferencia de su cuenta en pesos -> Cuenta Casa.
  Saldos:  fecha | cuenta | monto | nota     (Efectivo UYU / Efectivo USD / Interactive Brokers / eToro)

Una fila invalida NO entra al ledger: genera un pendiente y se queda en la
planilla hasta que alguien la corrija (el pendiente se va solo). Una fila que ya
esta cargada por otra via (mismo monto, +-3 dias) tampoco entra: pendiente
`carga_manual_duplicado`; si de verdad es otra, se resuelve en la revision
(motivo_resuelto) y entra sola en la corrida siguiente.

El id de cada fila sale de su CONTENIDO: reordenar o insertar filas en la
planilla no cambia nada; EDITAR una fila la convierte en otra (la vieja sale, la
nueva entra, y si la vieja tenia una resolucion hay que rehacerla).

Guarda anti-borrado: si la planilla viene con mas de MAX_BAJA filas menos que el
snapshot anterior (alguien la vacio sin querer), NO se pisa el snapshot y el
paso sale != 0 (salud_pasos lo escala). --aceptar-bajas si fue a proposito.

Ejecutar:
  python scripts/leer_carga_manual.py               # baja la planilla y procesa
  python scripts/leer_carga_manual.py --offline     # solo desde el snapshot
  python scripts/leer_carga_manual.py --preparar-planilla   # listas desplegables,
                                          formatos e instrucciones (idempotente)
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

import fuentes
from procesar_mails import cargar_reglas, categorizar

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]
SNAPSHOT = P("data", "fuentes", "carga_manual.csv")
SNAP_COLS = ["pestana", "fila", "fecha", "monto", "moneda", "que", "quien", "ambito",
             "categoria", "cuenta", "nota"]
STAGING_LEDGER = P("data", "staging", "ledger_carga_manual.csv")
STAGING_APORTES = P("data", "staging", "aportes_lu_carga.csv")
APORTES_COLS = ["mes", "fecha", "monto", "nota"]
STAGING_SALDOS = P("data", "staging", "saldos_carga.csv")
SALDOS_COLS = ["fecha", "cuenta", "moneda", "saldo", "nota", "fila"]
LEDGER = P("data", "ledger.csv")
PENDIENTES = P("data", "pendientes.csv")
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]
RESOLUCIONES = P("data", "resoluciones.csv")
RES_COLS = ["id_ledger", "campo", "valor", "nota", "fecha"]
APORTES_LU = P("data", "aportes_lu.csv")
CATEG_CSV = P("config", "categorias.csv")
PARAMETROS = P("config", "parametros.json")
CREDS = P("config", "gcp_service_account.json")

PREFIJO = "[planilla]"         # marca los pendientes de este paso (se regeneran)
MAX_BAJA = 3
TOL_DIAS_DUP = 3

# encabezado normalizado -> campo del snapshot
ENCABEZADOS = {
    "Gastos": {"fecha": "fecha", "monto": "monto", "moneda": "moneda", "quefue": "que",
               "que": "que", "quienpago": "quien", "quien": "quien",
               "casapersonal": "ambito", "ambito": "ambito", "categoria": "categoria",
               "nota": "nota"},
    "Aportes": {"fecha": "fecha", "quien": "quien", "monto": "monto", "nota": "nota"},
    "Saldos": {"fecha": "fecha", "cuenta": "cuenta", "monto": "monto", "saldo": "monto",
               "nota": "nota"},
}
OBLIGATORIOS = {"Gastos": {"fecha", "monto", "que", "quien"},
                "Aportes": {"fecha", "quien", "monto"},
                "Saldos": {"fecha", "cuenta", "monto"}}
QUIEN_GASTO = {"joacoefectivo": "Efectivo", "joaco": "Efectivo", "efectivo": "Efectivo",
               "lutarjeta": "Lu tarjeta", "luefectivo": "Lu efectivo"}
CUENTAS_SALDO = {"efectivouyu": ("Efectivo", "UYU"), "efectivo": ("Efectivo", "UYU"),
                 "efectivousd": ("Efectivo", "USD"), "gletir": ("Gletir", "USD"),
                 "interactivebrokers": ("Interactive Brokers", "USD"),
                 "ibkr": ("Interactive Brokers", "USD"), "ib": ("Interactive Brokers", "USD"),
                 "etoro": ("Etoro", "USD")}   # Gletir queda por las filas viejas (sep-oct 2026)
MONEDAS = {"": "UYU", "uyu": "UYU", "$": "UYU", "pesos": "UYU", "usd": "USD", "us$": "USD",
           "u$s": "USD", "dolares": "USD", "eur": "EUR"}
# de donde sale un aporte de Joaco si no se aclara: la cuenta personal en pesos
# vigente a la fecha del aporte (Santander UYU -> Itau UYU en oct-2026)
DESDE_JOACO = fuentes.cuenta_personal_uyu


# ------------------------------------------------------------------ util
def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9$]+", "", s)


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
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def fmt(x):
    return f"{x:.2f}"


class Invalida(Exception):
    pass


def a_numero(v, que="monto"):
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d,.\-]", "", str(v))          # "$ 1.090", "U$S 25,99" -> solo el numero
    if not t:
        raise Invalida(f"falta el {que}")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        raise Invalida(f"{que} {v!r} no es un numero")


def serial_a_iso(v):
    """Fecha de Sheets sin formato (dias desde 30/12/1899) -> AAAA-MM-DD."""
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))).isoformat()


def a_fecha(v, hoy):
    """Serial de Sheets, ISO, dd/mm/aaaa, dd/mm/aa o dd/mm (año en curso)."""
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{5}(\.\d+)?", str(v).strip()):
        v = serial_a_iso(v)
    s = str(v).strip()
    if not s:
        raise Invalida("falta la fecha")
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            d = dt.datetime.strptime(s, f).date()
            break
        except ValueError:
            d = None
    if d is None:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)
        if not m:
            raise Invalida(f"fecha {s!r} no se entiende (usar dd/mm/aaaa)")
        d = dt.date(hoy.year, int(m.group(2)), int(m.group(1)))
        if d > hoy + dt.timedelta(days=7):
            d = d.replace(year=hoy.year - 1)
    if d > hoy + dt.timedelta(days=1):
        raise Invalida(f"fecha {d.isoformat()} en el futuro")
    if d.year < 2026:
        raise Invalida(f"fecha {d.isoformat()}: la planilla arranca en 2026")
    return d.isoformat()


def tc(moneda):
    if moneda == "UYU":
        return 1.0
    with open(PARAMETROS, encoding="utf-8") as f:
        return float(json.load(f)[f"tc_{moneda.lower()}_uyu"])


def rid(*partes):
    return hashlib.sha1("|".join(["carga_manual", *map(str, partes)]).encode()).hexdigest()


# ------------------------------------------------------------------ bajar (red)
def credenciales(scopes):
    from google.oauth2.service_account import Credentials
    sa_json = os.environ.get("GCP_SA_JSON")
    if sa_json:
        return Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)
    if not os.path.exists(CREDS):
        raise SystemExit(f"ERROR: falta {CREDS} (o GCP_SA_JSON en el entorno)")
    return Credentials.from_service_account_file(CREDS, scopes=scopes)


def abrir(scopes):
    import gspread
    with open(PARAMETROS, encoding="utf-8") as f:
        sheet_id = json.load(f).get("carga_manual_sheet_id")
    if not sheet_id:
        raise SystemExit("ERROR: falta carga_manual_sheet_id en config/parametros.json")
    return gspread.authorize(credenciales(scopes)).open_by_key(sheet_id)


def columnas(pestana, encabezado):
    """-> {campo: indice de columna}. Falla claro si falta una obligatoria."""
    cols = {}
    for i, h in enumerate(encabezado):
        campo = ENCABEZADOS[pestana].get(norm(h))
        if campo and campo not in cols:
            cols[campo] = i
    faltan = OBLIGATORIOS[pestana] - set(cols)
    if faltan:
        raise SystemExit(f"ERROR: la pestaña {pestana!r} no tiene la(s) columna(s) "
                         f"{sorted(faltan)} (encabezado: {encabezado}). Alguien renombro o "
                         "borro una columna: el snapshot NO se toca hasta que se arregle.")
    return cols


def bajar():
    """Planilla -> filas del snapshot. Valores SIN formato: los numeros llegan
    como numeros y las fechas como serial, sin depender del locale (es_ES)."""
    sh = abrir(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    hojas = {ws.title: ws for ws in sh.worksheets()}
    filas = []
    for pestana in ENCABEZADOS:
        ws = next((w for t, w in hojas.items() if norm(t) == norm(pestana)), None)
        if ws is None:
            raise SystemExit(f"ERROR: la planilla no tiene la pestaña {pestana!r} "
                             f"(hay: {list(hojas)}). El snapshot NO se toca.")
        valores = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        if not valores:
            continue
        cols = columnas(pestana, valores[0])
        for n, fila in enumerate(valores[1:], start=2):
            if not any(str(c).strip() for c in fila):
                continue
            r = {c: "" for c in SNAP_COLS}
            r.update(pestana=pestana, fila=str(n))
            for campo, i in cols.items():
                v = fila[i] if i < len(fila) else ""
                if campo == "fecha" and isinstance(v, (int, float)) and v:
                    v = serial_a_iso(v)      # sin validar: eso es de procesar()
                elif isinstance(v, float) and v.is_integer():
                    v = int(v)
                r[campo] = str(v).strip()
            filas.append(r)
    return filas


# ------------------------------------------------------------------ procesar
def procesar(snap, hoy):
    """Snapshot -> (filas_ledger, aportes_lu, saldos, pendientes). Puro: no toca disco."""
    categorias = {norm(r["categoria"]): r["categoria"]
                  for r in leer_csv(CATEG_CSV, ["categoria", "tipo", "orden"])
                  if r["tipo"] in ("Fijo", "Variable")}
    reglas = cargar_reglas()
    ledger = [r for r in leer_csv(LEDGER, COLUMNS) if r["origen"] != "carga_manual"]
    aportes_prev = leer_csv(APORTES_LU, APORTES_COLS)
    resueltos = {(r["id_ledger"], r["valor"]) for r in leer_csv(RESOLUCIONES, RES_COLS)
                 if r["campo"] == "motivo_resuelto"}

    filas, aportes, saldos, pend = [], [], [], []
    vistos = Counter()

    def pendiente(i, motivo, r, texto):
        pend.append(dict(id_ledger=i, motivo=motivo,
                         detalle=f"{PREFIJO} {r['pestana']} fila {r['fila']}: {texto}",
                         fecha_alta=hoy.isoformat(), resuelto=""))

    def parecido(cuenta_o_destino, monto, fecha, campo="cuenta", tipo=None):
        f0 = dt.date.fromisoformat(fecha)
        for x in ledger:
            if x[campo] == cuenta_o_destino and x["monto"] and abs(float(x["monto"]) - abs(monto)) < 0.005 \
                    and (tipo is None or x["tipo"] == tipo) \
                    and abs((dt.date.fromisoformat(x["fecha"]) - f0).days) <= TOL_DIAS_DUP:
                return x
        return None

    for r in snap:
        clave = (r["pestana"], r["fecha"], r["monto"], r["moneda"], norm(r["que"]),
                 norm(r["quien"]), norm(r["ambito"]), norm(r["cuenta"]))
        vistos[clave] += 1
        h = rid(*clave, vistos[clave])
        i, fuente_id = h[:12], f"carga:{h[:16]}"
        try:
            fecha = a_fecha(r["fecha"], hoy)
            monto = a_numero(r["monto"])
            if r["pestana"] == "Saldos":
                cuenta = CUENTAS_SALDO.get(norm(r["cuenta"]))
                if not cuenta:
                    raise Invalida(f"cuenta {r['cuenta']!r}: usar Efectivo UYU, Efectivo USD, Interactive Brokers o eToro")
                if monto < 0:
                    raise Invalida("un saldo no puede ser negativo")
                saldos.append(dict(fecha=fecha, cuenta=cuenta[0], moneda=cuenta[1], saldo=fmt(monto),
                                   nota=r["nota"], fila=r["fila"]))
                continue
            if monto == 0:
                raise Invalida("monto 0")

            if r["pestana"] == "Aportes":
                quien = norm(r["quien"])
                if quien not in ("lu", "joaco"):
                    raise Invalida(f"quien {r['quien']!r}: Lu o Joaco")
                if monto < 0:
                    raise Invalida("un aporte es positivo")
                nota = (r["nota"] + " " if r["nota"] else "") + f"(planilla fila {r['fila']})"
                if quien == "lu":
                    ya = next((a for a in aportes_prev if a["mes"] == fecha[:7]
                               and abs(float(a["monto"]) - monto) < 0.005), None)
                    if ya and (i, "carga_manual_duplicado") not in resueltos:
                        pendiente(i, "carga_manual_duplicado", r,
                                  f"aporte de Lu {fmt(monto)} ({fecha}) ya esta en data/aportes_lu.csv "
                                  f"({ya['nota'][:50]!r}): NO se cargo. Si es OTRO aporte, resolvelo "
                                  "en la revision y entra solo")
                        continue
                    aportes.append(dict(mes=fecha[:7], fecha=fecha, monto=fmt(monto), nota=nota))
                    continue
                ya = parecido("Cuenta Casa", monto, fecha, "cuenta_destino", "transferencia")
                if ya and (i, "carga_manual_duplicado") not in resueltos:
                    pendiente(i, "carga_manual_duplicado", r,
                              f"aporte de Joaco {fmt(monto)} ({fecha}) ya esta en el ledger "
                              f"({ya['id']}, {ya['origen']}, {ya['fecha']}): NO se cargo. Si es OTRO, "
                              "resolvelo en la revision y entra solo")
                    continue
                f = {c: "" for c in COLUMNS}
                f.update(id=i, fecha=fecha, mes=fecha[:7], tipo="transferencia", monto=fmt(monto),
                         moneda="UYU", monto_uyu=fmt(monto), cuenta=DESDE_JOACO(fecha),
                         cuenta_destino="Cuenta Casa", descripcion=f"Aporte de Joaco a la casa {nota}",
                         ambito="interno", origen="carga_manual", estado="confirmado",
                         en_analisis="0", fuente_id=fuente_id)
                filas.append(f)
                continue

            # ---- Gastos
            cuenta = QUIEN_GASTO.get(norm(r["quien"]))
            if not cuenta:
                raise Invalida(f"quien pago {r['quien']!r}: Joaco efectivo, Lu tarjeta o Lu efectivo")
            moneda = MONEDAS.get(norm(r["moneda"]))
            if not moneda:
                raise Invalida(f"moneda {r['moneda']!r}: UYU o USD")
            ambito = norm(r["ambito"]) or ("casa" if cuenta.startswith("Lu") else "")
            if ambito not in ("casa", "personal"):
                raise Invalida("casa/personal vacio o invalido: poner casa o personal")
            if cuenta.startswith("Lu") and ambito == "personal":
                raise Invalida("un gasto PERSONAL de Lu no va en el sistema (solo los de casa que paga ella)")
            if not r["que"]:
                raise Invalida("falta 'que fue'")
            ya = parecido(cuenta, monto, fecha)
            if ya and (i, "carga_manual_duplicado") not in resueltos:
                pendiente(i, "carga_manual_duplicado", r,
                          f"{cuenta} {fmt(monto)} ({fecha}, {r['que']!r}) ya esta en el ledger "
                          f"({ya['id']}, {ya['origen']}): NO se cargo. Si es OTRO, resolvelo en la "
                          "revision y entra solo")
                continue
            categoria = ""
            if r["categoria"]:
                categoria = categorias.get(norm(r["categoria"]))
                if not categoria:
                    raise Invalida(f"categoria {r['categoria']!r} no existe")
            else:
                categoria, _, _ = categorizar(r["que"], reglas)
            f = {c: "" for c in COLUMNS}
            f.update(id=i, fecha=fecha, mes=fecha[:7], tipo="gasto", monto=fmt(monto), moneda=moneda,
                     monto_uyu=fmt(monto * tc(moneda)), cuenta=cuenta, categoria=categoria,
                     comercio=r["que"][:60],
                     descripcion=r["que"] + (f" -- {r['nota']}" if r["nota"] else "")
                     + f" (planilla fila {r['fila']})",
                     ambito=ambito, origen="carga_manual", estado="confirmado", en_analisis="1",
                     fuente_id=fuente_id)
            if not categoria:
                pendiente(i, "sin_categoria", r,
                          f"{r['que']!r} {fmt(monto)} sin categoria: ponela en la planilla, o se "
                          "resuelve en la revision")
            filas.append(f)
        except Invalida as e:
            pendiente(i, "carga_manual_invalida", r, f"{e}. NO se cargo: corregila en la planilla")

    pend = [p for p in pend if (p["id_ledger"], p["motivo"]) not in resueltos]
    con_pend = {p["id_ledger"] for p in pend}
    for f in filas:
        if f["id"] in con_pend:
            f["estado"] = "pendiente"
    return filas, aportes, saldos, pend


def guardar(snap, filas, aportes, saldos, pend):
    escribir_csv(SNAPSHOT, SNAP_COLS, snap)
    escribir_csv(STAGING_LEDGER, COLUMNS, sorted(filas, key=lambda r: (r["fecha"], r["id"])))
    escribir_csv(STAGING_APORTES, APORTES_COLS, aportes)
    escribir_csv(STAGING_SALDOS, SALDOS_COLS, saldos)
    # Los pendientes de la planilla se REGENERAN: una fila corregida se lleva su
    # pendiente sola. Se conserva la fecha de alta de los que siguen iguales.
    previos = leer_csv(PENDIENTES, PEND_COLS)
    abiertos_prev = {(p["id_ledger"], p["motivo"]): p for p in previos
                     if p["detalle"].startswith(PREFIJO) and not p["resuelto"]}
    quedan = [p for p in previos if not (p["detalle"].startswith(PREFIJO) and not p["resuelto"])]
    for p in pend:
        prev = abiertos_prev.get((p["id_ledger"], p["motivo"]))
        if prev:
            p["fecha_alta"] = prev["fecha_alta"]
    escribir_csv(PENDIENTES, PEND_COLS, quedan + pend)
    return len([p for p in pend if (p["id_ledger"], p["motivo"]) not in abiertos_prev])


# ------------------------------------------------------------------ preparar la planilla
INSTRUCCIONES = [
    ["Pestaña", "Qué va", "Ejemplos"],
    ["Gastos", "Lo que pagaste en efectivo, y los gastos de CASA que paga Lu con su tarjeta o en efectivo",
     "La propina de 452 del retiro de 4.500; el Uber de 364 y el parabrisas de 4.100 que pagó Lu"],
    ["Aportes", "Las transferencias a la Cuenta Casa, que no mandan aviso", "Los aportes de Lu; tus giros a la Cuenta Casa"],
    ["Saldos", "Una vez por mes: efectivo, dólares en mano, inversiones", "Efectivo UYU, Efectivo USD, Interactive Brokers, eToro"],
    [],
    ["Cómo funciona"],
    ["", "El sistema lee esta planilla dos veces por día. Lo que cargues aparece en el dashboard y en la posición de casa en la corrida siguiente."],
    ["", "NO cargues lo que se paga con tarjetas, cuentas del banco o Mercado Pago: eso llega solo por mail o por el estado de cuenta."],
    ["", "Un gasto de casa que paga Lu cuenta solo como aporte de ella. No hace falta cargarlo también en Aportes."],
    ["", "Si una fila está mal (falta la fecha, monto raro, categoría que no existe) NO se carga: queda en la bandeja de pendientes hasta que la corrijas acá."],
    ["", "Si algo ya estaba cargado (mismo monto, ±3 días) tampoco entra dos veces: queda como pendiente para confirmar."],
    ["", "Categoría es opcional: si la dejás vacía, se infiere de 'que fue' o queda para la revisión."],
    ["", "Corregir una fila está bien. Borrar filas viejas en masa, no: el sistema se niega a tomar una planilla que perdió muchas filas de golpe."],
]


def preparar_planilla():
    sh = abrir(["https://www.googleapis.com/auth/spreadsheets"])
    categorias = [r["categoria"] for r in leer_csv(CATEG_CSV, ["categoria", "tipo", "orden"])
                  if r["tipo"] in ("Fijo", "Variable")]
    listas = {("Gastos", "moneda"): ["UYU", "USD"],
              ("Gastos", "quien"): ["Joaco efectivo", "Lu tarjeta", "Lu efectivo"],
              ("Gastos", "ambito"): ["casa", "personal"],
              ("Gastos", "categoria"): categorias,
              ("Aportes", "quien"): ["Lu", "Joaco"],
              ("Saldos", "cuenta"): ["Efectivo UYU", "Efectivo USD", "Interactive Brokers", "eToro"]}
    reqs = []
    hojas = {ws.title: ws for ws in sh.worksheets()}
    for pestana in ENCABEZADOS:
        ws = next(w for t, w in hojas.items() if norm(t) == norm(pestana))
        cols = columnas(pestana, ws.row_values(1))
        rango = lambda c: {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": ws.row_count,
                           "startColumnIndex": c, "endColumnIndex": c + 1}
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": ws.id,
                     "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
        reqs.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                     "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                     "fields": "userEnteredFormat.textFormat.bold"}})
        reqs.append({"setDataValidation": {"range": rango(cols["fecha"]), "rule": {
            "condition": {"type": "DATE_IS_VALID"}, "strict": True,
            "inputMessage": "Fecha (dd/mm/aaaa). En el celular se abre un calendario."}}})
        reqs.append({"repeatCell": {"range": rango(cols["fecha"]),
                     "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}},
                     "fields": "userEnteredFormat.numberFormat"}})
        cond = ({"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "0"}]}
                if pestana == "Saldos" else
                {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]}
                if pestana == "Aportes" else
                {"type": "NUMBER_NOT_EQ", "values": [{"userEnteredValue": "0"}]})
        reqs.append({"setDataValidation": {"range": rango(cols["monto"]), "rule": {
            "condition": cond, "strict": True,
            "inputMessage": "Monto en números (1090 o 1090,50). Negativo = devolución."
            if pestana == "Gastos" else "Monto en números."}}})
        reqs.append({"repeatCell": {"range": rango(cols["monto"]),
                     "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                     "fields": "userEnteredFormat.numberFormat"}})
        for (p, campo), valores in listas.items():
            if p == pestana and campo in cols:
                reqs.append({"setDataValidation": {"range": rango(cols[campo]), "rule": {
                    "condition": {"type": "ONE_OF_LIST",
                                  "values": [{"userEnteredValue": v} for v in valores]},
                    "strict": True, "showCustomUi": True}}})
    sh.batch_update({"requests": reqs})
    ws = next((w for t, w in hojas.items() if norm(t) == norm("Que Va")), None)
    if ws is None:
        ws = sh.add_worksheet("Que Va", rows=40, cols=4)
    ws.clear()
    ancho = max(len(f) for f in INSTRUCCIONES)
    ws.update([f + [""] * (ancho - len(f)) for f in INSTRUCCIONES], "A1", value_input_option="RAW")
    sh.batch_update({"requests": [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": r, "endRowIndex": r + 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold"}} for r in (0, 5)]})
    print(f"planilla preparada: {len(reqs)} reglas de formato/validacion + instrucciones en 'Que Va'")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no bajar: procesar el snapshot commiteado")
    ap.add_argument("--aceptar-bajas", action="store_true",
                    help="tomar la planilla aunque haya perdido mas de %d filas" % MAX_BAJA)
    ap.add_argument("--preparar-planilla", action="store_true",
                    help="listas desplegables, formatos e instrucciones (idempotente)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    if args.preparar_planilla:
        preparar_planilla()
        return 0

    previo = leer_csv(SNAPSHOT, SNAP_COLS)
    if args.offline:
        snap = previo
    else:
        snap = bajar()
        if len(previo) - len(snap) > MAX_BAJA and not args.aceptar_bajas:
            print(f"ERROR: la planilla trae {len(snap)} filas y el snapshot anterior {len(previo)}: "
                  f"perdio {len(previo) - len(snap)} de golpe (tope {MAX_BAJA}). Parece que se vacio "
                  "sin querer: NO se pisa data/fuentes/carga_manual.csv y todo sigue con lo "
                  "anterior. Si fue a proposito: --aceptar-bajas.", file=sys.stderr)
            return 3
    hoy = dt.date.today()
    filas, aportes, saldos, pend = procesar(snap, hoy)
    nuevos = guardar(snap, filas, aportes, saldos, pend)
    por_pest = Counter(r["pestana"] for r in snap)
    print(f"planilla Carga manual: {len(snap)} filas ({dict(por_pest)}) -> "
          f"{len(filas)} al ledger, {len(aportes)} aportes de Lu, {len(saldos)} saldos; "
          f"{len(pend)} pendiente(s) abierto(s)")
    print(f"pendientes nuevos (planilla): {nuevos}")
    for p in pend:
        print(f"  {p['motivo']:24s} {p['detalle'][len(PREFIJO) + 1:][:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
