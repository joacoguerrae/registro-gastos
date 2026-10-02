# -*- coding: utf-8 -*-
"""
Fuentes reproducibles del ledger (auditoria 2026-09, Fase 1.1).

Hasta la Fase 1, las filas `manual`, `estado_cuenta` y `splitwise` existian
SOLO adentro de data/ledger.csv: merge() las conservaba releyendo su propia
salida. Si el ledger se perdia, se perdian ellas (SA1). Ahora cada una tiene su
archivo en data/fuentes/, y data/ledger.csv pasa a ser un artefacto de build:

    staging (1money, planilla_casa, carga_manual)   <- migradores / planilla
  + data/fuentes/{manual,estado_cuenta,splitwise}.csv
  + filas mail                                        <- data/inbox_raw/
  + data/resoluciones.csv                             (overrides)
  - data/fuentes/borrados.csv                         (bajas registradas)
  = data/ledger.csv

Quien escribe las fuentes:
  - scripts/gastos.py escribe data/fuentes/manual.csv directo (la unica puerta
    de escritura manual).
  - conciliar_estado / conciliar_debito / sync_splitwise siguen agregando al
    ledger como antes, y merge() ADOPTA toda fila nueva de esos origenes en su
    fuente. Mismo resultado sin tocar cuatro escritores, y un escritor futuro
    que se olvide de la fuente no pierde datos: sus filas se adoptan solas.

Reglas de merge() (ver sincronizar_fuentes):
  - Contenido: MANDA LA FUENTE. Una fila de un origen con fuente que aparece
    distinta en el ledger se restaura desde la fuente. Las correcciones van por
    data/resoluciones.csv, como siempre.
  - `estado` (pendiente/confirmado/conciliado): MANDA EL LEDGER. Es estado
    derivado que escriben la bandeja y los conciliadores; se copia a la fuente
    para que una reconstruccion desde cero lo conserve.
  - Borrar: SOLO con `gastos.py borrar`, que saca la fila de la fuente y la
    registra en borrados.csv. Borrarla del ledger a mano no sirve: la fuente la
    restaura en la corrida siguiente (a proposito: asi es como se cura un
    ledger truncado).
"""
import csv
import datetime as dt
import json
import os

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

COLUMNS = ["id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
           "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
           "descripcion", "ambito", "origen", "estado", "en_analisis",
           "fuente_id"]

DIR = P("data", "fuentes")
ORIGENES_CON_FUENTE = ("manual", "estado_cuenta", "splitwise")
BORRADOS = os.path.join(DIR, "borrados.csv")
# `fila` guarda la fila completa (JSON) al momento de borrarla: un borrado se
# puede deshacer copiandola de vuelta a su fuente.
BORRADOS_COLS = ["id", "origen", "fecha_fila", "monto", "moneda", "descripcion",
                 "motivo", "fecha_borrado", "fila"]


# Marca estado=conciliado de las filas SIN fuente propia (las mail): la escriben
# los conciliadores en el ledger y `procesar_mails --reprocesar` no la puede
# reproducir. Guardarla aca es lo que hace que una reconstruccion desde cero
# devuelva el ledger IDENTICO, no "identico salvo 9 filas".
CONCILIADOS = os.path.join(DIR, "conciliados.csv")


def aplicar_conciliados(rows):
    """Une las marcas guardadas con las del ledger y las re-aplica a las filas
    sin fuente. Nunca des-concilia. Devuelve cuantas restauro."""
    guardados = {r["id"] for r in _leer(CONCILIADOS, ["id"])}
    sin_fuente = [r for r in rows if r["origen"] not in ORIGENES_CON_FUENTE]
    actuales = {r["id"] for r in sin_fuente if r["estado"] == "conciliado"}
    restauradas = 0
    for r in sin_fuente:
        if r["id"] in guardados and r["estado"] != "conciliado":
            r["estado"] = "conciliado"
            restauradas += 1
    # SIN podar ids ausentes: en la 1ra pasada de reconstruir_ledger.py todavia
    # no hay filas mail, y podar ahi borraba todas las marcas (medido).
    nuevo = sorted(guardados | actuales)
    if nuevo != sorted(guardados) or not os.path.exists(CONCILIADOS):
        _escribir(CONCILIADOS, ["id"], [{"id": i} for i in nuevo])
    return restauradas


