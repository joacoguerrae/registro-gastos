# -*- coding: utf-8 -*-
"""
Fase 3 — Motor de conciliacion de estados de cuenta (docs/fase3.md seccion 2).

Dos capas separadas a proposito:

  (a) EXTRACTORES DE PDF por emisor. CALIBRADOS el 2026-07-16 contra 5 PDFs
      reales (3 BBVA may/jun/jul + 2 Santander MC may/jun 2026), que ahora
      fetch_mails.py baja solo de los adjuntos del mail. Validacion: la suma de
      las lineas extraidas coincide EXACTO con el total declarado por cada
      estado. Tambien vive aca el desbloqueo con contrasena (CI, en secrets):
      BBVA viene sin encriptar, Santander encriptado.
      OJO: el PDF de Santander no dice "MASTERCARD" ni "VISA" -> la tarjeta se
      identifica por el numero de cuenta (config/cuentas_bancarias.csv).

  (b) MOTOR DE MATCHING (funcion `conciliar`): NO depende de PDF. Recibe un
      dict "estado" ya extraido (o generado a mano/sintetico) + una lista de
      filas del ledger, y devuelve conciliaciones/inserciones/pendientes/
      cierre. Es la parte que se testea HOY, sin esperar un PDF real, en
      tests/test_conciliar_estado.py.

Contrato del dict "estado" (lo produce (a) o un generador sintetico):
    {
      "emisor": str,                 # informativo
      "cuenta": str,                 # cuenta canonica del ledger (config/cuentas.csv)
      "periodo": "YYYY-MM",
      "vencimiento": "YYYY-MM-DD" (opcional),
      "lineas": [
          {"fecha": "YYYY-MM-DD", "descripcion": str, "monto": float,
           "moneda": "UYU"|"USD"|"EUR", "monto_uyu": float opcional
           (equivalente en pesos que muestra el banco -- solo tiene sentido
           para lineas no-UYU; si esta presente y difiere del monto_uyu
           estimado de la fila del ledger, corrige via resoluciones.csv)},
          ...
      ],
      "total": {"UYU": float, "USD": float, ...},   # total del periodo por moneda
    }

Salidas en modo real (procesando PDFs de data/estados/):
  - data/ledger.csv                       (filas conciliadas + insertadas origen=estado_cuenta)
  - data/pendientes.csv                   (sin_respaldo_estado, sin_categoria)
  - data/resoluciones.csv                 (correccion persistente de monto_uyu con TC real)
  - data/staging/estados_procesados.csv   (log por PDF -- idempotencia)

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/conciliar_estado.py [archivo.pdf ...]
  (sin argumentos: procesa todos los PDF de data/estados/ que no esten en el log)

REGLA DE SECRETOS: la contrasena de los PDF (CI, config/secrets.local.json,
clave ci_pdf_password) nunca se imprime ni se loguea.
"""
import argparse
import csv
import datetime as dt
import difflib
import glob
import hashlib
import json
import os
import re
from collections import defaultdict

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

LEDGER = P("data", "ledger.csv")
PENDIENTES = P("data", "pendientes.csv")
RESOLUCIONES = P("data", "resoluciones.csv")
ESTADOS_DIR = P("data", "estados")
ESTADOS_PROCESADOS = P("data", "staging", "estados_procesados.csv")
CUENTAS_CSV = P("config", "cuentas.csv")
REGLAS_CSV = P("config", "reglas_comercio.csv")
PARAMETROS = P("config", "parametros.json")
SECRETS = P("config", "secrets.local.json")

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]
RES_COLS = ["id_ledger", "campo", "valor", "nota", "fecha"]
PROC_COLS = ["archivo", "emisor", "cuenta", "periodo", "procesado_en",
             "matcheadas", "insertadas", "sin_respaldo", "diferencia_cierre"]

