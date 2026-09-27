# -*- coding: utf-8 -*-
"""
Reconstruye data/ledger.csv DESDE CERO a partir de sus fuentes (Fase 1.1).

Es el procedimiento para cuando el ledger se perdio, se trunco o se corrompio
(el gate `1.conteo_origen` de merge_y_validar lo avisa con DESVIO). Tambien es
la prueba de que el ledger ya es un artefacto de build: el test
tests/test_fuentes.py::test_ledger_se_reconstruye_desde_cero lo corre sobre una
copia y exige llegar a las mismas filas.

Pasos (en este orden, y el orden importa):
  1. merge_y_validar.py       -> staging + data/fuentes/. Sale != 0 A PROPOSITO:
                                 todavia no hay filas mail y el gate lo marca.
                                 Tiene que ir ANTES que el paso 2 porque
                                 procesar_mails deduplica sus transferencias
                                 contra las filas 1money ya presentes.
  2. procesar_mails.py --reprocesar -> las filas mail desde data/inbox_raw/,
                                 con las resoluciones re-aplicadas.
  3. leer_carga_manual.py --offline -> la planilla desde su snapshot.
  4. merge_y_validar.py       -> tiene que salir 0.

Sale IDENTICO, `estado` incluido: la marca `conciliado` de las filas mail (que
escriben los conciliadores y --reprocesar no puede reproducir) se guarda en
data/fuentes/conciliados.csv y merge la restaura.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/reconstruir_ledger.py [--forzar]

Sin --forzar se niega a correr si data/ledger.csv existe y tiene filas mail
(no hay nada que reconstruir: es probable que quieras otra cosa).
"""
import argparse
import csv
import os
import subprocess
import sys

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(BASE, "data", "ledger.csv")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def correr(script, *args):
    print(f"\n=== {script} {' '.join(args)} ===", flush=True)
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args]).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forzar", action="store_true",
                    help="reconstruir aunque el ledger actual parezca sano")
    args = ap.parse_args()

    if os.path.exists(LEDGER) and not args.forzar:
        with open(LEDGER, encoding="utf-8-sig", newline="") as f:
            n_mail = sum(1 for r in csv.DictReader(f) if r.get("origen") == "mail")
        if n_mail:
            print(f"data/ledger.csv existe y tiene {n_mail} filas mail: no parece "
                  "roto. Si igual queres reconstruirlo, corre con --forzar.")
            return 2

    correr("merge_y_validar.py")          # exit != 0 esperado: faltan las mail
    if correr("procesar_mails.py", "--reprocesar") != 0:
        print("\nFALLO: procesar_mails --reprocesar no termino bien; el ledger quedo "
              "a medio reconstruir. No commitees.")
        return 1
    # La planilla desde su snapshot commiteado (sin red). DESPUES de los mails:
    # su chequeo de duplicados compara contra el ledger, y sin las filas mail
    # dejaria entrar como nuevas cosas que ya estaban cargadas.
    if correr("leer_carga_manual.py", "--offline") != 0:
        print("\nFALLO: leer_carga_manual --offline no termino bien. No commitees.")
        return 1
    rc = correr("merge_y_validar.py")
    print("\n" + ("ledger reconstruido y validado." if rc == 0 else
                  "FALLO: la validacion final no dio OK -- revisar arriba antes de commitear."))
    return rc


if __name__ == "__main__":
    sys.exit(main())
