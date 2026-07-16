# -*- coding: utf-8 -*-
"""
Migra los 19 presupuestos mensuales (Google Drive, "YYYY-MM") a data/presupuesto.csv.

Entrada : data/raw/presupuestos/YYYY-MM.txt  (contentSnippet crudo de get_file_metadata)
Salida  : data/presupuesto.csv  con columnas:
          mes,concepto_original,categoria,tipo,presupuesto,gasto_real_registrado,comentario

Reglas (docs/esquema.md, seccion "data/presupuesto.csv" y "Presupuestos"):
- Celdas vacias omitidas en el CSV del snippet (no queda coma vacia).
- "Variacion" usa coma decimal -> queda partida en dos campos ("-3","2%").
- Punto = separador de miles ("22.400" = 22400).
- Fila de total: la ultima, sin etiqueta (primer campo numerico) -> se usa solo para control.
- Excluir "Sin Pago Auto" y celdas auxiliares sueltas.
- Desde 2025-06 hay bloque lateral fijo mezclado ("Oportunidad de Ajustar") -> ignorarlo.
- 2025-04 tiene comparativo lateral (repite concepto,tipo,presupuesto nuevo) -> cortar ahi.
- concepto -> categoria nueva via config/mapeo_categorias.csv (case-insensitive, strip);
  sin mapeo -> "Otros" y se reporta.
- Gasto Real vacio en la hoja -> 0.00 (mes sin gasto registrado; p.ej. todo 2026-07).
"""
import os
import csv
import io
import re
import sys
from pathlib import Path

BASE = Path(os.environ.get("GASTOS_BASE") or Path(__file__).resolve().parent.parent)
RAW_DIR = BASE / "data" / "raw" / "presupuestos"
OUT_CSV = BASE / "data" / "presupuesto.csv"
MAPEO_CSV = BASE / "config" / "mapeo_categorias.csv"

# Controles esperados (Ppto / Gasto) provistos por el orquestador
ESPERADOS = {
    "2025-01": (75680, 111496),
    "2025-06": (109413, 91695),
    "2025-12": (106801, 148901),
    "2026-06": (169835, 262321),
    "2026-07": (107745, 0),
}

# Bloque lateral fijo (desde 2025-06) que el snippet mezcla al final de algunas filas.
# Se comparan tokens ya stripeados.
JUNK_SEQS = [
    ["Ropa / Peluqueria", "sin comprar ropa", "puedo ir a 1500"],
    ["Otras Salidas", "Nada programado", "puede bajar"],
    ["Otos Gastos", "Podría ser 0"],
]

NUM_RE = re.compile(r"^-?\d{1,3}(\.\d{3})+$|^-?\d+$")
PCT_RE = re.compile(r"^\d+%$")


def es_num(tok: str) -> bool:
    return bool(NUM_RE.match(tok.strip()))


def a_num(tok: str) -> float:
    return float(tok.strip().replace(".", ""))


def quitar_junk(tokens):
    """Elimina las secuencias contiguas del bloque lateral fijo."""
    toks = list(tokens)
    cambiado = True
    while cambiado:
        cambiado = False
        stripped = [t.strip() for t in toks]
        for seq in JUNK_SEQS:
            n = len(seq)
            for i in range(len(stripped) - n + 1):
                if stripped[i:i + n] == seq:
                    del toks[i:i + n]
                    cambiado = True
                    break
            if cambiado:
                break
    return toks


def extraer_bloque_presupuesto(texto: str):
    """Devuelve las lineas del tab Presupuesto (dentro del fence tras '# Presupuesto')."""
    m = re.search(r"# Presupuesto\s*```(.*?)```", texto, re.S)
    if not m:
        raise ValueError("No se encontro el bloque Presupuesto")
    return [l for l in m.group(1).splitlines()]