# tolerancias de matching (docs/fase3.md #2): monto +-0.5%, fecha +-3 dias.
# El "comercio fuzzy" es una senal secundaria (desempate / sanity check), NO
# un filtro duro: un estado real trunca/abrevia descripciones mucho mas que
# los avisos de mail, y monto+fecha+moneda ya son una firma bastante fuerte
# dentro del periodo de una sola tarjeta.
TOL_MONTO_PCT = 0.005
TOL_MONTO_MIN = 0.01
TOL_DIAS = 3
# Compras en MONEDA EXTRANJERA: el aviso por mail trae el importe del comercio y
# el estado el que factura el banco, que le suma un recargo fijo (~3% medido el
# 2/8/2026 sobre Google One 1,99->2,05 | Anthropic 97,47->100,45 | Apple
# 23,99->24,72). Con la tolerancia de 0,5% no matcheaban y cada suscripcion
# mensual se insertaba DUPLICADA. Se afloja solo para no-UYU; el riesgo de falso
# positivo lo acota que ademas tienen que coincidir fecha (+-3 dias) y
# descripcion (UMBRAL_FUZZY_MINIMO). El estado manda: el monto se corrige al suyo.
TOL_MONTO_PCT_ME = 0.06
UMBRAL_FUZZY_MINIMO = 0.20   # piso de cordura; por debajo, no se acepta ni de casualidad

# ambito por cuenta para filas INSERTADAS (origen=estado_cuenta): mismas
# reglas de esquema.md (Casa->casa, Deudas->split) + la excepcion de fase3
# para la Mastercard de la casa. Resto -> personal.
AMBITO_POR_CUENTA = {"Cuenta Casa": "casa", "Santander Mastercard": "casa",
                     "Deudas": "split"}


# ------------------------------------------------------------------ utilidades comunes
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
    """Primera regla que matchea gana. -> (categoria, ambito_override, hubo_match)."""
    for pat, cat, amb in reglas:
        if pat.search(texto):
            return cat, amb, True
    return "", "", False


def ambito_de_cuenta(cuenta):
    return AMBITO_POR_CUENTA.get(cuenta, "personal")


def fmt(x):
    return f"{x:.2f}"


def hid(origen, fuente_id):
    return hashlib.sha1((origen + fuente_id).encode()).hexdigest()[:12]


def normalizar(txt):
    return re.sub(r"[^a-z0-9 ]", "", (txt or "").lower()).strip()


def similares(a, b):
    return difflib.SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()


# ================================================================== (a) EXTRACTORES DE PDF
class ExtraccionPendienteError(Exception):
    """El extractor de este emisor todavia no fue calibrado con un PDF real."""


def desbloquear_pdf(path, password):
    """Devuelve el texto plano de un PDF, desencriptandolo con `password` si
    hace falta. Nunca imprime ni loguea la contrasena.

    Libreria usada: pypdf (unica libreria de manejo de PDF instalada en el
    Python de anaconda al 2026-07-12; ni pikepdf ni PyPDF2 estan disponibles
    -- pypdf es el sucesor mantenido de PyPDF2 y expone la misma API de
    lectura/decrypt, asi que cumple el contrato pedido).
    """
    import pypdf
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        resultado = reader.decrypt(password)
        if int(resultado) == 0:
            raise ValueError(f"{os.path.basename(path)}: contrasena incorrecta o PDF no desbloqueable")
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def detectar_emisor(texto):
    t = texto.upper()
    if "BBVA" in t:
        return "BBVA"
    # OJO: el PDF de las tarjetas Santander NO dice "MASTERCARD" ni "VISA" (verificado
    # con los estados reales de may/jun 2026). Se identifica el banco aca y la tarjeta
    # concreta por el numero de cuenta -> _cuenta_santander().
    if "SANTANDER" in t and "SU ESTADO DE CUENTA" in t:
        return "SANTANDER_TARJETA"
    return None


