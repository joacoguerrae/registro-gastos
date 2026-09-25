# -*- coding: utf-8 -*-
"""Que el ledger roto NO pase la validacion. Gate de la fase 0 (SA1)."""
import pytest
from conftest import escribir_ledger, filas

pytestmark = pytest.mark.smoke


def test_ledger_sano_valida(base, correr):
    p = correr("merge_y_validar.py")
    assert "validacion OK" in p.stdout


# GATE de la fase 0 (SA1). Sin xfail a proposito: el invariante de conteo por
# origen se implementa en ESTA misma fase (0.2), asi que el test exige lo
# correcto desde ahora. Antes de 0.2, merge_y_validar sale 0 con el ledger
# truncado O borrado: reconstruye 4.525 filas desde data/staging/ y pierde en
# silencio 284 mail + 71 estado_cuenta + 47 manual + 32 splitwise (verificado
# 19/9). Si este test falla, el gate NO esta cerrado -- no bajarle la asercion.
@pytest.mark.parametrize("corte", [0.5, 0.0])
def test_ledger_truncado_no_valida(base, correr, corte):
    path = base / "data" / "ledger.csv"
    rows = filas(path)
    # Sembrar PRIMERO con el ledger sano. Sin esto el test pasa por la razon
    # equivocada: con data/staging/ledger_stats.json ausente, la corrida sobre
    # el ledger roto SIEMBRA la referencia (mail=0) y 1.conteo_origen sale INFO;
    # el exit != 0 venia de otro chequeo incidental. Medido el 20/9.
    correr("merge_y_validar.py")
    assert (base / "data" / "staging" / "ledger_stats.json").exists()

    if corte == 0.0:
        path.unlink()
    else:
        escribir_ledger(path, rows[:int(len(rows) * corte)])
    p = correr("merge_y_validar.py", espera_exito=False)
    assert p.returncode != 0, (
        "merge_y_validar acepto un ledger truncado en silencio (SA1)\n" + p.stdout[-2000:])
    assert "1.conteo_origen" in p.stdout and "DESVIO" in p.stdout, (
        "el ledger roto fallo, pero NO por el gate de conteo por origen: el gate "
        "no esta cerrado\n" + p.stdout[-2000:])


def test_no_se_siembra_el_gate_desde_un_ledger_roto(base, correr):
    """Borrar ledger_stats.json no puede desarmar el gate.

    Si se sembrara desde el ledger mutilado, la perdida quedaria bendecida como
    la referencia nueva y el invariante no volveria a saltar nunca.

    Se borra el archivo a proposito para forzar el camino de SIEMBRA: el test
    tiene que valer igual exista o no en el repo. La primera version asumia que
    no existia (era cierto el 20/9, antes de que Actions lo sembrara) y se
    rompio sola el dia que aparecio, probando contra el camino equivocado.
    """
    stats = base / "data" / "staging" / "ledger_stats.json"
    stats.unlink(missing_ok=True)
    rows = filas(base / "data" / "ledger.csv")
    escribir_ledger(base / "data" / "ledger.csv",
                    [r for r in rows if r["origen"] in ("1money", "planilla_casa")])
    p = correr("merge_y_validar.py", espera_exito=False)
    assert p.returncode != 0
    assert "1.conteo_origen" in p.stdout and "DESVIO" in p.stdout, p.stdout[-2000:]
    assert not stats.exists(), \
        "sembro la referencia desde un ledger sin filas no regenerables"


def _primera_no_regenerable(rows, **filtros):
    """merge() reconstruye 1money y planilla_casa desde data/staging/: ensuciar
    una de esas filas se pisa solo y el test pasaria por la razon equivocada."""
    for r in rows:
        if r["origen"] in ("mail", "manual", "estado_cuenta", "splitwise") \
                and all(r[k] == v for k, v in filtros.items()):
            return r
    pytest.fail(f"no hay fila no-regenerable con {filtros}")


def test_ledger_con_id_duplicado_no_valida(base, correr):
    path = base / "data" / "ledger.csv"
    rows = filas(path)
    a = _primera_no_regenerable(rows, origen="manual")
    dup = dict(a)
    dup["id"] = _primera_no_regenerable([r for r in rows if r["id"] != a["id"]],
                                        origen="manual")["id"]
    escribir_ledger(path, rows + [dup])
    p = correr("merge_y_validar.py", espera_exito=False)
    assert p.returncode != 0
    assert "FALLO" in p.stdout


def test_categoria_fuera_de_catalogo_no_valida(base, correr):
    path = base / "data" / "ledger.csv"
    rows = filas(path)
    _primera_no_regenerable(rows, tipo="gasto", en_analisis="1")["categoria"] = "Categoria Inventada"
    escribir_ledger(path, rows)
    p = correr("merge_y_validar.py", espera_exito=False)
    assert p.returncode != 0
