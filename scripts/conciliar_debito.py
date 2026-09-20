# -*- coding: utf-8 -*-
"""Concilia extractos de cuentas de DEBITO (export CSV del web de Santander)
contra el ledger.

POR QUE EXISTE: los ingresos (sueldo, transferencias recibidas) y algunos debitos
(comisiones, debitos automaticos) NO generan aviso por mail -> el pipeline esta
ciego a ellos. El extracto mensual es la unica fuente. Ademas da el saldo real
de cierre para la conciliacion de saldos.

QUE HACE, por cada CSV de data/estados/ que no este en el log:
  1. Identifica la cuenta por el numero del encabezado (config/cuentas_bancarias.csv).
  2. Matchea cada movimiento contra el ledger (mismas tolerancias que
     conciliar_estado: monto +-0.5%, fecha +-3 dias). A diferencia del motor de
     tarjetas, ACA las candidatas incluyen gastos, ingresos Y transferencias:
     en una cuenta de debito los movimientos son de los tres tipos.
  3. Reporta 4 baldes:
       - conciliados
       - solo en el extracto / DEBITOS  -> gastos sin aviso por mail
       - solo en el extracto / CREDITOS -> INGRESOS a clasificar
       - solo en el ledger              -> sin respaldo en el extracto (sospechoso)
  4. Reporta el saldo de cierre del extracto (insumo de conciliar_saldos.py).

Por defecto SOLO REPORTA (dry-run). Con --insertar escribe las filas nuevas al
ledger (origen=estado_cuenta) y sus pendientes:
  - debitos sin regla de categoria -> pendiente `sin_categoria`
  - creditos                       -> pendiente `ingreso_a_clasificar`
    (Joaco define Sueldo / Otros ingresos / Devoluciones & cashback, o si es
     una transferencia entre cuentas propias -> interno)

Ejecutar:
  python scripts/conciliar_debito.py                 # todos los CSV nuevos, dry-run
  python scripts/conciliar_debito.py archivo.csv     # uno puntual
  python scripts/conciliar_debito.py --insertar      # aplica los cambios
"""
import argparse
import csv
import datetime as dt
import json
import hashlib
import io
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conciliar_estado import (TOL_MONTO_PCT, TOL_MONTO_MIN, TOL_DIAS, similares,
                              cargar_reglas, categorizar, ambito_de_cuenta, COLUMNS)

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(*a): return os.path.join(BASE, *a)

ESTADOS = P("data", "estados")
LEDGER = P("data", "ledger.csv")
PENDIENTES = P("data", "pendientes.csv")
CUENTAS_BANCARIAS = P("config", "cuentas_bancarias.csv")
LOG = P("data", "staging", "debitos_procesados.csv")
LOG_COLS = ["archivo", "cuenta", "moneda", "periodo", "saldo_cierre", "procesado_en", "detalle"]
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]


def leer_csv(path, cols=None):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def escribir_csv(path, cols, rows):
    # lineterminator="\n": csv.writer escribe CRLF en TODAS las plataformas y
    # .gitattributes fija *.csv eol=lf -> git avisa en cada add. Ver auditoria
    # 2026-09 SB1.
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=cols)
        w.writeheader()
        w.writerows(rows)



def _tc(moneda):
    """Tipo de cambio estimado de config/parametros.json (mismo que usan los
    parsers de mail). Solo para monedas != UYU."""
    with io.open(P("config", "parametros.json"), encoding="utf-8") as f:
        return json.load(f)[f"tc_{moneda.lower()}_uyu"]

def fmt(x):
    return f"{x:.2f}"