def _cuenta_santander(texto):
    """Mapea el numero de cuenta que trae el estado (ej '800944254160') a la cuenta
    del ledger, via config/cuentas_bancarias.csv. Como Mastercard y Visa Platinum
    llegan del mismo remitente y con el mismo layout, el numero es lo unico que las
    distingue: cuando llegue el primer estado de la Visa, alcanza con agregar su
    numero a ese CSV (el error de abajo lista los candidatos encontrados)."""
    path = P("config", "cuentas_bancarias.csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                num = (r.get("numero") or "").strip()
                # las cuentas Itau no son de este PDF, y el match es por
                # substring: sus numeros (7 digitos) podrian aparecer sueltos
                if r["cuenta_ledger"].strip().startswith("Itaú"):
                    continue
                if num and num in texto:
                    return r["cuenta_ledger"].strip()
    nums = sorted(set(re.findall(r"\b\d{10,14}\b", texto)))
    raise ValueError(
        "estado Santander: no pude identificar que tarjeta es. Numeros hallados en el "
        f"PDF: {nums}. Agregá el que corresponda a config/cuentas_bancarias.csv "
        "(numero -> cuenta_ledger: 'Santander Mastercard' o 'Santander Platinum').")


def _num_us(s):
    """'-1,647.16' -> -1647.16 (BBVA: coma=miles, punto=decimal)."""
    return float(s.replace(",", ""))


def _num_uy(s):
    """'6.938,82' -> 6938.82 (Santander: punto=miles, coma=decimal)."""
    return float(s.replace(".", "").replace(",", "."))


RE_BBVA_LINEA = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})$")
RE_BBVA_FEE = re.compile(r"^(SEGURO VIDA.*?)\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})$")


def extraer_bbva(texto):
    """Parser del estado BBVA Visa. Calibrado 2026-07-16 con 3 PDFs reales
    (may/jun/jul 2026). BBVA NO manda avisos por transaccion -> este PDF es la
    fuente primaria de todos sus consumos.

    Formato (el PDF no esta encriptado):
        Fecha de cierre 02/07/2026 Limite de credito 90,000.00
        Vencimiento actual 16/07/2026 ...
        Fecha Descripcion Pesos Dolares
        SALDO ANTERIOR -1,647.16 173.05
        04/06/2026 SU PAGO U$S 0.00 -173.05          <- pago, NO es consumo
        SEGURO VIDA 2,5X1000 11.67 0.00              <- cargo sin fecha
        28/05/2026 VELOV INTERNET (FR ,EUR, 4,00) 0.00 4.67
        07/06/2026 MAPFRE 204.51 0.00
        Total a pagar -1,430.98 150.37

    Dos columnas de monto (Pesos, Dolares): la que no es 0 define la moneda.
    Los consumos en EUR se facturan en USD y el PDF deja el original entre
    parentesis en la descripcion. Numeros en formato US.
    """
    cierre = re.search(r"Fecha de cierre\s+(\d{2}/\d{2}/\d{4})", texto)
    if not cierre:
        raise ValueError("extraer_bbva: no encontre 'Fecha de cierre' (formato inesperado)")
    f_cierre = dt.datetime.strptime(cierre.group(1), "%d/%m/%Y").date()
    venc = re.search(r"Vencimiento actual\s+(\d{2}/\d{2}/\d{4})", texto)

    lineas = []
    pagos = {"UYU": 0.0, "USD": 0.0}

    def agregar(fecha, desc, pesos, dolares):
        for monto, mon in ((_num_us(pesos), "UYU"), (_num_us(dolares), "USD")):
            if monto > 0.005:          # negativos = pagos/creditos; 0 = la otra columna
                lineas.append({"fecha": fecha, "descripcion": desc,
                               "monto": round(monto, 2), "moneda": mon})

    for ln in texto.splitlines():
        s = ln.strip()
        m = RE_BBVA_LINEA.match(s)
        if m:
            fecha, desc, pesos, dolares = m.groups()
            if "SU PAGO" in desc.upper():
                # pago del saldo anterior: no es consumo, pero hace falta para
                # cuadrar el total declarado (ver nota de abajo)
                pagos["UYU"] += _num_us(pesos)
                pagos["USD"] += _num_us(dolares)
                continue
            agregar(dt.datetime.strptime(fecha, "%d/%m/%Y").date().isoformat(),
                    desc.strip(), pesos, dolares)
            continue
        m2 = RE_BBVA_FEE.match(s)      # cargos sin fecha -> se les pone la de cierre
        if m2:
            agregar(f_cierre.isoformat(), m2.group(1).strip(), m2.group(2), m2.group(3))

    # 'Total a pagar' del BBVA es un SALDO, no el consumo del periodo: arrastra el
    # saldo anterior (Joaco tiene saldo A FAVOR en esta tarjeta desde hace meses,
    # por eso da negativo). La identidad del estado es
    #     total = saldo_anterior + pagos + consumos
    # asi que el consumo del periodo -- que es lo que tienen que sumar las lineas
    # extraidas, y lo que el motor compara -- sale despejando. Sin esto el chequeo
    # de cierre comparaba peras con manzanas y reportaba una diferencia enorme
    # que no era ningun error (calibrado 2026-08-02 con los estados de jul/ago).
    tot = re.search(r"Total a pagar\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})", texto)
    ant = re.search(r"SALDO ANTERIOR\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})", texto)
    saldo_cierre, total = {}, {}
    if tot:
        saldo_cierre = {"UYU": _num_us(tot.group(1)), "USD": _num_us(tot.group(2))}
        anterior = ({"UYU": _num_us(ant.group(1)), "USD": _num_us(ant.group(2))}
                    if ant else {"UYU": 0.0, "USD": 0.0})
        total = {m: round(saldo_cierre[m] - anterior[m] - pagos[m], 2) for m in saldo_cierre}
    return {"periodo": f_cierre.strftime("%Y-%m"),
            "vencimiento": (dt.datetime.strptime(venc.group(1), "%d/%m/%Y").date().isoformat()
                            if venc else None),
            "lineas": lineas,
            "total": total,              # consumo del periodo (lo que suman las lineas)
            "saldo_cierre": saldo_cierre}  # saldo de la tarjeta (para conciliar_saldos)


