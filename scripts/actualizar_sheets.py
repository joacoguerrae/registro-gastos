# -*- coding: utf-8 -*-
"""
Fase 2.5 -- Empuja los CSV del dashboard a los Google Sheets EXISTENTES
(por su ID, sin recrearlos, para no romper la conexion de Looker Studio).

Autenticacion: cuenta de servicio en config/gcp_service_account.json
(gitignored). Los 3 Sheets deben estar compartidos con el client_email de
esa cuenta como Editor.

Escribe los valores como numeros reales (no texto) para que Looker los lea
bien: cada celda que parsea como float se manda como numero; el resto, texto.

Ejecutar:
  C:/Users/joaco/anaconda3/python.exe scripts/actualizar_sheets.py
Tolerante a fallas: si no hay red o falta permiso, sale con codigo != 0 pero
imprime un diagnostico claro (el pipeline lo trata como paso opcional).
"""
import csv
import os
import sys
import json

import gspread
from google.oauth2.service_account import Credentials

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

CREDS = P("config", "gcp_service_account.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# CSV local  ->  ID del Google Sheet existente
DESTINOS = [
    ("out/resumen_mensual.csv",     "10Tfth6Z7V0_W4WzVQJ-GCqro8HeSuV1y8Aj0nob4MDo"),
    ("out/presupuesto_vs_real.csv", "1c-kDv0qQAdkQg1Qg_WFtg6qM2qgKG3pXlmIg941z1qo"),
    ("out/casa_mensual.csv",        "15YU1L7MFnqbYCkkP8IT0o2e8v7yTPJu8ADvbxHS6cPA"),
]


def celda(v):
    """Convierte a numero si parsea limpio; si no, deja el texto tal cual.
    El header y las columnas de texto (mes, categoria) quedan como string."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def cargar(path):
    with open(P(*path.split("/")), encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header, *body = rows
    return [header] + [[celda(c) for c in fila] for fila in body]


def main():
    # Env var (GitHub Actions) tiene prioridad; si no, archivo local (corridas en la PC).
    sa_json = os.environ.get("GCP_SA_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        if not os.path.exists(CREDS):
            print(f"ERROR: falta {CREDS} (cuenta de servicio). Ver docs/fase3.md / README.",
                  file=sys.stderr)
            return 2
        creds = Credentials.from_service_account_file(CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)

    errores = 0
    for csv_rel, sheet_id in DESTINOS:
        datos = cargar(csv_rel)
        nombre = csv_rel.split("/")[-1]
        try:
            sh = gc.open_by_key(sheet_id)
            ws = sh.sheet1
            ws.clear()
            # clear() borra valores pero NO el formato: si el Sheet tenia celdas
            # con formato de fecha (de cuando se creo desde CSV), un numero como
            # 6088.5 se mostraria como una fecha (~1916) y Looker lo leeria mal.
            # Limpiamos el numberFormat de toda la hoja -> vuelve a "automatico".
            sh.batch_update({"requests": [{
                "repeatCell": {
                    "range": {"sheetId": ws.id},
                    "cell": {"userEnteredFormat": {}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }]})
            ws.update(datos, value_input_option="RAW")
            print(f"  OK  {nombre}: {len(datos) - 1} filas -> {sh.title}")
        except gspread.exceptions.APIError as e:
            errores += 1
            code = getattr(e.response, "status_code", "?")
            if code == 403:
                print(f"  FALLO {nombre}: sin permiso (403). ¿Compartiste el Sheet "
                      f"{sheet_id} con la cuenta de servicio como Editor?", file=sys.stderr)
            else:
                print(f"  FALLO {nombre}: APIError {code}: {e}", file=sys.stderr)
        except Exception as e:  # noqa
            errores += 1
            print(f"  FALLO {nombre}: {type(e).__name__}: {e}", file=sys.stderr)

    if errores:
        print(f"{errores}/{len(DESTINOS)} Sheets NO se actualizaron.", file=sys.stderr)
        return 1
    print(f"{len(DESTINOS)} Sheets actualizados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