def ruta(origen):
    assert origen in ORIGENES_CON_FUENTE, origen
    return os.path.join(DIR, f"{origen}.csv")


def _leer(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        assert rd.fieldnames == cols, f"columnas de {path} != esperadas: {rd.fieldnames}"
        return list(rd)


def _escribir(path, cols, rows):
    # lineterminator="\n": ver auditoria 2026-09 SB1.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def leer(origen):
    return _leer(ruta(origen), COLUMNS)


def escribir(origen, rows):
    _escribir(ruta(origen), COLUMNS, sorted(rows, key=lambda r: (r["fecha"], r["id"])))


def leer_borrados():
    """-> {id: fila de borrados.csv}"""
    return {r["id"]: r for r in _leer(BORRADOS, BORRADOS_COLS)}


def registrar_borrado(fila, motivo):
    borr = list(_leer(BORRADOS, BORRADOS_COLS))
    if any(b["id"] == fila["id"] for b in borr):
        return False
    borr.append(dict(id=fila["id"], origen=fila["origen"], fecha_fila=fila["fecha"],
                     monto=fila["monto"], moneda=fila["moneda"],
                     descripcion=(fila["descripcion"] or fila["comercio"])[:90],
                     motivo=motivo, fecha_borrado=dt.date.today().isoformat(),
                     fila=json.dumps({c: fila.get(c, "") for c in COLUMNS}, ensure_ascii=False)))
    _escribir(BORRADOS, BORRADOS_COLS, borr)
    return True


def borrados_por_origen(borrados):
    out = {}
    for b in borrados.values():
        out[b["origen"]] = out.get(b["origen"], 0) + 1
    return out


def sincronizar_fuentes(filas_ledger, borrados):
    """Corazon de la Fase 1.1: reconcilia el ledger previo con las fuentes.

    filas_ledger: filas del data/ledger.csv anterior de los origenes con fuente.
    Devuelve (filas_para_el_ledger, informe). Escribe las fuentes si cambiaron.
      informe = {origen: {"adoptadas": [...ids], "restauradas": [...ids],
                          "estado": n, "total": n}}
    """
    por_origen_ledger = {o: {} for o in ORIGENES_CON_FUENTE}
    # Un id repetido NO se colapsa: se deja pasar entero al ledger para que el
    # chequeo de ids duplicados de merge_y_validar salga FALLO. Colapsarlo era
    # perder una fila en silencio -- paso el 2/10/2026 con dos extornos de EONE
    # identicos (mismo dia, monto y concepto) que conciliar_debito inserto con
    # el mismo id.
    repetidas = []
    for r in filas_ledger:
        if r["id"] in borrados:
            continue
        if r["id"] in por_origen_ledger[r["origen"]]:
            repetidas.append(dict(r))
            continue
        por_origen_ledger[r["origen"]][r["id"]] = r

    salida, informe = [], {}
    for o in ORIGENES_CON_FUENTE:
        fuente = leer(o)
        vistos = set()
        cambio = False
        adoptadas, restauradas, n_estado = [], [], 0
        nueva_fuente = []
        for r in fuente:
            if r["id"] in borrados:          # borrado registrado: se va de la fuente
                cambio = True
                continue
            vistos.add(r["id"])
            en_ledger = por_origen_ledger[o].get(r["id"])
            if en_ledger is None:
                restauradas.append(r["id"])
            elif en_ledger["estado"] != r["estado"]:
                r = dict(r, estado=en_ledger["estado"])
                n_estado += 1
                cambio = True
            nueva_fuente.append(r)
        for i, r in por_origen_ledger[o].items():
            if i not in vistos:
                nueva_fuente.append(dict(r))
                adoptadas.append(i)
                cambio = True
        if cambio or not os.path.exists(ruta(o)):
            escribir(o, nueva_fuente)
        salida.extend(dict(r) for r in nueva_fuente)
        informe[o] = dict(adoptadas=adoptadas, restauradas=restauradas,
                          estado=n_estado, total=len(nueva_fuente))
    salida.extend(repetidas)
    return salida, informe