RE_MC_LINEA = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2,4})\s+(.+?)\s+([\d.]+,\d{2})(-?)$")


def extraer_santander_mc(texto):
    """Parser del estado Santander Mastercard (tarjeta de la casa; PDF con
    contrasena = CI). Calibrado 2026-07-16 con 2 PDFs reales (may/jun 2026).
    Tampoco manda avisos por transaccion -> el PDF es la fuente primaria.

    Formato:
        SALDO ANTERIOR 6.938,82 0,00
        02/04/2026 103 PAGOS 6.938,82-        <- pago (sufijo '-'), NO es consumo
        01/04/2026 103 DLO PEDIDOSYA DESMADRE 208,40
        29/04/2026 103 RIOGAS POCITOS 1.097,00
        03/05/2026 103 SEGURO SALDO DEUDOR 16,90
        SALDO TOTAL 5.651,30 0,00

    Numeros en formato uruguayo. El '103' es un codigo de tarjeta.

    DOS SECCIONES (calibrado 2026-08-02 con el 1er estado de la Visa Platinum):
    el PDF trae primero TODOS los consumos en pesos y despues los de dolares,
    consecutivos y SIN separador ni rotulo; lo unico que se nota es que las
    fechas de la 2da vuelven para atras. Con los PDFs de la Mastercard no se
    veia porque su columna U$S venia siempre en 0.
    El corte se detecta por SUMA: se busca el unico k tal que las primeras k
    lineas suman el total en pesos declarado y las restantes el total en dolares.
    Es autovalidante -- si no hay corte exacto no se adivina: se avisa y todo
    queda en UYU (que es como se comportaba antes de esta calibracion).

    Las lineas con sufijo '-' son descuentos/reintegros (DESC ..., EPAY TAX
    FREE) y SI cuentan, en negativo: sin ellas las sumas no dan los totales
    declarados. Los 'PAGOS' (cancelacion del saldo anterior) se excluyen: no son
    consumo del periodo.
    """
    crudas = []
    for ln in texto.splitlines():
        m = RE_MC_LINEA.match(ln.strip())
        if not m:
            continue
        fecha, _cod, desc, monto, neg = m.groups()
        desc = desc.strip()
        if "PAGOS" in desc.upper():
            continue                   # cancelacion del saldo anterior, no es consumo
        crudas.append({"fecha": dt.datetime.strptime(fecha, "%d/%m/%Y").date().isoformat(),
                       "descripcion": desc,
                       "monto": round(_num_uy(monto) * (-1 if neg else 1), 2),
                       "moneda": "UYU"})

    tot = re.search(r"SALDO (?:TOTAL|CONTADO)\s+([\d.]+,\d{2})\s+([\d.]+,\d{2})", texto)
    total = {"UYU": _num_uy(tot.group(1)), "USD": _num_uy(tot.group(2))} if tot else {}

    # corte pesos/dolares por suma contra los totales declarados
    lineas = crudas
    if total.get("USD"):
        corte = None
        for k in range(len(crudas) + 1):
            uyu = round(sum(l["monto"] for l in crudas[:k]), 2)
            usd = round(sum(l["monto"] for l in crudas[k:]), 2)
            if abs(uyu - total["UYU"]) < 0.01 and abs(usd - total["USD"]) < 0.01:
                corte = k
                break
        if corte is None:
            print(f"  [!] Santander: el estado declara USD {total['USD']} pero no encontre el "
                  "corte pesos/dolares que cuadre con los totales -- las lineas quedan todas "
                  "en UYU. Revisar a mano.")
        else:
            for l in crudas[corte:]:
                l["moneda"] = "USD"
    # El PDF no rotula la fecha de cierre de forma parseable: el periodo sale del
    # ultimo consumo (cierra pocos dias despues). Si no hay consumos (estado
    # vacio), se usa la primera fecha que sigue a SALDO TOTAL (la de cierre).
    periodo = max((l["fecha"] for l in lineas), default="")[:7]
    if not periodo:
        # estado sin consumos: la fecha de cierre es la primera que aparece despues
        # de SALDO TOTAL (no necesariamente en la linea siguiente -- puede haber una
        # tasa en el medio), de ahi el re.S.
        f = re.search(r"SALDO TOTAL.*?(\d{2}/\d{2}/\d{4})", texto, re.S)
        periodo = dt.datetime.strptime(f.group(1), "%d/%m/%Y").strftime("%Y-%m") if f else ""
    if not periodo:
        raise ValueError("extraer_santander_mc: no pude determinar el periodo del estado")
    return {"periodo": periodo, "lineas": lineas, "total": total}


