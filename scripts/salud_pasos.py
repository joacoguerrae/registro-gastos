# -*- coding: utf-8 -*-
"""
Salud de los pasos NO CRITICOS del pipeline (auditoria 2026-07 M5).

El problema: Sheets, el dashboard y cierre_casa no deben frenar el sync si
fallan -- un 503 de Google no justifica perder la corrida entera. Pero con
`|| echo "::warning::..."` un paso podia fallar TODOS los dias sin que nadie se
entere: la corrida seguia en verde y la anotacion solo se ve si uno entra a
mirar el log. O sea, los datos dejaban de actualizarse EN SILENCIO -- el mismo
modo de falla que ya nos mordio dos veces (churn de line endings, cierre_casa
con path de Windows).

La politica: fallo aislado -> warning (los errores transitorios existen y se
arreglan solos). El MISMO paso fallando UMBRAL corridas seguidas -> Issue, que
llega por mail. Un solo Issue por racha (no se repite en cada corrida), y se
cierra solo cuando el paso se recupera.

El estado vive en data/staging/salud_pasos.json y se commitea con el resto del
pipeline: sin eso la racha no sobrevive al runner, que es efimero. NO va en
config/estado_sync.json aunque tambien se commitee -- procesar_mails.py lo
reescribe entero en cada corrida y borraria la racha.

Uso:
  salud_pasos.py correr <paso> -- <comando...>   corre, registra, SIEMPRE sale 0
  salud_pasos.py escalar [--dry-run]             abre/cierra Issues segun la racha
  salud_pasos.py estado                          imprime la salud actual
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

SALUD = P("data", "staging", "salud_pasos.json")

# Corridas seguidas fallando a partir de las cuales se abre Issue. Con el cron
# 2x/dia, 3 = ~1.5 dias: suficiente para que un error transitorio de Google se
# resuelva solo sin molestar, poco para que los datos queden viejos sin aviso.
UMBRAL = 3

# Que implica que el paso falle. Va en el warning y en el cuerpo del Issue: sin
# esto, "actualizar_sheets fallo" no le dice a nadie que Looker quedo viejo.
QUE_IMPLICA = {
    "sync_splitwise": "el pipeline sigue sin Splitwise: los gastos compartidos no se actualizan",
    "cierre_casa": "la posicion de casa queda con el valor previo (el dashboard la muestra vieja)",
    "generar_dashboard_html": "out/dashboard.html queda con los datos de la corrida anterior",
    "actualizar_sheets": "los Google Sheets y Looker quedan con los datos de la corrida anterior",
    "deploy_dashboard": "la URL del dashboard (Cloudflare Pages) queda con la version anterior",
}


def ahora():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def leer():
    if not os.path.exists(SALUD):
        return {}
    with open(SALUD, encoding="utf-8") as f:
        return json.load(f)


def escribir(d):
    os.makedirs(os.path.dirname(SALUD), exist_ok=True)
    with open(SALUD, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def registrar(paso, ok, detalle=""):
    """-> el estado del paso despues de registrar la corrida."""
    d = leer()
    e = d.setdefault(paso, {"fallos_seguidos": 0, "issue_abierto": False})
    if ok:
        e["fallos_seguidos"] = 0
        e["ultimo_ok"] = ahora()
        e.pop("detalle", None)
        # issue_abierto NO se toca aca: lo cierra escalar(), que es el unico que
        # tiene GH_TOKEN. Si se reseteara aca, se perderia el numero del Issue y
        # quedaria abierto para siempre.
    else:
        e["fallos_seguidos"] = e.get("fallos_seguidos", 0) + 1
        e["ultimo_fallo"] = ahora()
        e["detalle"] = detalle
    escribir(d)
    return e


def correr(paso, cmd):
    """Corre un paso no critico. Nunca hace fallar la corrida: ese es el punto."""
    r = subprocess.run(cmd)
    e = registrar(paso, r.returncode == 0, f"exit {r.returncode}")
    if r.returncode == 0:
        return 0
    n = e["fallos_seguidos"]
    veces = "1a corrida" if n == 1 else f"{n} corridas seguidas"
    falta = f" Se abre Issue si falla {UMBRAL - n} vez/veces mas." if n < UMBRAL else ""
    print(f"::warning::{paso} fallo (exit {r.returncode}) -- {veces}. "
          f"{QUE_IMPLICA.get(paso, '')}.{falta}")
    return 0


def gh(*args):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return None, "gh no esta instalado"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()
    return r.stdout.strip(), None


def escalar(dry_run=False):
    d = leer()
    if not d:
        print("sin registros de salud todavia")
        return 0

    # 1) cerrar los Issues de pasos que se recuperaron.
    # issue_abierto SIEMPRE se resetea al recuperarse, tengamos o no el numero:
    # si quedara pegado en True, el paso no volveria a escalar nunca (la racha
    # siguiente encontraria "ya tiene Issue") y se apagaria en silencio -- justo
    # el modo de falla que este script existe para eliminar.
    for paso, e in sorted(d.items()):
        if e.get("fallos_seguidos", 0) != 0 or not e.get("issue_abierto"):
            continue
        num = e.get("issue_num")
        if not num:
            print(f"::warning::{paso} se recupero pero no se guardo el numero de su Issue: "
                  "cerralo a mano. El escalado sigue funcionando.")
        elif dry_run:
            print(f"[dry-run] cerraria el Issue #{num} ({paso} se recupero)")
        else:
            _, err = gh("issue", "close", str(num), "-c",
                        f"`{paso}` volvio a funcionar el {e.get('ultimo_ok', '?')}: "
                        "el sync lo cierra solo.")
            if err:
                print(f"::warning::no se pudo cerrar el Issue #{num} de {paso}: {err}")
                continue          # se reintenta la proxima corrida
            print(f"Issue #{num} cerrado: {paso} se recupero")
        e["issue_abierto"] = False
        e.pop("issue_num", None)
        escribir(d)

    # 2) abrir Issue de los pasos en racha que todavia no tienen uno
    pend = [(p, e) for p, e in sorted(d.items())
            if e.get("fallos_seguidos", 0) >= UMBRAL and not e.get("issue_abierto")]
    if not pend:
        bajo = [f"{p} ({e['fallos_seguidos']}/{UMBRAL})" for p, e in sorted(d.items())
                if 0 < e.get("fallos_seguidos", 0) < UMBRAL]
        ya = [f"{p} ({e['fallos_seguidos']})" for p, e in sorted(d.items())
              if e.get("fallos_seguidos", 0) >= UMBRAL and e.get("issue_abierto")]
        partes = []
        if ya:
            partes.append(f"con Issue ya abierto: {', '.join(ya)}")
        if bajo:
            partes.append(f"en racha pero bajo el umbral: {', '.join(bajo)}")
        print("nada para escalar" + (f"; {'; '.join(partes)}" if partes
                                     else " (todos los pasos OK)"))
        return 0

    for paso, e in pend:
        n = e["fallos_seguidos"]
        titulo = f"El paso '{paso}' del sync falla hace {n} corridas"
        cuerpo = (
            f"**{paso}** viene fallando **{n} corridas seguidas**.\n\n"
            f"- **Qué implica:** {QUE_IMPLICA.get(paso, 'ver .github/workflows/sync.yml')}.\n"
            f"- **Último error:** `{e.get('detalle', '?')}`\n"
            f"- **Última vez que anduvo:** {e.get('ultimo_ok') or 'sin registro'}\n"
            f"- **Último fallo:** {e.get('ultimo_fallo', '?')}\n\n"
            f"El sync **sigue corriendo y commiteando**: este paso no es crítico, así que "
            f"no frena el pipeline. Pero los datos que produce quedan viejos.\n\n"
            f"Para diagnosticar: Actions → Sync gastos → última corrida → paso `{paso}`.\n\n"
            f"_Este Issue lo abre `scripts/salud_pasos.py` y se cierra solo cuando el paso "
            f"vuelva a funcionar._"
        )
        if dry_run:
            print(f"[dry-run] abriria Issue: {titulo}")
            e["issue_abierto"] = True
            e["issue_num"] = -1          # sentinela truthy: los reales arrancan en 1
            escribir(d)
            continue
        out, err = gh("issue", "create", "--title", titulo, "--body", cuerpo)
        if err:
            # No se marca: se reintenta en la proxima corrida. Marcar sin Issue
            # creado seria peor -- el aviso se perderia para siempre.
            print(f"::warning::no se pudo crear el Issue de {paso}: {err}")
            continue
        num = (out or "").rstrip("/").split("/")[-1]
        print(f"Issue creado para {paso}: {out}")
        e["issue_abierto"] = True
        if num.isdigit():
            e["issue_num"] = int(num)
        escribir(d)   # de a uno: si el proximo falla, este no se pierde
    return 0


def estado():
    d = leer()
    if not d:
        print("sin registros de salud todavia")
        return 0
    print(f"{'paso':24s} {'racha':>6s}  {'issue':>6s}  ultimo ok")
    for paso, e in sorted(d.items()):
        n = e.get("fallos_seguidos", 0)
        marca = "OK" if n == 0 else f"FALLA x{n}"
        iss = f"#{e['issue_num']}" if e.get("issue_num") else ("si" if e.get("issue_abierto") else "-")
        print(f"{paso:24s} {marca:>6s}  {iss:>6s}  {e.get('ultimo_ok', '-')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="salud de los pasos no criticos del pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_correr = sub.add_parser("correr", help="corre un paso no critico y registra el resultado")
    p_correr.add_argument("paso")
    p_correr.add_argument("comando", nargs=argparse.REMAINDER)

    p_esc = sub.add_parser("escalar", help="abre/cierra Issues segun la racha")
    p_esc.add_argument("--dry-run", action="store_true")

    sub.add_parser("estado", help="imprime la salud actual")

    args = ap.parse_args()
    if args.cmd == "correr":
        cmd = args.comando
        if cmd and cmd[0] == "--":      # separador opcional
            cmd = cmd[1:]
        if not cmd:
            ap.error("falta el comando: salud_pasos.py correr <paso> -- <comando...>")
        return correr(args.paso, cmd)
    if args.cmd == "escalar":
        return escalar(args.dry_run)
    return estado()


if __name__ == "__main__":
    sys.exit(main())