# ----------------------------------------------------------- parser del export
def parsear_santander(path):
    """Export CSV del web de Santander -> dict con encabezado + movimientos.
    Encabezado: Cliente / Cuenta / Numero / Moneda / Sucursal, luego 'Movimientos',
    'Desde:,dd/mm/aaaa,Hasta:,dd/mm/aaaa' y la tabla
    Fecha,Referencia,Concepto,Descripcion,Debito,Credito,Saldos."""
    raw = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    head, movs, en_tabla = {}, [], False
    desde = hasta = ""
    for ln in raw:
        p = next(csv.reader([ln]), [])
        if not p:
            continue
        if p[0] == "Fecha" and len(p) > 4:
            en_tabla = True
            continue
        if not en_tabla:
            k = p[0].strip().lower()
            if k in ("cliente", "cuenta", "moneda", "sucursal") and len(p) > 1:
                head[k] = p[1].strip()
            elif k.startswith("n") and "mero" in k and len(p) > 1:      # "Numero"/"Número"
                head["numero"] = p[1].strip()
            elif k == "desde:" and len(p) >= 4:
                desde, hasta = p[1].strip(), p[3].strip()
            continue
        if len(p) < 7 or not p[0].strip():
            continue
        deb, cred, saldo = p[4].strip(), p[5].strip(), p[6].strip()
        monto = float(deb) if deb else (float(cred) if cred else None)
        if monto is None:
            continue
        f = dt.datetime.strptime(p[0].strip(), "%d/%m/%Y").date()
        movs.append({"fecha": f.isoformat(), "concepto": p[2].strip(),
                     "monto": round(monto, 2), "saldo": saldo})
    return {"numero": head.get("numero", ""), "moneda": head.get("moneda", "UYU"),
            "cuenta_desc": head.get("cuenta", ""), "desde": desde, "hasta": hasta,
            "movimientos": movs}


def mapear_cuenta(numero, moneda):
    for r in leer_csv(CUENTAS_BANCARIAS):
        if r["numero"].strip() == numero and r["moneda"].strip() == moneda:
            return r["cuenta_ledger"].strip()
    return None


# ------------------------------------------------------------------ matching
def conciliar_cuenta(ext, cuenta, ledger):
    """Matchea los movimientos del extracto contra el ledger.
    Candidatas: filas de esa cuenta (gasto/ingreso/transferencia) en el rango."""
    mon = ext["moneda"]
    fechas = [m["fecha"] for m in ext["movimientos"]]
    f_min, f_max = min(fechas), max(fechas)

    def signo(r):
        """monto con signo desde la optica de la cuenta: negativo = sale."""
        m = float(r["monto"] or 0)
        if r["tipo"] == "gasto":
            return -m
        if r["tipo"] == "ingreso":
            return +m
        if r["tipo"] == "transferencia":
            if r["cuenta"] == cuenta:
                return -m
            if r["cuenta_destino"] == cuenta:
                return +m
        return 0.0

    cand = []
    for r in ledger:
        if r["moneda"] != mon:
            continue
        if not (f_min <= r["fecha"] <= f_max):
            continue
        if r["cuenta"] != cuenta and r["cuenta_destino"] != cuenta:
            continue
        # OJO: en_analisis=0 SI entra al matching. No cuenta en agregados, pero el
        # movimiento existe en el banco (transferencias internas, aportes). Excluirlo
        # rompe matches validos. Solo se lo excluye del reporte "sin respaldo".
        s = signo(r)
        if s:
            cand.append((r, s))

    usados, matches, solo_ext = set(), [], []
    for mv in ext["movimientos"]:
        f_mv = dt.date.fromisoformat(mv["fecha"])
        mejor, mejor_score = None, -1.0
        for (r, s) in cand:
            if id(r) in usados:
                continue
            if (s < 0) != (mv["monto"] < 0):          # mismo sentido
                continue
            tol = max(abs(s) * TOL_MONTO_PCT, TOL_MONTO_MIN)
            if abs(abs(s) - abs(mv["monto"])) > tol:
                continue
            if abs((dt.date.fromisoformat(r["fecha"]) - f_mv).days) > TOL_DIAS:
                continue
            score = similares(mv["concepto"], r["descripcion"] or r["comercio"])
            if score > mejor_score:
                mejor, mejor_score = r, score
        if mejor is not None:
            usados.add(id(mejor))
            matches.append((mv, mejor, mejor_score))
        else:
            solo_ext.append(mv)
    # "sin respaldo" solo para filas con antiguedad suficiente: una compra de las
    # ultimas 24-72h puede estar en el ledger (aviso por mail, fecha de transaccion)
    # y todavia no haber posteado en el banco. Sin este margen se marcarian como
    # sospechosas cosas que solo estan en transito.
    # OJO: NO filtrar por en_analisis aca. Una transferencia interna con
    # en_analisis=0 que el banco no respalda es un FANTASMA (fila que dice que
    # hubo un movimiento que nunca existio) y es justo lo que hay que ver.
    # Lo ya resuelto se silencia con resoluciones.csv (motivo_resuelto), como
    # en el resto del sistema.
    limite = (dt.date.fromisoformat(f_max) - dt.timedelta(days=TOL_DIAS)).isoformat()
    solo_ledger = [r for (r, _s) in cand if id(r) not in usados and r["fecha"] <= limite]
    return matches, solo_ext, solo_ledger