EXTRACTORES = {
    "BBVA": (extraer_bbva, "BBVA Crédito"),
    # cuenta=None -> se resuelve por numero (MC vs Visa comparten layout y remitente)
    "SANTANDER_TARJETA": (extraer_santander_mc, None),
}


def extraer_estado(pdf_path, password):
    """Orquesta (a): desbloquea, identifica emisor por contenido, delega al
    extractor correspondiente. Los extractores son stubs -- esto propaga
    ExtraccionPendienteError hasta que se calibren."""
    texto = desbloquear_pdf(pdf_path, password)
    emisor = detectar_emisor(texto)
    if emisor is None:
        raise ValueError(f"{pdf_path}: no se pudo identificar el emisor "
                          "(se esperaba 'BBVA' o 'MASTERCARD'+'SANTANDER' en el texto)")
    extractor, cuenta = EXTRACTORES[emisor]
    estado = extractor(texto)
    if cuenta is None:                 # Santander: la tarjeta sale del numero
        cuenta = _cuenta_santander(texto)
    estado.setdefault("cuenta", cuenta)
    estado.setdefault("emisor", emisor)
    return estado


# ================================================================== (b) MOTOR DE MATCHING
def _fila_nueva(linea, estado, reglas):
    """Construye la fila de ledger para una linea del estado SIN match
    (origen=estado_cuenta). fuente_id es deterministico (cuenta+fecha+monto+
    moneda+descripcion) para que reprocesar el mismo PDF sea idempotente."""
    fuente_id = "estado:" + hashlib.sha1(
        f"{estado['cuenta']}|{linea['fecha']}|{linea['moneda']}|"
        f"{fmt(linea['monto'])}|{linea['descripcion']}".encode()).hexdigest()[:16]
    categoria, amb_override, _ = categorizar(linea["descripcion"], reglas)
    ambito = amb_override or ambito_de_cuenta(estado["cuenta"])
    r = {c: "" for c in COLUMNS}
    r.update(id=hid("estado_cuenta", fuente_id), fecha=linea["fecha"],
             mes=linea["fecha"][:7], tipo="gasto", monto=fmt(linea["monto"]),
             moneda=linea["moneda"], cuenta=estado["cuenta"], categoria=categoria,
             comercio=linea["descripcion"], descripcion=linea["descripcion"],
             ambito=ambito, origen="estado_cuenta", fuente_id=fuente_id,
             estado="confirmado", en_analisis="1")
    if linea["moneda"] == "UYU":
        r["monto_uyu"] = fmt(linea["monto"])
    elif linea.get("monto_uyu") is not None:
        r["monto_uyu"] = fmt(round(float(linea["monto_uyu"]), 2))
    else:
        # El estado no declara el equivalente en pesos (el Santander no lo trae
        # en la seccion de dolares): se estima con el TC de config/parametros.json,
        # igual que hacen los parsers de mail. Antes quedaba VACIO y un gasto sin
        # monto_uyu no suma en ningun agregado -- lo caza merge_y_validar, que fue
        # justo lo que paso al procesar el 1er estado de la Platinum (2/8/2026).
        with open(PARAMETROS, encoding="utf-8") as f:
            tc = json.load(f)[f"tc_{linea['moneda'].lower()}_uyu"]
        r["monto_uyu"] = fmt(round(linea["monto"] * tc, 2))
    if not categoria:
        r["estado"] = "pendiente"
    return r


