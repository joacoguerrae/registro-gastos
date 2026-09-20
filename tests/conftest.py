# -*- coding: utf-8 -*-
"""Fixtures del suite. Regla de oro: NINGUN test escribe en el repo.

Cada test corre contra una COPIA de data/ + config/ + out/ en tmp_path, y los
scripts la encuentran via GASTOS_BASE (todos lo respetan: la linea
`BASE = os.environ.get("GASTOS_BASE") or ...` al inicio de cada script).
"""
import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

# Permanente y a nivel de modulo: los tests que IMPORTAN un script (en vez de
# correrlo) lo necesitan en el path. NO hacerlo con monkeypatch.syspath_prepend
# dentro de un fixture: al deshacerlo, el importlib.reload de teardown explota
# con ModuleNotFoundError.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

COPIAR = ("data", "config", "out")

# Nunca se copian: los secretos (en CI no existen) ni data/raw (7,4 MB que solo
# usa merge_y_validar --migracion, que no esta en el suite).
NO_COPIAR = {"secrets.local.json", "gcp_service_account.json",
             "gmail_token.json", "oauth_client.json",
             "raw", "__pycache__"}


def _ignorar(dirpath, nombres):
    return [n for n in nombres if n in NO_COPIAR]


def _copiar(destino):
    for d in COPIAR:
        if (REPO / d).exists():
            shutil.copytree(REPO / d, destino / d, ignore=_ignorar)
    return destino


@pytest.fixture(scope="session")
def base_congelada(tmp_path_factory):
    """Copia unica de la sesion, para tests que SOLO LEEN (~4,5 MB, 0,4 s)."""
    return _copiar(tmp_path_factory.mktemp("congelada"))


@pytest.fixture
def base(tmp_path):
    """Copia fresca por test: es la que apunta GASTOS_BASE cuando se corre algo."""
    return _copiar(tmp_path)


@pytest.fixture
def correr(base):
    """Corre un script del pipeline en SUBPROCESO con GASTOS_BASE=base.

    Subproceso y no import: los scripts calculan BASE en tiempo de import, asi
    que importar dos veces con GASTOS_BASE distinto devuelve rutas viejas.
    PYTHONIOENCODING: los scripts imprimen acentos y la consola default de
    Windows (cp1252) los revienta.
    """
    def _correr(script, *args, espera_exito=True):
        env = dict(os.environ, GASTOS_BASE=str(base), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        if espera_exito and p.returncode != 0:
            raise AssertionError(
                f"{script} {' '.join(args)} salio {p.returncode}\n"
                f"--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}")
        return p
    return _correr


# ---------------------------------------------------------------- helpers CSV
def filas(path):
    """CSV -> lista de dicts.

    NUNCA comparar BYTES de un CSV. csv.writer escribe CRLF en toda plataforma y
    .gitattributes normaliza a LF en el repo: los bytes del working tree dependen
    de si el archivo lo escribio git (checkout, LF) o Python (CRLF). Verificado
    el 19/9: convirtiendo todos los CSV de una copia a LF y corriendo
    sync_splitwise, las FILAS quedan iguales y los bytes no. Ademas la fase 0.4
    cambia el lineterminator a "\\n" y romperia cualquier test de bytes.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def por_id(path, origen=None):
    rs = filas(path)
    if origen:
        rs = [r for r in rs if r["origen"] == origen]
    return {r["id"]: r for r in rs}


def diff_filas(a, b, ignorar=()):
    """-> (solo_en_a, solo_en_b, {campo: [(id, valor_a, valor_b), ...]})"""
    ig = set(ignorar)
    campos = {}
    for i in sorted(set(a) & set(b)):
        for c in a[i]:
            if c not in ig and a[i][c] != b[i][c]:
                campos.setdefault(c, []).append((i, a[i][c], b[i][c]))
    return sorted(set(a) - set(b)), sorted(set(b) - set(a)), campos


def escribir_ledger(path, rows):
    """Reescribe un ledger con csv.DictWriter.

    NO usar Set-Content ni modo texto: `-Encoding utf8` de PowerShell 5.1 mete
    BOM y merge_y_validar aborta en :106 con AssertionError de columnas, o sea
    el test "pasa" por la razon equivocada.
    """
    cols = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
