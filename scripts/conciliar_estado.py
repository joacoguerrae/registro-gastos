# -*- coding: utf-8 -*-
"""
Fase 3 — Motor de conciliacion de estados de cuenta (docs/fase3.md seccion 2).

Dos capas separadas a proposito:

  (a) EXTRACTORES DE PDF por emisor (BBVA Visa, Santander Mastercard). Hoy son
      stubs bien definidos: identifican el emisor por contenido y fallan con
      ExtraccionPendienteError ("primer PDF real pendiente") hasta calibrarlos
      con el primer archivo real que Joaco suelte en data/estados/. Tambien
      vive aca el desbloqueo de PDF con contrasena (CI, en secrets).

  (b) MOTOR DE MATCHING (funcion `conciliar`): NO depende de PDF. Recibe un
      dict "estado" ya extraido (o generado a mano/sintetico) + una lista de
      filas del ledger, y devuelve conciliaciones/inserciones/pendientes/
      cierre. Es la parte que se testea HOY, sin esperar un PDF real, en
      scripts/test_conciliacion.py.

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

BASE = r"C:\Users\joaco\Desktop\proyectos claude\gastos"
P = lambda *a: os.path.join(BASE, *a)

LEDGER = P("data", "ledger.csv")
PENDIENTES = P("data", "pendientes.csv")
RESOLUCIONES = P("data", "resoluciones.csv")
ESTADOS_DIR = P("data", "estados")
ESTADOS_PROCESADOS = P("data", "staging", "estados_procesados.csv")
CUENTAS_CSV = P("config", "cuentas.csv")
REGLAS_CSV = P("config", "reglas_comercio.csv")
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
    if "MASTERCARD" in t and "SANTANDER" in t:
        return "SANTANDER_MC"
    return None


def extraer_bbva(texto):
    """Parser del estado de cuenta BBVA Visa. STUB: BBVA no tiene avisos por
    transaccion (config/cuentas.csv), por lo que ESTE PDF es la fuente
    primaria de todos sus gastos -- calibrar apenas Joaco suelte el primero
    en data/estados/ (secciones del PDF, columnas fecha/descripcion/monto,
    formato de moneda, total y vencimiento)."""
    raise ExtraccionPendienteError(
        "extraer_bbva: primer PDF real de BBVA Visa pendiente para calibrar "
        "el formato (fecha/descripcion/monto/total/vencimiento). Soltar un "
        "estado real en data/estados/ y completar esta funcion.")


def extraer_santander_mc(texto):
    """Parser del estado de cuenta Santander Mastercard (tarjeta de la casa,
    protegido con contrasena = CI). STUB: sin avisos por transaccion
    tampoco -- calibrar con el primer PDF real."""
    raise ExtraccionPendienteError(
        "extraer_santander_mc: primer PDF real de Santander Mastercard "
        "pendiente para calibrar el formato (fecha/descripcion/monto/total/"
        "vencimiento). Soltar un estado real en data/estados/ y completar "
        "esta funcion.")


EXTRACTORES = {
    "BBVA": (extraer_bbva, "BBVA Crédito"),
    "SANTANDER_MC": (extraer_santander_mc, "Santander Mastercard"),
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
    estado = extractor(texto)          # nunca retorna hoy (stub) -- deja subir el error
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
    # si no hay UYU ni monto_uyu declarado: queda vacio (no hay TC conocido).
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
    # candidatas: gastos de la cuenta en el periodo. Las transferencias con
    # cuenta=esta tarjeta (splits/aportes financiados con la tarjeta) no son
    # lineas de compra propiamente dichas y quedan fuera del matching por ahora.
    candidatas = [r for r in ledger_rows
                  if r["cuenta"] == cuenta and r["mes"] == periodo and r["tipo"] == "gasto"]

    usadas_ids = set()
    matches, sin_match_lineas = [], []
    for linea in estado["lineas"]:
        f_estado = dt.date.fromisoformat(linea["fecha"])
        mejor, mejor_score = None, -1.0
        for r in candidatas:
            if id(r) in usadas_ids or r["moneda"] != linea["moneda"]:
                continue
            monto_r = float(r["monto"])
            tol = max(monto_r * TOL_MONTO_PCT, TOL_MONTO_MIN)
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
    sin_respaldo = [r for r in candidatas if id(r) not in usadas_ids]

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