def conciliar(estado, ledger_rows, reglas, ambito_de_cuenta_fn=ambito_de_cuenta):
    """Motor de matching PURO. No toca disco.

    Args:
      estado: dict con el contrato de arriba.
      ledger_rows: lista de dicts del ledger (se MUTAN in place las filas que
        matchean: estado, monto_uyu -- pasarle una copia si no se quiere tocar
        el original).
      reglas: salida de cargar_reglas().

    Returns dict:
      matches: [(linea_estado, fila_ledger, score_fuzzy)]
      nuevas: [fila_ledger origen=estado_cuenta] (lineas sin match)
      sin_respaldo: [fila_ledger] (candidatas del periodo sin linea del estado)
      correcciones_tc: [(id_ledger, monto_uyu_nuevo_str, motivo)]
      cierre: {moneda: {"calculado": x, "estado": y, "diferencia": d}}
    """
    cuenta, periodo = estado["cuenta"], estado["periodo"]
    # candidatas: gastos de la cuenta en la VENTANA DEL ESTADO. Ojo: un estado de
    # tarjeta NO cubre un mes calendario, va de cierre a cierre (el de julio de la
    # Platinum arranca el 27/06). Filtrando por mes==periodo, las filas del mes
    # anterior no podian matchear nunca y se insertaban DUPLICADAS -- pasaba con
    # el Uber del 27/6, que ya estaba cargado a mano (detectado 2/8/2026).
    # Las transferencias con cuenta=esta tarjeta (splits/aportes financiados con
    # la tarjeta) no son lineas de compra y quedan fuera del matching por ahora.
    fechas_est = [l["fecha"] for l in estado["lineas"]]
    if fechas_est:
        desde = (dt.date.fromisoformat(min(fechas_est)) - dt.timedelta(days=TOL_DIAS)).isoformat()
        hasta_match = (dt.date.fromisoformat(max(fechas_est)) + dt.timedelta(days=TOL_DIAS)).isoformat()
        hasta_cobertura = max(fechas_est)
    else:
        desde = hasta_match = hasta_cobertura = periodo + "-99"
    candidatas = [r for r in ledger_rows
                  if r["cuenta"] == cuenta and desde <= r["fecha"] <= hasta_match
                  and r["tipo"] == "gasto"]

    usadas_ids = set()
    matches, sin_match_lineas = [], []
    for linea in estado["lineas"]:
        f_estado = dt.date.fromisoformat(linea["fecha"])
        mejor, mejor_score = None, -1.0
        for r in candidatas:
            if id(r) in usadas_ids or r["moneda"] != linea["moneda"]:
                continue
            monto_r = float(r["monto"])
            pct = TOL_MONTO_PCT if linea["moneda"] == "UYU" else TOL_MONTO_PCT_ME
            tol = max(abs(monto_r) * pct, TOL_MONTO_MIN)
            if abs(monto_r - linea["monto"]) > tol:
                continue
            f_r = dt.date.fromisoformat(r["fecha"])
            if abs((f_r - f_estado).days) > TOL_DIAS:
                continue
            score = similares(linea["descripcion"], r["descripcion"] or r["comercio"])
            if score > mejor_score:
                mejor, mejor_score = r, score
        if mejor is not None and mejor_score >= UMBRAL_FUZZY_MINIMO:
            usadas_ids.add(id(mejor))
            matches.append((linea, mejor, mejor_score))
        else:
            sin_match_lineas.append(linea)

    # aplicar conciliacion + correccion de TC real (si el estado lo declara)
    correcciones_tc = []
    for linea, fila, _score in matches:
        fila["estado"] = "conciliado"
        if linea.get("monto_uyu") is not None and fila["moneda"] != "UYU":
            nuevo = fmt(round(float(linea["monto_uyu"]), 2))
            if nuevo != fila["monto_uyu"]:
                correcciones_tc.append((
                    fila["id"], nuevo,
                    f"TC real del estado {cuenta} {periodo}: {fila['moneda']} {fila['monto']} "
                    f"-> {nuevo} UYU (antes {fila['monto_uyu']} estimado, ver tc_estimado)"))
                fila["monto_uyu"] = nuevo

    nuevas = [_fila_nueva(linea, estado, reglas) for linea in sin_match_lineas]
    # sin_respaldo: solo las filas dentro de la COBERTURA del estado. Las
    # posteriores al ultimo consumo listado son compras que van a aparecer en el
    # estado siguiente, no huerfanas -- marcarlas era ruido garantizado todos los
    # meses (la ventana de matching se estira TOL_DIAS mas para poder matchear,
    # pero eso no las hace parte del periodo).
    sin_respaldo = [r for r in candidatas
                    if id(r) not in usadas_ids and r["fecha"] <= hasta_cobertura]

    # cierre al peso: conciliadas (monto original) + insertadas, por moneda,
    # contra el total que declara el estado.
    calculado = defaultdict(float)
    for _linea, fila, _score in matches:
        calculado[fila["moneda"]] += float(fila["monto"])
    for r in nuevas:
        calculado[r["moneda"]] += float(r["monto"])
    cierre = {}
    for m in set(calculado) | set(estado.get("total", {})):
        calc = round(calculado.get(m, 0.0), 2)
        tot = round(float(estado.get("total", {}).get(m, 0.0)), 2)
        cierre[m] = {"calculado": calc, "estado": tot, "diferencia": round(calc - tot, 2)}

    return dict(matches=matches, nuevas=nuevas, sin_respaldo=sin_respaldo,
                correcciones_tc=correcciones_tc, cierre=cierre)


