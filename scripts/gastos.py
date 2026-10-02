# -*- coding: utf-8 -*-
"""
gastos.py -- la UNICA puerta de escritura manual del sistema (auditoria
2026-09, Fase 1.2).

Antes de esto, cada alta a mano (hermana de un split, gasto que pago Lu, aporte
entre cuentas propias, cobro de `A cobrar`, borrado de un duplicado) se hacia
con Python descartable pegado en la terminal: ~15 veces entre agosto y
setiembre, con errores reales (gasto sin monto_uyu, heredoc truncado, borrado
por linea fisica). Cada subcomando de aca valida contra los catalogos y aplica
en codigo los invariantes que antes eran prosa.

TODOS los subcomandos son en seco por defecto: muestran lo que harian. Con
--aplicar escriben, y despues corren procesar_mails.py (aplica resoluciones y
cierra pendientes) + merge_y_validar.py (invariantes) -- y cierre_casa.py si
tocaron la casa -- salvo --sin-pipeline.

  split <id> --mia X [--a DESTINO[=X] ...] [--participacion-lu F]
      Divide un gasto: la fila original queda con tu parte (via resoluciones) y
      el resto va en filas hermanas `transferencia` a DESTINO (default Deudas).
      X = importe (1433.33), fraccion (1/3) o porcentaje (50%). Un destino sin
      valor se lleva el resto. Verifica original + hermanas = cargo real.
      Avisa si el cargo salio de una cuenta de la casa (genera retiro).
  gasto --fecha F --monto M --cuenta C --categoria K --ambito casa|personal --desc D
  pago-lu --fecha F --monto M --categoria K --desc D [--efectivo]
      Gasto de casa que pago Lu con su tarjeta (o en efectivo). cierre_casa lo
      cuenta solo como aporte de ella: no hace falta tocar aportes_lu.csv ni
      cierre_exclusiones.csv (Fase 1.3).
  aporte --quien lu|joaco --monto M (--fecha F | --mes AAAA-MM) [--desde CUENTA]
      Lu -> data/aportes_lu.csv. Joaco -> transferencia CUENTA -> Cuenta Casa.
      Rechaza un aporte repetido (mismo mes y monto) sin --forzar (SM12).
  transferencia --de A --a B --monto M --fecha F --desc D
  cobro --monto M --fecha F [--a CUENTA] --desc D
      Te pagan algo de `A cobrar` (lo que te deben por fuera de Splitwise).
  resolver <id> campo=valor ... [--cerrar MOTIVO ...] [--participacion-lu F] --nota N
      Corrige una fila (cualquier origen) via data/resoluciones.csv.
  borrar <id> --motivo M
      Da de baja una fila con registro en data/fuentes/borrados.csv. No sirve
      para 1money/planilla_casa (usar `resolver <id> en_analisis=0`) ni para la
      planilla "Carga manual" (borrar la fila en la planilla).

Ejemplos:
  python scripts/gastos.py split 4fe0b33136a4 --mia 2/6 --a Deudas=3/6 --a "A cobrar" --nota "YouTube"
  python scripts/gastos.py pago-lu --fecha 2026-08-31 --monto 602 --categoria Auto --desc "Llavero del auto"
  python scripts/gastos.py aporte --quien lu --monto 31000 --mes 2026-09 --aplicar
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

import fuentes

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)
SCRIPTS = os.path.dirname(os.path.abspath(__file__))

COLUMNS = fuentes.COLUMNS
LEDGER = P("data", "ledger.csv")
RESOLUCIONES = P("data", "resoluciones.csv")
RES_COLS = ["id_ledger", "campo", "valor", "nota", "fecha"]
PENDIENTES = P("data", "pendientes.csv")
PEND_COLS = ["id_ledger", "motivo", "detalle", "fecha_alta", "resuelto"]
APORTES_LU = P("data", "aportes_lu.csv")
APORTES_COLS = ["mes", "fecha", "monto", "nota"]
PARTICIPACIONES = P("data", "participaciones.csv")
PART_COLS = ["id_ledger", "frac_lu", "frac_joaco", "nota"]
CUENTAS_CSV = P("config", "cuentas.csv")
CATEG_CSV = P("config", "categorias.csv")
PARAMETROS = P("config", "parametros.json")

# Espejo de cierre_casa.CUENTAS_CASA: plata que YA es de la casa.
CUENTAS_CASA = {"Cuenta Casa", "Santander Mastercard", "Mercado Pago"}
MONEDAS = ("UYU", "USD", "EUR")
AMBITOS = ("casa", "personal", "split", "interno")
TIPOS = ("gasto", "ingreso", "transferencia")
# Columnas que resolver NO toca: la identidad de la fila. `mes` SI: por defecto
# sigue a la fecha, pero se puede imputar a otro mes a proposito (una cuota de
# setiembre pagada el 2/10: la fecha queda real -- la usa la conciliacion
# bancaria -- y el mes dice a que mes pertenece el gasto).
NO_RESOLVIBLES = {"id", "origen", "fuente_id"}
SIN_BORRADO = {"1money": "usa `resolver <id> en_analisis=0`: sus filas se "
                         "regeneran desde data/staging/ en cada corrida",
               "planilla_casa": "usa `resolver <id> en_analisis=0`: sus filas se "
                                "regeneran desde data/staging/ en cada corrida",
               "carga_manual": "borra la fila en la planilla \"Carga manual\": el "
                               "pipeline la toma de ahi en cada corrida"}

HOY = dt.date.today().isoformat()


# ------------------------------------------------------------------ util
class Error(Exception):
    pass


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


def agregar_csv(path, cols, rows):
    """Agrega AL FINAL sin reescribir: reescribir con csv.DictWriter re-cotiza
    campos viejos (saca comillas innecesarias) y ensucia el diff de git con
    lineas que no cambiaron (medido el 27/9 sobre resoluciones.csv)."""
    if not os.path.exists(path):
        return escribir_csv(path, cols, rows)
    with open(path, "rb") as f:
        crudo = f.read()
    assert crudo.split(b"\n", 1)[0].decode("utf-8-sig").strip() == ",".join(cols), \
        f"columnas de {path} != esperadas"
    nl = "\r\n" if b"\r\n" in crudo else "\n"
    with open(path, "a", encoding="utf-8", newline="") as f:
        if crudo and not crudo.endswith(b"\n"):
            f.write(nl)
        w = csv.DictWriter(f, lineterminator=nl, fieldnames=cols)
        w.writerows(rows)


def dec(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt(x):
    return f"{dec(x):.2f}"


def num(s, que="monto"):
    """'1.090,50' | '1090.50' | '1090' | '$ 1.090' -> Decimal."""
    t = str(s).strip().replace("$", "").replace(" ", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return Decimal(t)
    except Exception:
        raise Error(f"{que} {s!r} no es un numero")


def fecha_iso(s):
    s = str(s).strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    raise Error(f"fecha {s!r}: usar AAAA-MM-DD o DD/MM/AAAA")


def slug(t, n=28):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:n].strip("-") or "sin-desc"


def hid(fuente_id):
    """Mismo esquema que las 50 filas manual existentes."""
    return hashlib.sha1(("manual" + fuente_id).encode()).hexdigest()[:12]


def tc(moneda):
    if moneda == "UYU":
        return Decimal(1)
    with open(PARAMETROS, encoding="utf-8") as f:
        return Decimal(str(json.load(f)[f"tc_{moneda.lower()}_uyu"]))


def catalogo_cuentas():
    return {r["cuenta"]: r for r in leer_csv(
        CUENTAS_CSV, ["cuenta", "alias_1money", "tarjetas", "moneda", "tipo", "activa", "notas"])}


def catalogo_categorias():
    return {r["categoria"]: r["tipo"] for r in leer_csv(CATEG_CSV, ["categoria", "tipo", "orden"])}


def validar_cuenta(c, que="cuenta"):
    cat = catalogo_cuentas()
    if c not in cat:
        parecidas = [x for x in cat if slug(c)[:5] in slug(x)]
        raise Error(f"{que} {c!r} no esta en config/cuentas.csv"
                    + (f" (¿quisiste decir {', '.join(repr(x) for x in parecidas[:4])}?)" if parecidas else ""))
    if cat[c]["activa"] == "no":
        print(f"  OJO: {c!r} esta marcada inactiva en config/cuentas.csv")
    return c


def validar_categoria(k, tipo="gasto"):
    cat = catalogo_categorias()
    if k not in cat:
        raise Error(f"categoria {k!r} no esta en config/categorias.csv. Opciones: "
                    + ", ".join(c for c, t in cat.items() if (t == "Ingreso") == (tipo == "ingreso")))
    return k


# ------------------------------------------------------------------ estado
class Estado:
    """Todo lo que un subcomando lee, y lo que planea escribir."""

    def __init__(self):
        self.ledger = leer_csv(LEDGER, COLUMNS)
        self.por_id = {r["id"]: r for r in self.ledger}
        self.borrados = fuentes.leer_borrados()
        self.filas_nuevas, self.resoluciones, self.aportes_lu = [], [], []
        self.participaciones, self.bajas = [], []
        self.toca_casa, self.meses = False, set()
        self.avisos = []

    def fila(self, i):
        if i not in self.por_id:
            if i in self.borrados:
                raise Error(f"{i} esta dado de baja (data/fuentes/borrados.csv): "
                            f"{self.borrados[i]['motivo']}")
            raise Error(f"no hay ninguna fila con id {i} en data/ledger.csv")
        return self.por_id[i]

    def id_libre(self, fuente_id):
        i = hid(fuente_id)
        otro = [r for r in fuentes.leer("manual") if r["id"] == i or r["fuente_id"] == fuente_id]
        if i in self.por_id or i in self.borrados or otro \
                or any(r["id"] == i for r in self.filas_nuevas):
            raise Error(f"ya existe una fila con fuente_id {fuente_id!r} (id {i}). Si es "
                        "OTRO movimiento igual, pasa --sufijo 2 (o 3, ...) para distinguirlo")
        return i

    def nueva_fila(self, **campos):
        r = {c: "" for c in COLUMNS}
        r.update(campos)
        r.update(mes=r["fecha"][:7], origen="manual")
        r.setdefault("estado", "confirmado")
        r["estado"] = r["estado"] or "confirmado"
        r["id"] = self.id_libre(r["fuente_id"])
        if r["tipo"] in ("gasto", "ingreso") and not r["monto_uyu"]:
            raise Error("gasto/ingreso sin monto_uyu (no sumaria en ningun agregado)")
        self.filas_nuevas.append(r)
        self._casa(r)
        return r

    def resolver(self, i, campo, valor, nota):
        self.resoluciones.append(dict(id_ledger=i, campo=campo, valor=valor, nota=nota, fecha=HOY))

    def _casa(self, r):
        if r["ambito"] == "casa" or r["cuenta"] in CUENTAS_CASA or r["cuenta_destino"] in CUENTAS_CASA \
                or r["cuenta"].startswith("Lu "):
            self.toca_casa = True
            self.meses.add(r["fecha"][:7])

    def pendientes_abiertos(self, i):
        return [p for p in leer_csv(PENDIENTES, PEND_COLS) if p["id_ledger"] == i and not p["resuelto"]]

    # -------------------------------------------------------------- salida
    def mostrar(self):
        if self.filas_nuevas:
            print(f"\nfilas nuevas (origen=manual -> data/fuentes/manual.csv + data/ledger.csv): "
                  f"{len(self.filas_nuevas)}")
            for r in self.filas_nuevas:
                dest = f" -> {r['cuenta_destino']}" if r["cuenta_destino"] else ""
                print(f"  + {r['id']}  {r['fecha']}  {r['tipo']:13s} {r['moneda']} {r['monto']:>10s} "
                      f"(UYU {r['monto_uyu'] or '-'})  {r['cuenta']}{dest}  "
                      f"{r['categoria'] or '-'} / {r['ambito']}  en_analisis={r['en_analisis']}")
                print(f"      {r['descripcion']}")
        if self.resoluciones:
            print(f"\ndata/resoluciones.csv: +{len(self.resoluciones)}")
            for r in self.resoluciones:
                print(f"  + {r['id_ledger']}  {r['campo']} = {r['valor']}   ({r['nota'][:70]})")
        if self.participaciones:
            print("\ndata/participaciones.csv:")
            for r in self.participaciones:
                print(f"  + {r['id_ledger']}  Lu {r['frac_lu']} / Joaco {r['frac_joaco']}  ({r['nota'][:60]})")
        if self.aportes_lu:
            print("\ndata/aportes_lu.csv:")
            for r in self.aportes_lu:
                print(f"  + {r['mes']}  {r['fecha'] or '(sin fecha)'}  {r['monto']}  ({r['nota'][:60]})")
        if self.bajas:
            print("\nbajas (data/fuentes/borrados.csv):")
            for r, motivo in self.bajas:
                print(f"  - {r['id']}  {r['origen']}  {r['fecha']} {r['moneda']} {r['monto']}  "
                      f"{(r['descripcion'] or r['comercio'])[:50]}   motivo: {motivo}")
        for a in self.avisos:
            print(f"\nOJO: {a}")

    def aplicar(self):
        if self.filas_nuevas:
            fuentes.escribir("manual", fuentes.leer("manual") + self.filas_nuevas)
            self.ledger.extend(self.filas_nuevas)
        for r, motivo in self.bajas:
            fuentes.registrar_borrado(r, motivo)
            if r["origen"] in fuentes.ORIGENES_CON_FUENTE:
                fuentes.escribir(r["origen"], [x for x in fuentes.leer(r["origen"]) if x["id"] != r["id"]])
            self.ledger = [x for x in self.ledger if x["id"] != r["id"]]
        if self.filas_nuevas or self.bajas:
            self.ledger.sort(key=lambda r: (r["fecha"], r["id"]))
            escribir_csv(LEDGER, COLUMNS, self.ledger)
        if self.resoluciones:
            agregar_csv(RESOLUCIONES, RES_COLS, self.resoluciones)
        if self.participaciones:
            ids = {p["id_ledger"] for p in self.participaciones}
            previas = leer_csv(PARTICIPACIONES, PART_COLS)
            if any(p["id_ledger"] in ids for p in previas):     # reemplaza la fraccion pactada
                escribir_csv(PARTICIPACIONES, PART_COLS,
                             [p for p in previas if p["id_ledger"] not in ids] + self.participaciones)
            else:
                agregar_csv(PARTICIPACIONES, PART_COLS, self.participaciones)
        if self.aportes_lu:
            agregar_csv(APORTES_LU, APORTES_COLS, self.aportes_lu)

    def vacio(self):
        return not (self.filas_nuevas or self.resoluciones or self.aportes_lu
                    or self.participaciones or self.bajas)


def pipeline(est):
    """Lo mismo que el paso 8 de /revisar-pendientes, en un solo lugar."""
    pasos = [("procesar_mails.py",), ("merge_y_validar.py",)]
    if est.toca_casa and est.meses:
        # Tocar un mes ya cerrado cambia su posicion, y la de todos los que
        # vienen despues (cada cierre arranca de la posicion del anterior).
        desde = min(est.meses)
        cerrados = {r["mes"] for r in leer_csv(P("data", "cierre_casa.csv"),
                                               ["mes", "parte_lu", "parte_joaco", "aportes_lu",
                                                "aportes_joaco", "posicion_lu", "posicion_joaco", "nota"])}
        pasos += [("cierre_casa.py", m) for m in sorted({m for m in cerrados if m > desde} | est.meses)]
    for script, *args in pasos:
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        lineas = p.stdout.splitlines()
        if script == "merge_y_validar.py":
            clave = [l for l in lineas if l.startswith(("[FALLO]", "[DESVIO]")) or "validacion" in l]
        elif script == "cierre_casa.py":
            clave = [l for l in lineas if l.startswith(("=== Cierre", "aportes", "POSICION"))]
        else:
            clave = [l for l in lineas if "pendientes nuevos" in l or "resoluciones aplicadas" in l]
        print(f"\n[{script} {' '.join(args)}] exit {p.returncode}")
        for l in clave[:12]:
            print("  " + l)
        if p.returncode != 0:
            print(p.stderr[-1500:])
            print(f"\nFALLO {script}: lo que se escribio QUEDA escrito. Revisar antes de commitear.")
            return p.returncode
    return 0


# ------------------------------------------------------------------ partes (split)
RE_FRAC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def valor_parte(txt, total):
    """'1/3' | '50%' | '1433.33' -> Decimal (importe, sin redondear)."""
    m = RE_FRAC.match(txt)
    if m:
        return total * Decimal(m.group(1)) / Decimal(m.group(2))
    if txt.strip().endswith("%"):
        return total * num(txt.strip()[:-1], "porcentaje") / 100
    return num(txt, "parte")


def cmd_split(est, a):
    o = est.fila(a.id)
    if o["tipo"] != "gasto":
        raise Error(f"{a.id} es {o['tipo']}, no gasto: solo se dividen gastos")
    hermanas_prev = [r for r in est.ledger if r["fuente_id"].startswith(a.id + "-split")]
    if hermanas_prev:
        raise Error(f"{a.id} ya tiene split: {', '.join(r['id'] + ' -> ' + r['cuenta_destino'] + ' ' + r['monto'] for r in hermanas_prev)}")
    total, total_uyu = dec(o["monto"]), dec(o["monto_uyu"] or 0)
    if not total_uyu:
        raise Error(f"{a.id} no tiene monto_uyu: resolverlo antes de dividir")

    destinos = []                         # [(cuenta, Decimal | None)]
    for d in (a.a or ["Deudas"]):
        cuenta, _, val = d.partition("=")
        destinos.append((validar_cuenta(cuenta.strip(), "destino"), val.strip() or None))
    if sum(1 for _, v in destinos if v is None) > 1:
        raise Error("solo UN destino puede llevarse el resto (los demas necesitan =X)")
    mia = dec(valor_parte(a.mia, total))
    fijos = [(c, dec(valor_parte(v, total))) for c, v in destinos if v is not None]
    resto = [c for c, v in destinos if v is None]
    partes = list(fijos)
    if resto:
        partes.append((resto[0], total - mia - sum(v for _, v in fijos)))
    else:
        # sin destino "resto": el redondeo lo absorbe tu parte, si la diferencia
        # es de centavos. Si no, las partes no suman el cargo.
        dif = total - mia - sum(v for _, v in fijos)
        if abs(dif) > Decimal("0.02"):
            raise Error(f"las partes no suman el cargo: tu parte {mia} + "
                        f"{' + '.join(str(v) for _, v in fijos)} = {mia + sum(v for _, v in fijos)} "
                        f"!= {total}. Deja un destino sin =X para que se lleve el resto")
        mia += dif
    if any(v <= 0 for _, v in partes) or mia <= 0:
        raise Error(f"alguna parte queda <= 0: tu parte {mia}, " + ", ".join(f"{c} {v}" for c, v in partes))

    # monto_uyu proporcional; el redondeo lo absorbe la fila original, asi la
    # suma en pesos tambien es exacta.
    partes_uyu = [dec(total_uyu * v / total) for _, v in partes]
    mia_uyu = total_uyu - sum(partes_uyu)

    nota = a.nota or ""
    que = (o["comercio"] or o["descripcion"])[:50]
    sufijos = [""] if len(partes) == 1 else [f"-{chr(97 + i)}" for i in range(len(partes))]
    for (cuenta, v), v_uyu, suf in zip(partes, partes_uyu, sufijos):
        est.nueva_fila(
            fecha=o["fecha"], tipo="transferencia", monto=fmt(v), moneda=o["moneda"],
            monto_uyu=fmt(v_uyu), cuenta=o["cuenta"], cuenta_destino=cuenta,
            descripcion=f"{que} {o['fecha'][8:10]}/{o['fecha'][5:7]}: parte ajena -> {cuenta}"
                        + (f" ({nota})" if nota else ""),
            ambito="interno", en_analisis="0", fuente_id=f"{a.id}-split{suf}")
    detalle = (f"tu parte {fmt(mia)} + " + " + ".join(f"{c} {fmt(v)}" for c, v in partes)
               + f" = {fmt(total)} {o['moneda']}" + (f" -- {nota}" if nota else ""))
    est.resolver(a.id, "monto", fmt(mia), f"split: {detalle}")
    est.resolver(a.id, "monto_uyu", fmt(mia_uyu), f"split: {fmt(mia_uyu)} de {fmt(total_uyu)} UYU")
    est.resolver(a.id, "motivo_resuelto", "split_a_cargar", f"split: {detalle}")
    assert dec(mia) + sum(v for _, v in partes) == total
    assert mia_uyu + sum(partes_uyu) == total_uyu
    if a.participacion_lu is not None:
        participacion(est, a.id, a.participacion_lu, nota or "split")
    if o["cuenta"] in CUENTAS_CASA:
        est.toca_casa = True
        est.meses.add(o["mes"])
        est.avisos.append(
            f"el cargo salio de {o['cuenta']} (cuenta de la casa): las hermanas a Deudas "
            "cuentan como RETIRO tuyo del fondo en cierre_casa. Cuando cobres por Splitwise, "
            "transferi esa plata a la Cuenta Casa y el retiro se cancela.")
    if any(c == "Deudas" for c, _ in partes):
        est.avisos.append("acordate de cargarlo en Splitwise (la parte que va a Deudas).")


def participacion(est, i, frac_lu, nota):
    f = num(frac_lu, "participacion-lu")
    if not (0 <= f <= 1):
        raise Error("--participacion-lu va de 0 a 1 (0.5 = mitad y mitad; 1 = todo de Lu)")
    def frac(x):                          # mismo formato que el archivo: 0.5, 1.0, 0.6667
        s = f"{x:.4f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    est.participaciones.append(dict(id_ledger=i, frac_lu=frac(f), frac_joaco=frac(1 - f),
                                    nota=f"{nota} ({HOY})"))
    est.toca_casa = True
    est.meses.add(est.fila(i)["mes"])


# ------------------------------------------------------------------ altas simples
def monto_y_uyu(a):
    m = num(a.monto)
    if m == 0:
        raise Error("monto 0")
    moneda = a.moneda.upper()
    if moneda not in MONEDAS:
        raise Error(f"moneda {a.moneda!r}: {MONEDAS}")
    if getattr(a, "monto_uyu", None):
        uyu = num(a.monto_uyu, "monto-uyu")
    else:
        uyu = m * tc(moneda)
        if moneda != "UYU":
            print(f"  monto_uyu estimado con tc {tc(moneda)} de config/parametros.json")
    return m, moneda, uyu


def cmd_gasto(est, a):
    m, moneda, uyu = monto_y_uyu(a)
    validar_categoria(a.categoria, "ingreso" if a.ingreso else "gasto")
    if a.ambito not in ("casa", "personal", "split"):
        raise Error("--ambito casa | personal | split")
    fecha = fecha_iso(a.fecha)
    tipo = "ingreso" if a.ingreso else "gasto"
    est.nueva_fila(fecha=fecha, tipo=tipo, monto=fmt(m), moneda=moneda, monto_uyu=fmt(uyu),
                   cuenta=validar_cuenta(a.cuenta), categoria=a.categoria,
                   comercio=a.comercio or "", descripcion=a.desc, ambito=a.ambito,
                   en_analisis=a.en_analisis,
                   fuente_id=f"{tipo}-{fecha}-{fmt(abs(m))}-{slug(a.desc)}{sufijo(a)}")


def cmd_pago_lu(est, a):
    m, moneda, uyu = monto_y_uyu(a)
    validar_categoria(a.categoria)
    fecha = fecha_iso(a.fecha)
    cuenta = "Lu efectivo" if a.efectivo else "Lu tarjeta"
    validar_cuenta(cuenta)
    est.nueva_fila(fecha=fecha, tipo="gasto", monto=fmt(m), moneda=moneda, monto_uyu=fmt(uyu),
                   cuenta=cuenta, categoria=a.categoria,
                   descripcion=f"{a.desc} (pagado por Lu{' en efectivo' if a.efectivo else ' con su tarjeta'}: aporte de ella)",
                   ambito="casa", en_analisis="1",
                   fuente_id=f"lu-{fecha}-{fmt(m)}-{slug(a.desc)}{sufijo(a)}")


def cmd_aporte(est, a):
    m = num(a.monto)
    if m <= 0:
        raise Error("un aporte es > 0")
    if not (a.fecha or a.mes):
        raise Error("--fecha AAAA-MM-DD, o --mes AAAA-MM si no sabes el dia")
    fecha = fecha_iso(a.fecha) if a.fecha else ""
    mes = fecha[:7] if fecha else a.mes
    if not re.fullmatch(r"\d{4}-\d{2}", mes):
        raise Error(f"--mes {mes!r}: AAAA-MM")
    est.toca_casa = True
    est.meses.add(mes)
    if a.quien == "lu":
        repetidos = [r for r in leer_csv(APORTES_LU, APORTES_COLS)
                     if r["mes"] == mes and dec(r["monto"]) == dec(m)]
        if repetidos and not a.forzar:
            raise Error(f"ya hay un aporte de Lu de {fmt(m)} en {mes} en data/aportes_lu.csv "
                        f"({repetidos[0]['nota'][:60]!r}). Si es OTRO aporte del mismo monto, --forzar")
        est.aportes_lu.append(dict(mes=mes, fecha=fecha, monto=fmt(m),
                                   nota=a.nota or f"aporte de Lu, cargado con gastos.py el {HOY}"))
        return
    if not fecha:
        raise Error("el aporte de Joaco es una transferencia del ledger: necesita --fecha")
    desde = validar_cuenta(a.desde)
    f0 = dt.date.fromisoformat(fecha)
    parecidos = [r for r in est.ledger if r["tipo"] == "transferencia"
                 and r["cuenta_destino"] == "Cuenta Casa" and dec(r["monto"]) == dec(m)
                 and abs((dt.date.fromisoformat(r["fecha"]) - f0).days) <= 3]
    if parecidos and not a.forzar:
        raise Error(f"ya hay una transferencia de {fmt(m)} a Cuenta Casa el {parecidos[0]['fecha']} "
                    f"({parecidos[0]['id']}, {parecidos[0]['origen']}). Si es otra, --forzar")
    est.nueva_fila(fecha=fecha, tipo="transferencia", monto=fmt(m), moneda="UYU", monto_uyu=fmt(m),
                   cuenta=desde, cuenta_destino="Cuenta Casa",
                   descripcion=a.nota or "Aporte de Joaco a la casa",
                   ambito="interno", en_analisis="0",
                   fuente_id=f"aporte-joaco-{fmt(m)}-{fecha}{sufijo(a)}")


def cmd_transferencia(est, a):
    m, moneda, uyu = monto_y_uyu(a)
    fecha = fecha_iso(a.fecha)
    if a.de == a.a:
        raise Error("origen y destino iguales")
    est.nueva_fila(fecha=fecha, tipo="transferencia", monto=fmt(m), moneda=moneda,
                   monto_uyu=fmt(uyu), cuenta=validar_cuenta(a.de, "--de"),
                   cuenta_destino=validar_cuenta(a.a, "--a"), descripcion=a.desc,
                   ambito="interno", en_analisis=a.en_analisis,
                   fuente_id=f"transf-{fecha}-{fmt(m)}-{slug(a.desc)}{sufijo(a)}")


def cmd_cobro(est, a):
    m, moneda, uyu = monto_y_uyu(a)
    fecha = fecha_iso(a.fecha)
    est.nueva_fila(fecha=fecha, tipo="transferencia", monto=fmt(m), moneda=moneda,
                   monto_uyu=fmt(uyu), cuenta=validar_cuenta(a.de, "--de"),
                   cuenta_destino=validar_cuenta(a.a, "--a"), descripcion=a.desc,
                   ambito="interno", en_analisis="0",
                   fuente_id=f"cobro-{fecha}-{fmt(m)}-{slug(a.desc)}{sufijo(a)}")


def sufijo(a):
    return f"-{a.sufijo}" if getattr(a, "sufijo", None) else ""


# ------------------------------------------------------------------ resolver / borrar
def cmd_resolver(est, a):
    if not (a.cambios or a.cerrar or a.participacion_lu is not None):
        raise Error("nada que resolver: pasa campo=valor, --cerrar MOTIVO o --participacion-lu")
    if not a.nota:
        raise Error("--nota es obligatoria: es la memoria del porque")
    if a.id not in est.por_id:
        # Pendientes SIN fila: sync_splitwise y procesar_mails los crean sobre el
        # id que la fila HABRIA tenido (split "SIN cargo que matchee",
        # formato_nuevo). Solo se pueden cerrar, no corregir.
        abiertos = {p["motivo"] for p in est.pendientes_abiertos(a.id)}
        if a.cerrar and not a.cambios and a.participacion_lu is None and abiertos:
            for motivo in a.cerrar:
                if motivo not in abiertos:
                    raise Error(f"{a.id} no tiene fila en el ledger ni pendiente {motivo!r} abierto "
                                f"(abiertos: {sorted(abiertos)})")
                est.resolver(a.id, "motivo_resuelto", motivo, a.nota)
            return
        est.fila(a.id)                    # levanta el error con el detalle
    r = est.fila(a.id)
    nuevos = {}
    for c in a.cambios:
        campo, sep, valor = c.partition("=")
        campo = campo.strip()
        if not sep:
            raise Error(f"{c!r}: el formato es campo=valor")
        if campo not in COLUMNS:
            raise Error(f"campo {campo!r} no existe. Columnas: {', '.join(COLUMNS)}")
        if campo in NO_RESOLVIBLES:
            raise Error(f"{campo!r} no se corrige por resolucion (es la identidad de la fila)")
        nuevos[campo] = valor
    tipo = nuevos.get("tipo", r["tipo"])
    if "tipo" in nuevos and tipo not in TIPOS:
        raise Error(f"tipo {tipo!r}: {TIPOS}")
    if "ambito" in nuevos and nuevos["ambito"] not in AMBITOS:
        raise Error(f"ambito {nuevos['ambito']!r}: {AMBITOS}")
    if "en_analisis" in nuevos and nuevos["en_analisis"] not in ("0", "1"):
        raise Error("en_analisis: 0 o 1")
    if "moneda" in nuevos and nuevos["moneda"] not in MONEDAS:
        raise Error(f"moneda: {MONEDAS}")
    for c in ("cuenta", "cuenta_destino"):
        if nuevos.get(c):
            validar_cuenta(nuevos[c], c)
    if nuevos.get("categoria"):
        validar_categoria(nuevos["categoria"], tipo)
    for c in ("monto", "monto_uyu"):
        if c in nuevos:
            nuevos[c] = fmt(num(nuevos[c], c))
    if "fecha" in nuevos:
        nuevos["fecha"] = fecha_iso(nuevos["fecha"])
        # el mes sigue a la fecha, salvo que se impute otro explicitamente
        if "mes" not in nuevos and nuevos["fecha"][:7] != r["mes"]:
            nuevos["mes"] = nuevos["fecha"][:7]
            print(f"  mes = {nuevos['mes']} (sigue a la fecha nueva)")
    if "mes" in nuevos:
        if not re.fullmatch(r"\d{4}-\d{2}", nuevos["mes"]):
            raise Error(f"mes {nuevos['mes']!r}: AAAA-MM")
        if nuevos["mes"] != nuevos.get("fecha", r["fecha"])[:7]:
            est.avisos.append(f"IMPUTACION: la fila cuenta en {nuevos['mes']} aunque su fecha sea "
                              f"{nuevos.get('fecha', r['fecha'])} (la fecha real se conserva para la "
                              "conciliacion con el banco)")
    # transferencia -> gasto/ingreso: un gasto sin monto_uyu no suma en ningun
    # agregado (paso dos veces, SA2). Se exige o se completa.
    if tipo in ("gasto", "ingreso") and not (nuevos.get("monto_uyu") or r["monto_uyu"]):
        moneda = nuevos.get("moneda", r["moneda"])
        if moneda != "UYU":
            raise Error(f"la fila queda como {tipo} en {moneda} sin monto_uyu: pasa monto_uyu=...")
        nuevos["monto_uyu"] = nuevos.get("monto", r["monto"])
        print(f"  monto_uyu = {nuevos['monto_uyu']} (UYU: igual al monto)")
    if tipo == "transferencia" and not (nuevos.get("cuenta_destino") or r["cuenta_destino"]):
        raise Error("una transferencia necesita cuenta_destino (si no, la plata desaparece)")
    for campo, valor in nuevos.items():
        if r[campo] == valor:
            print(f"  (sin cambio) {campo} ya vale {valor!r}")
            continue
        est.resolver(a.id, campo, valor, a.nota)
    abiertos = {p["motivo"] for p in est.pendientes_abiertos(a.id)}
    for motivo in a.cerrar or []:
        if motivo not in abiertos:
            est.avisos.append(f"{a.id} no tiene pendiente abierto {motivo!r} "
                              f"(abiertos: {sorted(abiertos) or 'ninguno'}); se registra igual y "
                              "suprime que vuelva a nacer")
        est.resolver(a.id, "motivo_resuelto", motivo, a.nota)
    if a.participacion_lu is not None:
        participacion(est, a.id, a.participacion_lu, a.nota)
    fila_final = dict(r, **nuevos)
    est._casa(fila_final)
    if r["ambito"] == "casa":
        est._casa(r)
    if est.toca_casa:                     # una imputacion mueve el gasto de mes
        est.meses |= {fila_final["mes"], r["mes"]}


def cmd_borrar(est, a):
    r = est.fila(a.id)
    if r["origen"] in SIN_BORRADO:
        raise Error(f"{a.id} es origen={r['origen']}: no se borra. {SIN_BORRADO[r['origen']]}")
    if not a.motivo:
        raise Error("--motivo es obligatorio")
    hermanas = [x for x in est.ledger if x["fuente_id"].startswith(a.id + "-split")]
    if hermanas and not a.forzar:
        raise Error(f"{a.id} tiene filas hermanas de split ({', '.join(x['id'] for x in hermanas)}): "
                    "borrarla deja las hermanas huerfanas. Borralas primero, o --forzar")
    est.bajas.append((r, a.motivo))
    for p in est.pendientes_abiertos(a.id):
        est.resolver(a.id, "motivo_resuelto", p["motivo"], f"fila dada de baja: {a.motivo}")
    est._casa(r)


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def comun(p):
        p.add_argument("--aplicar", action="store_true", help="escribir (sin esto: en seco)")
        p.add_argument("--sin-pipeline", action="store_true",
                       help="con --aplicar: no correr procesar_mails/merge/cierre despues")
        return p

    def alta(p, moneda=True):
        comun(p)
        p.add_argument("--fecha", required=True)
        p.add_argument("--monto", required=True)
        if moneda:
            p.add_argument("--moneda", default="UYU")
            p.add_argument("--monto-uyu", help="equivalente en pesos (default: monto x tc)")
        p.add_argument("--sufijo", help="distingue dos movimientos identicos del mismo dia")
        return p

    p = comun(sub.add_parser("split", help="divide un gasto en tu parte + hermanas"))
    p.add_argument("id")
    p.add_argument("--mia", required=True, help="tu parte: importe, a/b o N%%")
    p.add_argument("--a", action="append", metavar="DESTINO[=X]",
                   help="destino de una parte ajena (default: Deudas con el resto)")
    p.add_argument("--participacion-lu", help="tu parte es de casa y se reparte fijo: fraccion de Lu (0-1)")
    p.add_argument("--nota", default="")

    p = alta(sub.add_parser("gasto", help="gasto (o --ingreso) manual"))
    p.add_argument("--cuenta", required=True)
    p.add_argument("--categoria", required=True)
    p.add_argument("--ambito", required=True)
    p.add_argument("--desc", required=True)
    p.add_argument("--comercio", default="")
    p.add_argument("--ingreso", action="store_true")
    p.add_argument("--en-analisis", default="1", choices=("0", "1"))

    p = alta(sub.add_parser("pago-lu", help="gasto de casa que pago Lu"))
    p.add_argument("--categoria", required=True)
    p.add_argument("--desc", required=True)
    p.add_argument("--efectivo", action="store_true", help="lo pago en efectivo, no con su tarjeta")

    p = comun(sub.add_parser("aporte", help="aporte a la casa"))
    p.add_argument("--quien", required=True, choices=("lu", "joaco"))
    p.add_argument("--monto", required=True)
    p.add_argument("--fecha")
    p.add_argument("--mes", help="AAAA-MM, si no sabes el dia (solo Lu)")
    p.add_argument("--desde", default="Santander UYU", help="cuenta de origen (Joaco)")
    p.add_argument("--nota", default="")
    p.add_argument("--forzar", action="store_true")
    p.add_argument("--sufijo")

    p = alta(sub.add_parser("transferencia", help="transferencia entre cuentas"))
    p.add_argument("--de", required=True)
    p.add_argument("--a", required=True)
    p.add_argument("--desc", required=True)
    p.add_argument("--en-analisis", default="0", choices=("0", "1"))

    p = alta(sub.add_parser("cobro", help="te pagan algo de 'A cobrar'"))
    p.add_argument("--de", default="A cobrar")
    p.add_argument("--a", default="Santander UYU")
    p.add_argument("--desc", required=True)

    p = comun(sub.add_parser("resolver", help="corrige una fila via resoluciones.csv"))
    p.add_argument("id")
    p.add_argument("cambios", nargs="*", metavar="campo=valor")
    p.add_argument("--cerrar", action="append", metavar="MOTIVO")
    p.add_argument("--participacion-lu")
    p.add_argument("--nota", default="")

    p = comun(sub.add_parser("borrar", help="da de baja una fila (con registro)"))
    p.add_argument("id")
    p.add_argument("--motivo", default="")
    p.add_argument("--forzar", action="store_true")

    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    est = Estado()
    try:
        {"split": cmd_split, "gasto": cmd_gasto, "pago-lu": cmd_pago_lu, "aporte": cmd_aporte,
         "transferencia": cmd_transferencia, "cobro": cmd_cobro, "resolver": cmd_resolver,
         "borrar": cmd_borrar}[a.cmd](est, a)
    except Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if est.vacio():
        print("nada para escribir")
        return 0
    est.mostrar()
    if not a.aplicar:
        print("\n(en seco: no se escribio nada. Repeti con --aplicar.)")
        return 0
    est.aplicar()
    print("\nescrito.")
    return 0 if a.sin_pipeline else pipeline(est)


if __name__ == "__main__":
    sys.exit(main())