def fila_nueva(mv, cuenta, moneda, reglas):
    seed = f"estado:{cuenta}:{mv['fecha']}:{fmt(mv['monto'])}:{mv['concepto'][:40]}"
    rid = hashlib.sha1(seed.encode()).hexdigest()[:12]
    entra = mv["monto"] > 0
    r = {c: "" for c in COLUMNS}
    cat, amb_ov, _ = ("", "", None) if entra else categorizar(mv["concepto"], reglas)
    r.update(id=rid, fecha=mv["fecha"], mes=mv["fecha"][:7],
             tipo="ingreso" if entra else "gasto",
             monto=fmt(abs(mv["monto"])), moneda=moneda,
             # Extracto en moneda extranjera (caja de ahorro USD): se estima el
             # equivalente en pesos con el tc de config/parametros.json. Antes
             # quedaba VACIO, y una fila de gasto/ingreso sin monto_uyu no suma en
             # ningun agregado -- lo caza merge_y_validar (paso al conciliar el
             # extracto USD de julio, 2/8/2026).
             monto_uyu=fmt(abs(mv["monto"]) * (1 if moneda == "UYU" else _tc(moneda))),
             cuenta=cuenta, categoria=cat, comercio=mv["concepto"][:60],
             descripcion=mv["concepto"][:90],
             ambito=amb_ov or ambito_de_cuenta(cuenta),
             origen="estado_cuenta", estado="pendiente", en_analisis="1",
             fuente_id=seed)
    return r