def pendientes_de_resultado(resultado, cuenta, periodo, hoy):
    """Pendientes derivados de un resultado de conciliar(): sin_respaldo_estado
    para las filas del ledger huerfanas, sin_categoria para las insertadas sin regla."""
    pend = []
    for r in resultado["sin_respaldo"]:
        pend.append(dict(
            id_ledger=r["id"], motivo="sin_respaldo_estado",
            detalle=f"{cuenta} {periodo}: fila del ledger ({r['fecha']} {r['moneda']} "
                    f"{r['monto']} {r['descripcion'] or r['comercio']}) sin linea "
                    "correspondiente en el estado de cuenta (posible reversa/duplicado)",
            fecha_alta=hoy, resuelto=""))
    for r in resultado["nuevas"]:
        if not r["categoria"]:
            pend.append(dict(
                id_ledger=r["id"], motivo="sin_categoria",
                detalle=f"estado_cuenta {cuenta} {periodo}: {r['descripcion']!r} sin regla",
                fecha_alta=hoy, resuelto=""))
    return pend


def resoluciones_de_correcciones(correcciones_tc, hoy):
    """Cada correccion de TC real genera 2 filas de resoluciones.csv: el
    override persistente de monto_uyu y el cierre del pendiente tc_estimado
    (mismo mecanismo que usa procesar_mails.py)."""
    out = []
    for id_ledger, nuevo_monto_uyu, motivo in correcciones_tc:
        out.append(dict(id_ledger=id_ledger, campo="monto_uyu", valor=nuevo_monto_uyu,
                        nota=motivo, fecha=hoy))
        out.append(dict(id_ledger=id_ledger, campo="motivo_resuelto", valor="tc_estimado",
                        nota="corregido por conciliacion de estado (Fase 3)", fecha=hoy))
    return out


