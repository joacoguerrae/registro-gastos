# -*- coding: utf-8 -*-
"""
Fase 3 — Test del MOTOR DE MATCHING de conciliar_estado.py (docs/fase3.md #2).

Genera un estado SINTETICO de la Santander Platinum (tarjeta 6427) para el
periodo 2026-07, a partir de las filas reales de esa cuenta en ese mes que ya
estan en data/ledger.csv (SOLO LECTURA), y corre scripts/conciliar_estado.conciliar()
contra una COPIA en memoria del ledger.

Diseno del estado sintetico:
  - 6 filas reales de gasto (Santander Platinum, jul-2026) hoy en el ledger.
  - Se usan 5 de esas 6 en el estado (2 en USD con un TC "real" != al estimado,
    para probar la correccion de monto_uyu); la 6ta se deja AFUERA del estado
    a proposito -> debe generar pendiente sin_respaldo_estado.
  - +2 lineas inventadas que el ledger no tiene (ej. debito automatico,
    comision) -> deben insertarse con origen=estado_cuenta.

Verifica:
  1. las 5 filas incluidas en el estado matchean y quedan estado=conciliado
     (en la copia de prueba).
  2. las 2 lineas inventadas se insertan con origen=estado_cuenta.
  3. la fila dejada afuera genera pendiente sin_respaldo_estado.
  4. el cierre al peso (por moneda) da diferencia 0.
  5. data/ledger.csv en disco queda IDENTICO a como estaba antes de correr
     (este test nunca escribe datos reales).

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/test_conciliacion.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conciliar_estado as ce

CUENTA = "Santander Platinum"
PERIODO = "2026-07"
TC_REAL_USD = 41.10   # distinto del tc estimado (40.33, config/parametros.json) a proposito


def cargar_filas_reales():
    ledger_disco = ce.leer_csv(ce.LEDGER, ce.COLUMNS)
    filas = [r for r in ledger_disco if r["cuenta"] == CUENTA and r["mes"] == PERIODO
             and r["tipo"] == "gasto"]
    assert len(filas) == 6, (
        f"se esperaban 6 filas de gasto de {CUENTA} en {PERIODO}, se encontraron {len(filas)} "
        "-- el ledger cambio; ajustar el test o revisar data/ledger.csv")
    return ledger_disco, filas


def construir_estado_sintetico(filas):
    reales = sorted(filas, key=lambda r: r["fecha"])
    en_estado, dejada_afuera = reales[:5], reales[5]

    lineas = []
    for r in en_estado:
        linea = dict(fecha=r["fecha"], descripcion=r["comercio"] or r["descripcion"],
                     monto=float(r["monto"]), moneda=r["moneda"])
        if r["moneda"] != "UYU":
            linea["monto_uyu"] = round(linea["monto"] * TC_REAL_USD, 2)
        lineas.append(linea)

    inventadas = [
        dict(fecha="2026-07-15", descripcion="DEBITO AUTOMATICO SEGURO HOGAR",
             monto=450.00, moneda="UYU"),
        dict(fecha="2026-07-22", descripcion="COMISION MANTENIMIENTO ANUAL",
             monto=150.00, moneda="UYU"),
    ]
    lineas += inventadas

    total = {}
    for m in ("UYU", "USD", "EUR"):
        s = round(sum(l["monto"] for l in lineas if l["moneda"] == m), 2)
        if s:
            total[m] = s

    estado = dict(emisor="SANTANDER_MC_SINTETICO_TEST", cuenta=CUENTA, periodo=PERIODO,
                  vencimiento="2026-08-05", lineas=lineas, total=total)
    return estado, dejada_afuera, inventadas


def main():
    ledger_real_antes, filas_reales = cargar_filas_reales()
    estado, dejada_afuera, inventadas = construir_estado_sintetico(filas_reales)
    ids_en_estado = {r["id"] for r in filas_reales if r["id"] != dejada_afuera["id"]}

    # COPIA de prueba: deepcopy del ledger completo. El motor muta filas in
    # place (estado, monto_uyu) -- nunca debe tocar ledger_real_antes/disco.
    ledger_copia = copy.deepcopy(ledger_real_antes)
    reglas = ce.cargar_reglas()

    resultado = ce.conciliar(estado, ledger_copia, reglas)

    errores = []

    # 1. matches -> conciliado
    ids_matcheados = {fila["id"] for _linea, fila, _score in resultado["matches"]}
    if ids_matcheados != ids_en_estado:
        errores.append(f"matches esperados {ids_en_estado}, obtenidos {ids_matcheados}")
    for r in ledger_copia:
        if r["id"] in ids_en_estado and r["estado"] != "conciliado":
            errores.append(f"fila {r['id']} debia quedar 'conciliado', quedo '{r['estado']}'")

    # 2. insertadas origen=estado_cuenta
    if len(resultado["nuevas"]) != 2:
        errores.append(f"se esperaban 2 filas nuevas, se obtuvieron {len(resultado['nuevas'])}")
    if not all(r["origen"] == "estado_cuenta" for r in resultado["nuevas"]):
        errores.append("alguna fila nueva no tiene origen=estado_cuenta")

    # 3. sin_respaldo_estado
    ids_sin_respaldo = {r["id"] for r in resultado["sin_respaldo"]}
    if ids_sin_respaldo != {dejada_afuera["id"]}:
        errores.append(f"sin_respaldo esperado {{{dejada_afuera['id']}}}, obtenido {ids_sin_respaldo}")
    pend_sr = ce.pendientes_de_resultado(resultado, CUENTA, PERIODO, "2026-07-12")
    motivos_sr = [p["motivo"] for p in pend_sr if p["id_ledger"] == dejada_afuera["id"]]
    if "sin_respaldo_estado" not in motivos_sr:
        errores.append(f"no se genero pendiente sin_respaldo_estado para {dejada_afuera['id']}")

    # 4. cierre al peso == 0 por moneda
    for moneda, c in resultado["cierre"].items():
        if abs(c["diferencia"]) > 0.01:
            errores.append(f"cierre {moneda}: diferencia {c['diferencia']} (esperada 0)")

    # 4b. correcciones de TC: las 2 lineas USD deben corregirse (tc real != estimado)
    ids_usd = {r["id"] for r in filas_reales if r["moneda"] == "USD"}
    ids_corregidos = {t[0] for t in resultado["correcciones_tc"]}
    if ids_corregidos != (ids_usd & ids_en_estado):
        errores.append(f"correcciones TC esperadas para {ids_usd & ids_en_estado}, "
                       f"obtenidas para {ids_corregidos}")

    # 5. el ledger real en disco no se toco
    ledger_real_despues = ce.leer_csv(ce.LEDGER, ce.COLUMNS)
    if ledger_real_despues != ledger_real_antes:
        errores.append("ALERTA: data/ledger.csv fue modificado por este test")

    # ---------------------------------------------------------------- reporte
    print(f"filas reales {CUENTA} {PERIODO}: {len(filas_reales)}")
    print(f"estado sintetico: {len(estado['lineas'])} lineas "
          f"({len(estado['lineas']) - len(inventadas)} reales + {len(inventadas)} inventadas); "
          f"fila dejada afuera: {dejada_afuera['id']} ({dejada_afuera['fecha']} "
          f"{dejada_afuera['moneda']} {dejada_afuera['monto']} {dejada_afuera['comercio']})")

    print(f"\nmatcheadas -> conciliado: {len(resultado['matches'])} (esperado 5)")
    for _linea, fila, score in sorted(resultado["matches"], key=lambda m: m[1]["fecha"]):
        print(f"  {fila['id']} {fila['fecha']} {fila['moneda']:>3} {fila['monto']:>10} "
              f"'{(fila['descripcion'] or fila['comercio'])}' estado={fila['estado']} score={score:.2f}")

    print(f"\ninsertadas (origen=estado_cuenta): {len(resultado['nuevas'])} (esperado 2)")
    for r in resultado["nuevas"]:
        print(f"  {r['id']} {r['fecha']} {r['moneda']} {r['monto']} '{r['descripcion']}' "
              f"-> ambito={r['ambito']} categoria={r['categoria'] or '(sin categoria)'} estado={r['estado']}")

    print(f"\nsin_respaldo_estado: {len(resultado['sin_respaldo'])} (esperado 1)")
    for p in pend_sr:
        if p["motivo"] == "sin_respaldo_estado":
            print(f"  {p['id_ledger']}: {p['detalle']}")

    print(f"\ncorrecciones de TC (monto_uyu real vs estimado): {len(resultado['correcciones_tc'])}")
    for id_ledger, nuevo, motivo in resultado["correcciones_tc"]:
        print(f"  {id_ledger} -> {nuevo} UYU | {motivo}")

    print("\ncierre al peso (calculado vs total del estado, por moneda):")
    for moneda, c in sorted(resultado["cierre"].items()):
        print(f"  {moneda}: calculado {c['calculado']:.2f} | estado {c['estado']:.2f} | "
              f"diferencia {c['diferencia']:.2f}")

    print(f"\ndata/ledger.csv real intacto: {'SI' if ledger_real_despues == ledger_real_antes else 'NO -- VER ALERTA ARRIBA'}")

    if errores:
        print("\nFALLOS:")
        for e in errores:
            print(f"  - {e}")
        raise SystemExit(1)
    print("\nTODO OK: motor de matching validado contra el estado sintetico.")


if __name__ == "__main__":
    main()