def parse_mes(path: Path, mapeo, sin_mapeo, warnings):
    mes = path.stem
    lineas = extraer_bloque_presupuesto(path.read_text(encoding="utf-8"))

    # ubicar header
    idx = None
    for i, l in enumerate(lineas):
        if l.startswith("Conceptos,Tipo,Presupuesto"):
            idx = i
            break
    if idx is None:
        raise ValueError(f"{mes}: sin header de Presupuesto")

    filas = []
    total_row = None
    for l in lineas[idx + 1:]:
        if not l.strip():
            continue
        toks = l.split(",")
        f0 = toks[0].strip()
        if es_num(f0):
            # fila de total (la primera con >=2 campos numericos) o celda suelta
            if total_row is None and len(toks) >= 2 and es_num(toks[1]):
                total_row = (a_num(toks[0]), a_num(toks[1]))
            continue
        if f0 == "Sin Pago Auto":
            continue

        concepto = f0
        tipo = toks[1].strip()
        if tipo == "Varable":
            tipo = "Variable"
        if tipo not in ("Fijo", "Variable"):
            warnings.append(f"{mes} | {concepto}: tipo raro '{tipo}'")

        rest = toks[2:]

        # 2025-04: comparativo lateral repite el concepto -> cortar ahi
        if mes == "2025-04":
            for i, t in enumerate(rest):
                if t.strip() == concepto:
                    rest = rest[:i]
                    break
            # el corte deja colgado el 'Tipo' del lateral en 0 casos (concepto va primero)

        rest = quitar_junk(rest)

        # caminar tokens: numeros iniciales
        nums = []
        i = 0
        while i < len(rest) and es_num(rest[i]):
            nums.append(a_num(rest[i]))
            i += 1
        variacion = None
        if i < len(rest):
            t = rest[i].strip()
            if PCT_RE.match(t) and nums:
                variacion = float(f"{int(nums.pop())}.{t[:-1]}")
                i += 1
            elif t == "-":
                i += 1
        # resto = comentarios; numeros puros aca son lateral (2025-04) -> descartar
        comentarios = [t.strip() for t in rest[i:] if t.strip() and not es_num(t)
                       and not PCT_RE.match(t.strip()) and t.strip() != "-"]
        comentario = "; ".join(comentarios)

        # asignar P, G, D
        p = g = d = None
        if len(nums) >= 3:
            p, g, d = nums[0], nums[1], nums[2]
            if len(nums) > 3:
                warnings.append(f"{mes} | {concepto}: {len(nums)} numeros, uso los 3 primeros {nums}")
        elif len(nums) == 2:
            if variacion is not None and abs(variacion - 100.0) < 0.05 and nums[0] == nums[1]:
                # presupuesto en blanco, gasto=diferencia (ej: Otos Gastos 2025-03)
                p, g, d = 0.0, nums[0], nums[1]
            elif nums[1] < 0:
                # gasto en blanco: (presupuesto, diferencia)
                p, d = nums
                g = p + d
                if g < -0.001:
                    warnings.append(f"{mes} | {concepto}: gasto implicito negativo {g}")
                g = max(g, 0.0)
            else:
                p, g = nums
                d = g - p
        elif len(nums) == 1:
            p, g, d = nums[0], 0.0, -nums[0]
        else:
            warnings.append(f"{mes} | {concepto}: fila sin numeros, descartada: {l!r}")
            continue

        # consistencia Diferencia = Gasto - Presupuesto
        if abs((g - p) - d) > 0.01:
            warnings.append(f"{mes} | {concepto}: inconsistencia D={d} vs G-P={g - p}")

        key = concepto.strip().lower()
        if key in mapeo:
            categoria = mapeo[key]
        else:
            categoria = "Otros"
            sin_mapeo.setdefault(concepto, []).append(mes)

        filas.append({
            "mes": mes,
            "concepto_original": concepto,
            "categoria": categoria,
            "tipo": tipo,
            "presupuesto": f"{p:.2f}",
            "gasto_real_registrado": f"{g:.2f}",
            "comentario": comentario,
        })

    return filas, total_row


def main():
    mapeo = {}
    with open(MAPEO_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mapeo[r["original"].strip().lower()] = r["categoria"].strip()

    sin_mapeo = {}
    warnings = []
    todas = []
    controles = []

    archivos = sorted(RAW_DIR.glob("*.txt"))
    if len(archivos) != 19:
        warnings.append(f"Se esperaban 19 archivos crudos, hay {len(archivos)}")

    for path in archivos:
        filas, total_row = parse_mes(path, mapeo, sin_mapeo, warnings)
        todas.extend(filas)
        sum_p = sum(float(r["presupuesto"]) for r in filas)
        sum_g = sum(float(r["gasto_real_registrado"]) for r in filas)
        controles.append((path.stem, len(filas), sum_p, sum_g, total_row))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "mes", "concepto_original", "categoria", "tipo",
            "presupuesto", "gasto_real_registrado", "comentario"])
        w.writeheader()
        w.writerows(todas)

    # ---------- reporte ----------
    out = io.StringIO()
    out.write(f"Filas totales: {len(todas)}\n")
    out.write(f"Total presupuesto: {sum(float(r['presupuesto']) for r in todas):.2f}\n")
    out.write(f"Total gasto_real_registrado: {sum(float(r['gasto_real_registrado']) for r in todas):.2f}\n\n")
    out.write("mes       filas  sum_ppto    sum_gasto   total_hoja_p total_hoja_g  ok_hoja  esperado_p esperado_g ok_esp\n")
    for mes, n, sp, sg, tr in controles:
        tp, tg = (tr if tr else (float("nan"), float("nan")))
        ok_hoja = "SI" if tr and abs(sp - tp) < 0.01 and abs(sg - tg) < 0.01 else "NO"
        if mes in ESPERADOS:
            ep, eg = ESPERADOS[mes]
            ok_esp = "SI" if abs(sp - ep) < 0.01 and abs(sg - eg) < 0.01 else "NO"
            out.write(f"{mes}  {n:5d}  {sp:10.2f}  {sg:10.2f}  {tp:11.2f}  {tg:11.2f}   {ok_hoja}     {ep:9d}  {eg:9d}  {ok_esp}\n")
        else:
            out.write(f"{mes}  {n:5d}  {sp:10.2f}  {sg:10.2f}  {tp:11.2f}  {tg:11.2f}   {ok_hoja}\n")

    out.write("\nConceptos sin mapeo (-> Otros):\n")
    if sin_mapeo:
        for c, meses in sorted(sin_mapeo.items()):
            out.write(f"  {c!r}: {meses}\n")
    else:
        out.write("  (ninguno)\n")

    out.write("\nWarnings:\n")
    if warnings:
        for wmsg in warnings:
            out.write(f"  {wmsg}\n")
    else:
        out.write("  (ninguno)\n")

    print(out.getvalue())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