# ---------------------------------------------------------------------- main
def procesar(path, ledger, reglas, insertar, hoy):
    # La carpeta data/estados/ tambien junta CSV que NO son extractos de debito
    # (exports de tarjeta de credito, movimientos post-cierre). Sin este guard,
    # uno solo de esos abortaba la corrida entera y con ella el paso 4 del
    # cierre: paso el 2/9/2026 con un export de la Mastercard Hipermas, cuyos
    # importes vienen con coma decimal. Se saltea RUIDOSAMENTE, nunca en
    # silencio: si el archivo era un extracto de verdad hay que enterarse.
    try:
        ext = parsear_santander(path)
    except Exception as e:
        print(f"  [!] {os.path.basename(path)}: no parece un extracto de debito "
              f"del Santander, lo salteo ({type(e).__name__}: {e}). "
              f"Si SI lo es, el parser necesita ajuste -- no lo ignores.")
        return None
    if not ext["movimientos"]:
        print(f"  {os.path.basename(path)}: sin movimientos parseables -- lo salteo")
        return None
    cuenta = mapear_cuenta(ext["numero"], ext["moneda"])
    nombre = os.path.basename(path)
    if not cuenta:
        print(f"  [!] {nombre}: cuenta DESCONOCIDA (numero {ext['numero']}, {ext['moneda']}, "
              f"'{ext['cuenta_desc']}').\n      Agregala a config/cuentas_bancarias.csv y volve a correr.")
        return None

    matches, solo_ext, solo_led = conciliar_cuenta(ext, cuenta, ledger)
    saldo_cierre = ext["movimientos"][0]["saldo"]   # el export viene con el mas reciente arriba
    periodo = ext["movimientos"][0]["fecha"][:7]
    print(f"\n=== {nombre} -> {cuenta} ({ext['moneda']}) | {ext['desde']} a {ext['hasta']} ===")
    print(f"  movimientos del extracto: {len(ext['movimientos'])} | saldo de cierre: {saldo_cierre}")
    print(f"  conciliados con el ledger: {len(matches)}")

    deb = [m for m in solo_ext if m["monto"] < 0]
    cred = [m for m in solo_ext if m["monto"] > 0]
    print(f"  --- solo en el extracto: {len(solo_ext)} ({len(deb)} debitos, {len(cred)} creditos) ---")
    for m in sorted(solo_ext, key=lambda x: x["fecha"]):
        tag = "INGRESO" if m["monto"] > 0 else "gasto  "
        print(f"    {m['fecha']} | {tag} | {m['monto']:>11.2f} | {m['concepto'][:58]}")
    print(f"  --- solo en el ledger (sin respaldo del extracto): {len(solo_led)} ---")
    for r in sorted(solo_led, key=lambda x: x["fecha"]):
        print(f"    {r['fecha']} | {r['tipo']:13} | {r['monto']:>10} | {(r['descripcion'] or r['comercio'])[:50]} | {r['id']}")

    nuevas = [fila_nueva(m, cuenta, ext["moneda"], reglas) for m in solo_ext]
    return {"ext": ext, "cuenta": cuenta, "periodo": periodo, "saldo_cierre": saldo_cierre,
            "nuevas": nuevas, "solo_led": solo_led, "archivo": nombre,
            "detalle": f"{len(matches)} conciliados, {len(deb)} debitos y {len(cred)} creditos nuevos"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivos", nargs="*", help="CSV puntuales (default: todos los nuevos de data/estados/)")
    ap.add_argument("--insertar", action="store_true", help="escribe las filas nuevas al ledger")
    args = ap.parse_args()

    hoy = dt.date.today().isoformat()
    ledger = leer_csv(LEDGER)
    reglas = cargar_reglas()
    procesados = {r["archivo"] for r in leer_csv(LOG)}

    if args.archivos:
        paths = [a if os.path.isabs(a) else P("data", "estados", a) for a in args.archivos]
    else:
        paths = [os.path.join(ESTADOS, f) for f in sorted(os.listdir(ESTADOS))
                 if f.lower().endswith(".csv") and f not in procesados]
    if not paths:
        print("no hay CSV de debito nuevos en data/estados/")
        return 0

    resultados = [r for r in (procesar(p, ledger, reglas, args.insertar, hoy) for p in paths) if r]
    if not resultados:
        return 0

    total_nuevas = sum(len(r["nuevas"]) for r in resultados)
    print(f"\n=== RESUMEN: {total_nuevas} filas nuevas propuestas ===")
    for r in resultados:
        print(f"  {r['cuenta']}: {r['detalle']} | saldo de cierre {r['saldo_cierre']}")

    if not args.insertar:
        print("\n(dry-run: no se escribio nada. Corre con --insertar para aplicar.)")
        return 0

    pend = leer_csv(PENDIENTES)
    # (id, motivo) ya resueltos -> no se vuelven a levantar (mismo mecanismo que
    # procesar_mails / sync_splitwise)
    resueltos = {(r["id_ledger"], r["valor"]) for r in leer_csv(P("data", "resoluciones.csv"))
                 if r.get("campo") == "motivo_resuelto"}
    nuevas_todas, pend_nuevos = [], []
    for r in resultados:
        for f in r["nuevas"]:
            nuevas_todas.append(f)
            if f["tipo"] == "ingreso":
                pend_nuevos.append(dict(id_ledger=f["id"], motivo="ingreso_a_clasificar",
                                        detalle=f"{r['cuenta']} {f['fecha']} {f['monto']} {f['moneda']}: "
                                                f"{f['descripcion'][:60]} -- Sueldo / Otros ingresos / "
                                                f"Devoluciones & cashback / transferencia interna?",
                                        fecha_alta=dt.date.today().isoformat(), resuelto=""))
            elif not f["categoria"]:
                pend_nuevos.append(dict(id_ledger=f["id"], motivo="sin_categoria",
                                        detalle=f"{r['cuenta']} {f['descripcion'][:60]} (del extracto, sin aviso por mail)",
                                        fecha_alta=dt.date.today().isoformat(), resuelto=""))
        for r2 in r["solo_led"]:
            if (r2["id"], "sin_respaldo_estado") in resueltos:      # ya lo decidiste antes
                continue
            pend_nuevos.append(dict(id_ledger=r2["id"], motivo="sin_respaldo_estado",
                                    detalle=f"fila del ledger sin linea en el extracto de {r['cuenta']} {r['periodo']}",
                                    fecha_alta=dt.date.today().isoformat(), resuelto=""))

    escribir_csv(LEDGER, COLUMNS, sorted(ledger + nuevas_todas, key=lambda x: (x["fecha"], x["id"])))
    if pend_nuevos:
        escribir_csv(PENDIENTES, PEND_COLS, pend + pend_nuevos)
    log = leer_csv(LOG)
    for r in resultados:
        log.append(dict(archivo=r["archivo"], cuenta=r["cuenta"], moneda=r["ext"]["moneda"],
                        periodo=r["periodo"], saldo_cierre=r["saldo_cierre"],
                        procesado_en=dt.datetime.now().isoformat(timespec="seconds"),
                        detalle=r["detalle"]))
    escribir_csv(LOG, LOG_COLS, log)
    print(f"\nOK: {len(nuevas_todas)} filas insertadas (origen=estado_cuenta), "
          f"{len(pend_nuevos)} pendientes nuevos. Corre merge_y_validar.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