# ================================================================== orquestacion real (I/O)
def procesar_pdf(pdf_path, ledger, reglas, password, procesados_prev, hoy):
    """Procesa UN PDF real: extrae (a), concilia (b) contra `ledger` (se muta
    in place), devuelve (resultado, log_row) o (None, log_row) si fallo."""
    archivo = os.path.basename(pdf_path)
    try:
        estado = extraer_estado(pdf_path, password)
    except ExtraccionPendienteError as e:
        return None, dict(archivo=archivo, emisor="", cuenta="", periodo="",
                          procesado_en=hoy, matcheadas="", insertadas="",
                          sin_respaldo="", diferencia_cierre=str(e))
    resultado = conciliar(estado, ledger, reglas)
    diff_txt = "; ".join(f"{m}:{c['diferencia']:.2f}" for m, c in resultado["cierre"].items())
    log = dict(archivo=archivo, emisor=estado.get("emisor", ""), cuenta=estado["cuenta"],
              periodo=estado["periodo"], procesado_en=hoy,
              matcheadas=len(resultado["matches"]), insertadas=len(resultado["nuevas"]),
              sin_respaldo=len(resultado["sin_respaldo"]), diferencia_cierre=diff_txt)
    return resultado, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivos", nargs="*", help="PDF puntuales; sin args = todos los nuevos de data/estados/")
    args = ap.parse_args()

    with open(SECRETS, encoding="utf-8") as f:
        password = json.load(f)["ci_pdf_password"]

    procesados = leer_csv(ESTADOS_PROCESADOS, PROC_COLS)
    ya_hechos = {p["archivo"] for p in procesados}

    if args.archivos:
        pdfs = args.archivos
    else:
        pdfs = [p for p in sorted(glob.glob(os.path.join(ESTADOS_DIR, "*.pdf")))
                if os.path.basename(p) not in ya_hechos]

    if not pdfs:
        print("sin PDFs nuevos para procesar en data/estados/")
        return

    ledger = leer_csv(LEDGER, COLUMNS)
    reglas = cargar_reglas()
    pendientes = leer_csv(PENDIENTES, PEND_COLS)
    resoluciones = leer_csv(RESOLUCIONES, RES_COLS)
    hoy = dt.date.today().isoformat()

    pend_nuevos_total, res_nuevas_total, logs_nuevos = [], [], []
    huboEscritura = False

    for pdf_path in pdfs:
        resultado, log = procesar_pdf(pdf_path, ledger, reglas, password, procesados, hoy)
        logs_nuevos.append(log)
        if resultado is None:
            print(f"{log['archivo']}: {log['diferencia_cierre']}")
            continue
        huboEscritura = True
        ledger.extend(resultado["nuevas"])
        pend_nuevos_total.extend(pendientes_de_resultado(
            resultado, log["cuenta"], log["periodo"], hoy))
        res_nuevas_total.extend(resoluciones_de_correcciones(resultado["correcciones_tc"], hoy))
        print(f"{log['archivo']} ({log['emisor']} / {log['cuenta']} / {log['periodo']}): "
              f"{log['matcheadas']} matcheadas, {log['insertadas']} insertadas, "
              f"{log['sin_respaldo']} sin_respaldo, cierre {log['diferencia_cierre']}")

    if huboEscritura:
        ledger.sort(key=lambda r: (r["fecha"], r["id"]))
        escribir_csv(LEDGER, COLUMNS, ledger)
        if pend_nuevos_total:
            escribir_csv(PENDIENTES, PEND_COLS, pendientes + pend_nuevos_total)
        if res_nuevas_total:
            escribir_csv(RESOLUCIONES, RES_COLS, resoluciones + res_nuevas_total)
    escribir_csv(ESTADOS_PROCESADOS, PROC_COLS, procesados + logs_nuevos)

    print(f"\nPDFs procesados en esta corrida: {len(pdfs)}; "
          f"pendientes nuevos: {len(pend_nuevos_total)}; "
          f"correcciones de TC: {len(res_nuevas_total)//2 if res_nuevas_total else 0}")


if __name__ == "__main__":
    main()
