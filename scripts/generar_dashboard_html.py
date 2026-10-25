"""Genera out/dashboard.html: dashboard self-contained (un archivo, sin dependencias
externas) del sistema de gastos. Lee los agregados que ya produce el pipeline y el
ledger, y emite HTML+CSS+JS inline. Se regenera en cada corrida -> siempre al dia.

Pestanas: Categorias (vista personal), Presupuesto vs real, Casa (con Lu y aportes).
Filtros: multi-seleccion de meses (default mes corriente), de categorias (para ver
evolucion de categorias puntuales) y de tipo Fijo/Variable (config/categorias.csv).
Saldos estimados de cuentas (ultimo saldo real por cuenta + movimientos), inversiones
(planilla Carga manual + giros), posicion de casa. Logos embebidos desde
assets/logos/. Estilo cozy/calido, claro y oscuro.
"""
import base64
import calendar
import csv
import json
import os
import datetime as dt
from collections import defaultdict

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(*a): return os.path.join(BASE, *a)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGOS_DIR = os.path.join(REPO, "assets", "logos")   # codigo, no datos: no sigue a GASTOS_BASE
LOGO_MAX_BYTES = 60_000

# (cuenta del ledger, moneda, etiqueta, grupo, nivel, logo)
BANCOS = [
    ("Itaú UYU",      "UYU", "Itaú $",        "personal", "principal",  "itau"),
    ("Itaú USD",      "USD", "Itaú USD",      "personal", "principal",  "itau"),
    ("Cuenta Casa",   "UYU", "Cuenta Casa",   "casa",     "principal",  "casa"),
    ("BBVA USD",      "USD", "BBVA USD",      "personal", "secundaria", "bbva"),
    ("Santander UYU", "UYU", "Santander $",   "personal", "secundaria", "santander"),
    ("Santander USD", "USD", "Santander USD", "personal", "secundaria", "santander"),
]
# (cuenta, moneda, etiqueta, logo). El saldo sale de la pestana Saldos de la
# planilla Carga manual (data/staging/saldos_carga.csv) + giros posteriores.
INVERSIONES = [
    ("Gletir",              "USD", "Gletir",              "gletir"),
    ("Interactive Brokers", "USD", "Interactive Brokers", "ibkr"),
]


def leer(path, **kw):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, **kw))


def leer_opt(path):
    return leer(path) if os.path.exists(path) else []


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def logo_uri(clave):
    """data URI del logo (assets/logos/<clave>.svg|.png), o None si no hay archivo.
    El dashboard es un unico HTML sin recursos externos: el logo va embebido."""
    for ext, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        path = os.path.join(LOGOS_DIR, clave + ext)
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > LOGO_MAX_BYTES:
                raise SystemExit(f"{path}: {len(raw)} bytes, el maximo es {LOGO_MAX_BYTES}. "
                                 "Achicalo (SVG o PNG chico) para no inflar el dashboard.")
            return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return None


def tipos_categoria():
    """categoria -> Fijo|Variable segun config/categorias.csv (las de Ingreso no van)."""
    return {r["categoria"]: r["tipo"] for r in leer_opt(P("config", "categorias.csv"))
            if r["tipo"] in ("Fijo", "Variable")}


def bases_saldo():
    """(cuenta, moneda) -> (fecha_corte, saldo) del ultimo saldo REAL conocido.

    Manda la fila mas nueva entre data/saldos.csv (conciliaciones, solo las que
    tienen saldo_real) y config/saldos_iniciales.csv (semillas). Antes se usaba
    una fecha fija (12/7) para todas las cuentas con semillas al 31/8: los
    movimientos del medio se contaban dos veces."""
    base = {}

    def tomar(cuenta, moneda, fecha, saldo):
        k = (cuenta, moneda)
        if fecha and (k not in base or fecha >= base[k][0]):
            base[k] = (fecha, saldo)

    for r in leer_opt(P("config", "saldos_iniciales.csv")):
        tomar(r["cuenta"], r["moneda"], r["fecha_corte"], num(r["saldo"]))
    for r in leer_opt(P("data", "saldos.csv")):
        if (r.get("saldo_real") or "").strip():
            tomar(r["cuenta"], r["moneda"], r["fecha_corte"], num(r["saldo_real"]))
    return base


def calcular_saldos(ledger):
    base = bases_saldo()
    out = []
    for (c, mon, et, g, nivel, logo) in BANCOS:
        fecha, saldo = base.get((c, mon), ("", 0.0))
        for r in ledger:
            if r["fecha"] <= fecha or r["moneda"] != mon:
                continue
            mto = num(r["monto"])
            if r["tipo"] == "gasto" and r["cuenta"] == c:
                saldo -= mto
            elif r["tipo"] == "ingreso" and r["cuenta"] == c:
                saldo += mto
            elif r["tipo"] == "transferencia":
                if r["cuenta"] == c:
                    saldo -= mto
                if r["cuenta_destino"] == c:
                    saldo += mto
        out.append({"cuenta": et, "moneda": mon, "saldo": round(saldo, 2), "grupo": g,
                    "nivel": nivel, "base_fecha": fecha, "logo": logo_uri(logo)})
    return out


def calcular_inversiones(ledger):
    """Ultimo saldo cargado en la planilla + giros posteriores (entradas - retiros).
    No mira en_analisis: esas transferencias van en 0 a proposito (plata propia)."""
    ultimo = {}
    for r in leer_opt(P("data", "staging", "saldos_carga.csv")):
        k = (r["cuenta"], r["moneda"])
        if k not in ultimo or r["fecha"] >= ultimo[k][0]:
            ultimo[k] = (r["fecha"], num(r["saldo"]))
    out = []
    for (c, mon, et, logo) in INVERSIONES:
        if (c, mon) not in ultimo:
            out.append({"cuenta": et, "moneda": mon, "saldo": None, "base_fecha": "",
                        "aportes_desde": 0.0, "logo": logo_uri(logo)})
            continue
        fecha, saldo = ultimo[(c, mon)]
        aportes = 0.0
        for r in ledger:
            if r["fecha"] <= fecha or r["moneda"] != mon or r["tipo"] != "transferencia":
                continue
            if r["cuenta_destino"] == c:
                aportes += num(r["monto"])
            if r["cuenta"] == c:
                aportes -= num(r["monto"])
        out.append({"cuenta": et, "moneda": mon, "saldo": round(saldo + aportes, 2),
                    "base_fecha": fecha, "aportes_desde": round(aportes, 2), "logo": logo_uri(logo)})
    return out


def armar_data():
    """Todo lo que el dashboard necesita, como dict serializable. Puro: no escribe."""
    personal = defaultdict(list)
    for r in leer(P("out", "resumen_mensual.csv")):
        personal[r["mes"]].append({"cat": r["categoria"], "gasto": round(num(r["gasto_uyu"]), 2)})

    presup = defaultdict(list)
    for r in leer(P("out", "presupuesto_vs_real.csv")):
        presup[r["mes"]].append({
            "cat": r["categoria"], "ppto": round(num(r["presupuesto"]), 2),
            "real": round(num(r["gasto_real"]), 2), "dif": round(num(r["diferencia"]), 2)})

    casa = defaultdict(lambda: {"total": 0.0, "lu": 0.0, "joaco": 0.0, "cats": []})
    for r in leer(P("out", "casa_mensual.csv")):
        m = casa[r["mes"]]
        gt, gl, gj = num(r["gasto_total"]), num(r["gasto_lu"]), num(r["gasto_joaco"])
        m["total"] += gt; m["lu"] += gl; m["joaco"] += gj
        m["cats"].append({"cat": r["categoria"], "total": round(gt, 2),
                          "lu": round(gl, 2), "joaco": round(gj, 2)})
    for m in casa.values():
        m["total"] = round(m["total"], 2); m["lu"] = round(m["lu"], 2); m["joaco"] = round(m["joaco"], 2)
        m["cats"].sort(key=lambda c: -c["total"])

    # cierre de casa por mes: aportes + posicion
    casa_cierre = {}
    for r in leer_opt(P("data", "cierre_casa.csv")):
        casa_cierre[r["mes"]] = {
            "aportes_lu": round(num(r["aportes_lu"]), 2), "aportes_joaco": round(num(r["aportes_joaco"]), 2),
            "posicion_lu": round(num(r["posicion_lu"]), 2), "posicion_joaco": round(num(r["posicion_joaco"]), 2)}

    ledger = leer(P("data", "ledger.csv"))
    max_fecha = max((r["fecha"] for r in ledger), default="")

    mes_curso = max_fecha[:7] if max_fecha else ""
    dia_corte = int(max_fecha[8:10]) if len(max_fecha) >= 10 else 0
    dias_mes = calendar.monthrange(int(mes_curso[:4]), int(mes_curso[5:7]))[1] if mes_curso else 30

    meses = sorted(set(personal) | set(presup) | set(casa))
    cats = sorted({x["cat"] for m in personal.values() for x in m}
                  | {x["cat"] for m in casa.values() for x in m["cats"]})
    tipos = tipos_categoria()

    return {
        "mes_en_curso": mes_curso, "dia_corte": dia_corte, "dias_mes": dias_mes,
        "meses": meses, "mes_default": meses[-1] if meses else "", "categorias": cats,
        "cat_tipo": {c: tipos.get(c, "Variable") for c in cats},
        "personal": personal, "presupuesto": presup,
        "casa": {k: v for k, v in casa.items()}, "casa_cierre": casa_cierre,
        "saldos": calcular_saldos(ledger), "inversiones": calcular_inversiones(ledger),
        "generado": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def main():
    data = armar_data()
    # "</" escapado: un texto con "</script>" no puede cerrar el bloque JSON
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload)
    out = P("out", "dashboard.html")
    # newline="\n": out/dashboard.html se commitea con eol=lf; sin esto una
    # corrida local lo reescribe entero en CRLF (auditoria 2026-09 SB1).
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"{out}: {len(data['meses'])} meses, {len(data['categorias'])} categorias, "
          f"saldos {len(data['saldos'])} cuentas, {len(data['inversiones'])} inversiones")


TEMPLATE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mis gastos</title>
<style>
:root{
  --ground:#f6f1e9; --surface:#fcfaf5; --surface-2:#f1ebe0; --line:#e6dccb;
  --ink:#2e2621; --ink-2:#6f6357; --ink-3:#9c9081;
  --accent:#c17d34; --accent-soft:#e9d4b3;
  --good:#5e8b57; --bad:#bc4b3c; --good-soft:#dfe8d6; --bad-soft:#f0d9d2;
  --c1:#c8873b; --c2:#3e7c82; --c3:#8e4e63; --c4:#7a9b52; --c5:#5b6e9e;
  --c6:#c1663e; --c7:#b87089; --c8:#a99c8e;
  --shadow:0 1px 2px rgba(60,45,30,.05), 0 8px 24px -12px rgba(90,65,35,.18);
  --radius:15px;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#201b17; --surface:#2a2420; --surface-2:#332c26; --line:#3d352d;
  --ink:#f1e9dd; --ink-2:#b7a998; --ink-3:#8a7d6d;
  --accent:#e0a458; --accent-soft:#5a4426;
  --good:#8bb27f; --bad:#e0806f; --good-soft:#33402c; --bad-soft:#43302a;
  --c1:#e0a458; --c2:#5aa6ad; --c3:#c07a92; --c4:#a3c470; --c5:#8a9bd0;
  --c6:#e08a63; --c7:#dba0b6; --c8:#b2a596;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 30px -14px rgba(0,0,0,.6);
}}
:root[data-theme="light"]{
  --ground:#f6f1e9; --surface:#fcfaf5; --surface-2:#f1ebe0; --line:#e6dccb;
  --ink:#2e2621; --ink-2:#6f6357; --ink-3:#9c9081; --accent:#c17d34; --accent-soft:#e9d4b3;
  --good:#5e8b57; --bad:#bc4b3c; --good-soft:#dfe8d6; --bad-soft:#f0d9d2;
  --c1:#c8873b; --c2:#3e7c82; --c3:#8e4e63; --c4:#7a9b52; --c5:#5b6e9e; --c6:#c1663e; --c7:#b87089; --c8:#a99c8e;
  --shadow:0 1px 2px rgba(60,45,30,.05), 0 8px 24px -12px rgba(90,65,35,.18);
}
:root[data-theme="dark"]{
  --ground:#201b17; --surface:#2a2420; --surface-2:#332c26; --line:#3d352d;
  --ink:#f1e9dd; --ink-2:#b7a998; --ink-3:#8a7d6d; --accent:#e0a458; --accent-soft:#5a4426;
  --good:#8bb27f; --bad:#e0806f; --good-soft:#33402c; --bad-soft:#43302a;
  --c1:#e0a458; --c2:#5aa6ad; --c3:#c07a92; --c4:#a3c470; --c5:#8a9bd0; --c6:#e08a63; --c7:#dba0b6; --c8:#b2a596;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 30px -14px rgba(0,0,0,.6);
}
*{box-sizing:border-box} html,body{margin:0}
body{background:var(--ground); color:var(--ink); font-family:var(--sans); font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased; padding-bottom:48px}
.wrap{max-width:1080px; margin:0 auto; padding:0 20px}
h1,h2,h3{font-family:var(--serif); font-weight:600; margin:0; text-wrap:balance}
.tnum{font-variant-numeric:tabular-nums}
header{position:sticky; top:0; z-index:30; background:color-mix(in srgb,var(--ground) 90%,transparent); backdrop-filter:blur(10px); border-bottom:1px solid var(--line)}
.head{display:flex; align-items:center; gap:14px; padding:13px 20px; max-width:1080px; margin:0 auto; flex-wrap:wrap}
.brand{display:flex; align-items:center; gap:10px; margin-right:auto}
.brand .dot{width:30px; height:30px; border-radius:9px; background:radial-gradient(120% 120% at 30% 25%, var(--c1), var(--accent) 55%, var(--c6)); box-shadow:inset 0 0 0 1px rgba(255,255,255,.25)}
.brand h1{font-size:20px} .brand small{display:block; font-size:11px; color:var(--ink-3)}
button.ghost{font-family:var(--sans); color:var(--ink); background:var(--surface); border:1px solid var(--line); border-radius:10px; width:38px; height:38px; display:grid; place-items:center; font-size:16px; cursor:pointer}
button.ghost:hover{border-color:var(--accent)}
/* filtros */
.filters{display:flex; gap:10px; padding:0 20px 12px; max-width:1080px; margin:0 auto; flex-wrap:wrap; align-items:flex-start}
.ms{position:relative}
.ms-btn{display:flex; align-items:center; gap:8px; font-family:var(--sans); font-size:13.5px; color:var(--ink); background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:8px 12px; cursor:pointer; max-width:340px}
.ms-btn:hover{border-color:var(--accent)}
.ms-btn .k{font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--ink-3); font-weight:600}
.ms-btn .v{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.ms-btn .car{color:var(--ink-3); font-size:11px}
.ms-panel{position:absolute; top:calc(100% + 6px); left:0; z-index:40; background:var(--surface); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); padding:8px; min-width:230px; max-height:340px; overflow:auto; display:none}
.ms-panel.open{display:block}
.ms-panel .quick{display:flex; gap:6px; padding:4px 6px 8px; border-bottom:1px solid var(--line); margin-bottom:6px}
.ms-panel .quick button{flex:1; font-size:12px; padding:5px; border:1px solid var(--line); background:var(--surface-2); color:var(--ink-2); border-radius:8px; cursor:pointer}
.ms-panel .quick button:hover{border-color:var(--accent); color:var(--ink)}
.opt{display:flex; align-items:center; gap:9px; padding:6px 8px; border-radius:8px; cursor:pointer; font-size:13.5px}
.opt:hover{background:var(--surface-2)}
.opt input{accent-color:var(--accent); width:15px; height:15px}
.opt .sw{width:10px;height:10px;border-radius:3px;flex:none}
.tabs{display:flex; gap:4px; padding:0 20px; max-width:1080px; margin:0 auto; flex-wrap:wrap}
.tab{font-family:var(--sans); font-size:14.5px; font-weight:600; color:var(--ink-2); background:none; border:none; padding:9px 15px; border-radius:10px 10px 0 0; cursor:pointer; border-bottom:2px solid transparent}
.tab:hover{color:var(--ink)} .tab[aria-selected="true"]{color:var(--ink); background:var(--surface); border-bottom-color:var(--accent)}
main{padding-top:22px}
.card{background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:18px; box-shadow:var(--shadow)}
.card h3{font-size:16px} .card .sub{font-size:12px; color:var(--ink-3); margin-bottom:14px}
.cols{display:grid; grid-template-columns:1fr 1fr; gap:16px} @media(max-width:820px){.cols{grid-template-columns:1fr}}
.kpis{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px} @media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface); border:1px solid var(--line); border-radius:13px; padding:14px 15px; box-shadow:var(--shadow)}
.kpi .lab{font-size:11.5px; color:var(--ink-3); letter-spacing:.04em; text-transform:uppercase; font-weight:600}
.kpi .val{font-family:var(--serif); font-size:26px; margin-top:6px} .kpi .val small{font-size:14px; color:var(--ink-2); font-family:var(--sans)}
.pill{display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:600; padding:3px 9px; border-radius:20px; margin-top:8px}
.pill.up{background:var(--bad-soft); color:var(--bad)} .pill.down{background:var(--good-soft); color:var(--good)} .pill.flat{background:var(--surface-2); color:var(--ink-2)}
.saldos{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px} @media(max-width:560px){.saldos{grid-template-columns:1fr 1fr; gap:8px} .acct{padding:11px 12px} .acct .s{font-size:17px}}
.acct{padding:13px 15px; border-radius:12px; background:var(--surface-2); border:1px solid var(--line); min-width:0}
.acct .logo{width:22px; height:22px; border-radius:6px; display:block; flex:none}
.acct .body{display:flex; flex-direction:column; gap:3px; min-width:0}
.acct .n{font-size:12.5px; color:var(--ink-2); display:flex; align-items:center; gap:7px}
.acct .n .tag{font-size:10px; font-weight:700; letter-spacing:.05em; padding:1px 6px; border-radius:6px; background:var(--surface); color:var(--ink-3)}
.acct .s{font-family:var(--serif); font-size:20px; white-space:nowrap} .acct .cur{font-size:12px; color:var(--ink-3)}
.acct .warn{font-size:11.5px; color:var(--bad)}
.money-row{display:grid; grid-template-columns:2fr 1fr; gap:16px; margin-bottom:16px} @media(max-width:820px){.money-row{grid-template-columns:1fr}}
.money-row .card{margin:0}
.inv{display:flex; flex-direction:column; gap:10px}
.inv-tot{display:flex; justify-content:space-between; font-size:13px; color:var(--ink-2); border-top:1px solid var(--line); padding-top:10px; margin-top:2px}
.inv-tot b{font-family:var(--serif); font-size:17px; color:var(--ink)}
details.sec{margin-top:12px}
details.sec summary{cursor:pointer; font-size:12.5px; color:var(--ink-3); list-style:none; display:inline-flex; align-items:center; gap:6px}
details.sec summary::-webkit-details-marker{display:none}
details.sec summary::before{content:"▸"; font-size:10px} details.sec[open] summary::before{content:"▾"}
.sec-list{display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:6px 14px; margin-top:8px}
.sec-row{display:grid; grid-template-columns:18px 1fr auto; align-items:center; gap:8px; font-size:12.5px; color:var(--ink-2); padding:5px 8px; border-radius:8px; background:var(--surface-2)}
.sec-row .logo{width:18px; height:18px; border-radius:5px; margin:0}
.sec-row .amt{font-variant-numeric:tabular-nums; color:var(--ink); font-weight:600}
.sec-row .f{font-size:11px; color:var(--ink-3); grid-column:2 / 4}
.seg{display:inline-flex; gap:2px; padding:2px; background:var(--surface-2); border:1px solid var(--line); border-radius:9px; margin:0 0 10px}
.seg button{font-family:var(--sans); font-size:12px; font-weight:600; color:var(--ink-2); background:none; border:none; padding:5px 10px; border-radius:7px; cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--surface); color:var(--ink); box-shadow:0 1px 2px rgba(0,0,0,.08)}
.seg button:disabled{opacity:.4; cursor:not-allowed}
.legend{display:flex; flex-wrap:wrap; gap:10px 16px; margin-top:8px}
.legend .it{display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--ink-2)}
.legend .sw{width:11px; height:11px; border-radius:3px; flex:none} .legend .v{color:var(--ink); font-variant-numeric:tabular-nums}
.bars{display:flex; flex-direction:column; gap:9px}
.bar-row{display:grid; grid-template-columns:130px 1fr auto; align-items:center; gap:12px; font-size:13px} @media(max-width:560px){.bar-row{grid-template-columns:100px 1fr auto; gap:8px}}
.bar-row .lbl{color:var(--ink-2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.bar-track{background:var(--surface-2); border-radius:6px; height:15px; overflow:hidden}
.bar-fill{height:100%; border-radius:6px; background:linear-gradient(90deg,var(--accent),var(--c1))}
.bar-lane{height:15px}
.bar-row.vs{grid-template-columns:130px 1fr 86px 122px}
@media(max-width:560px){
  .bar-row.vs{grid-template-columns:1fr 74px 100px; grid-template-areas:"lbl amt prev" "bar bar bar"; gap:3px 8px}
  .bar-row.vs .lbl{grid-area:lbl} .bar-row.vs .amt{grid-area:amt} .bar-row.vs .prev{grid-area:prev}
  .bar-row.vs .bar-lane{grid-area:bar; height:7px} .bar-head .bar-lane{display:none}
}
.bar-head span{white-space:nowrap} .bar-head .amt,.bar-head .prev{color:var(--ink-3); font-size:11px; font-weight:600}
.bar-row.vs .amt,.bar-row.vs .prev{text-align:right}
.bar-row .prev{font-variant-numeric:tabular-nums; color:var(--ink-3); white-space:nowrap; font-size:12.5px}
.bar-row .prev .d{margin-left:0; margin-right:4px}
.bar-head{font-size:11px; letter-spacing:.05em; text-transform:uppercase; color:var(--ink-3); font-weight:600; margin-bottom:2px}
.bar-head span:nth-child(3),.bar-head span:nth-child(4){text-align:right}
.bar-row .amt{font-variant-numeric:tabular-nums; color:var(--ink); font-weight:600; white-space:nowrap}
.d.up{color:var(--bad)} .d.down{color:var(--good)} .d{font-size:11px; margin-left:6px}
table{width:100%; border-collapse:collapse; font-size:13.5px} .tscroll{overflow-x:auto}
th,td{padding:9px 10px; text-align:right; white-space:nowrap} th:first-child,td:first-child{text-align:left}
thead th{font-size:11px; letter-spacing:.05em; text-transform:uppercase; color:var(--ink-3); font-weight:600; border-bottom:1px solid var(--line)}
tbody tr{border-bottom:1px solid var(--line)} tbody tr:last-child{border-bottom:none} tbody td{font-variant-numeric:tabular-nums}
tr.total td{font-weight:700; border-top:2px solid var(--line); border-bottom:none}
.chip{display:inline-block; font-size:11.5px; font-weight:600; padding:2px 8px; border-radius:7px; font-variant-numeric:tabular-nums}
.chip.good{background:var(--good-soft); color:var(--good)} .chip.bad{background:var(--bad-soft); color:var(--bad)}
svg{display:block; width:100%; overflow:visible}
.gridln{stroke:var(--line); stroke-width:1; stroke-dasharray:2 4; opacity:.7}
.axlab{fill:var(--ink-3); font-size:11px; font-family:var(--sans)}
.tip{position:fixed; z-index:60; pointer-events:none; background:var(--ink); color:var(--ground); font-size:12px; padding:7px 10px; border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.25); opacity:0; transition:opacity .1s; white-space:nowrap}
.foot{color:var(--ink-3); font-size:12px; text-align:center; margin-top:26px}
.hidden{display:none !important}
.posbar{height:26px; border-radius:8px; background:var(--surface-2); position:relative; overflow:hidden; margin-top:6px}
.posbar .mid{position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--ink-3); opacity:.4}
.split2{display:grid; grid-template-columns:1fr 1fr; gap:14px} @media(max-width:560px){.split2{grid-template-columns:1fr}}
.mini{display:flex; flex-direction:column; gap:3px; padding:12px 14px; border-radius:12px; background:var(--surface-2); border:1px solid var(--line)}
.mini .l{font-size:11.5px; color:var(--ink-3); text-transform:uppercase; letter-spacing:.04em; font-weight:600}
.mini .n{font-family:var(--serif); font-size:21px}
</style>
</head>
<body>
<div class="tip" id="tip"></div>
<header>
  <div class="head">
    <div class="brand"><div class="dot"></div><div><h1>Mis gastos</h1><small id="genstamp"></small></div></div>
    <button class="ghost" id="theme" title="Cambiar tema" aria-label="Cambiar tema">◐</button>
  </div>
  <div class="filters">
    <div class="ms" id="ms-mes"></div>
    <div class="ms" id="ms-cat"></div>
    <div class="ms" id="ms-tipo"></div>
  </div>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" data-tab="cat" aria-selected="true">Categorías</button>
    <button class="tab" role="tab" data-tab="pre" aria-selected="false">Presupuesto</button>
    <button class="tab" role="tab" data-tab="casa" aria-selected="false">Casa</button>
  </div>
</header>
<main class="wrap">
  <section id="tab-cat"></section>
  <section id="tab-pre" class="hidden"></section>
  <section id="tab-casa" class="hidden"></section>
  <p class="foot" id="foot"></p>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const CAT_COLORS = ["--c1","--c2","--c3","--c4","--c5","--c6","--c7","--c8"];
const cssvar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt0 = n => Math.round(n).toLocaleString("es-UY");
const fmt = n => (n<0?"-":"")+"$ "+fmt0(Math.abs(n));
const fmtc = (n,cur) => (n<0?"-":"")+(cur==="USD"?"US$ ":"$ ")+Math.abs(n).toLocaleString("es-UY",{minimumFractionDigits:cur==="USD"?2:0,maximumFractionDigits:cur==="USD"?2:0});
const MN=["","ene","feb","mar","abr","may","jun","jul","ago","set","oct","nov","dic"];
const mesLabel = m => { const [y,mm]=m.split("-"); return MN[+mm]+" "+y; };
const el=(t,c,h)=>{const e=document.createElement(t); if(c)e.className=c; if(h!=null)e.innerHTML=h; return e;};
const tip=document.getElementById("tip");
const showTip=(h,x,y)=>{tip.innerHTML=h; tip.style.opacity=1; tip.style.left=(x+14)+"px"; tip.style.top=(y+14)+"px";};
const hideTip=()=>tip.style.opacity=0;

let selMeses=[D.mes_default];
let selCats=null;            // null = todas
const TIPOS=["Fijo","Variable"];
let selTipos=new Set(TIPOS); // Fijo / Variable (config/categorias.csv)
let casaVista="total";       // Evolución de casa: total | ambos | joaco | lu
let curTab="cat";

/* ------- helpers de agregación (sobre selMeses, filtrado por selCats y selTipos) ------- */
const tipoDe = c => D.cat_tipo[c] || "Variable";
const catOk = c => (!selCats || selCats.has(c)) && selTipos.has(tipoDe(c));
const tipoFiltrado = () => selTipos.size < TIPOS.length;
const tipoNote = () => tipoFiltrado() ? (selTipos.has("Fijo") ? " · solo fijos" : " · solo variables") : "";
const fechaCorta = f => f ? f.slice(8,10)+"/"+f.slice(5,7) : "";
const logoImg = (s,cls) => s.logo ? `<img class="${cls||'logo'}" src="${s.logo}" alt="">` : "";
/* color progresivo según % del presupuesto usado: <=70% verde, 100% ámbar, >=130% rojo */
function devColor(pct){
  if(pct==null||!isFinite(pct)) return {fg:"var(--ink-2)",bg:"var(--surface-2)"};
  const mix=(a,b,t)=>`color-mix(in oklab, ${b} ${t}%, ${a})`;
  if(pct<=100){ const t=Math.min(1,Math.max(0,(pct-70)/30))*100;
    return {fg:mix("var(--good)","var(--accent)",t), bg:mix("var(--good-soft)","var(--accent-soft)",t)}; }
  const t=Math.min(1,(pct-100)/30)*100;
  return {fg:mix("var(--accent)","var(--bad)",t), bg:mix("var(--accent-soft)","var(--bad-soft)",t)};
}
const latestSel = () => selMeses.slice().sort().slice(-1)[0];
const incluyeCurso = () => selMeses.includes(D.mes_en_curso);
function aggMap(src, val){ const a={}; selMeses.forEach(m=>(src[m]||[]).forEach(x=>{ if(catOk(x.cat)) a[x.cat]=(a[x.cat]||0)+val(x); })); return a; }
function last6(mes){ const i=D.meses.indexOf(mes); const o=[]; for(let k=Math.max(0,i-5);k<=i;k++) o.push(D.meses[k]); return o; }
function periodLabel(){
  if(selMeses.length===1) return mesLabel(selMeses[0]);
  const s=selMeses.slice().sort(), idx=s.map(m=>D.meses.indexOf(m));
  const contig=idx.every((v,i)=>i===0||v===idx[i-1]+1);
  return contig ? mesLabel(s[0])+" – "+mesLabel(s[s.length-1]) : selMeses.length+" meses";
}
function cursoNote(){ return incluyeCurso()?`<span class="pill flat">● incluye ${mesLabel(D.mes_en_curso)} en curso (día ${D.dia_corte}/${D.dias_mes})</span>`:""; }

/* ------------------------------- charts -------------------------------- */
function donut(items,total){
  const R=78,r=48,C=100; let a=-Math.PI/2, paths="";
  items.forEach((it,i)=>{ const f=total>0?it.value/total:0,a2=a+f*2*Math.PI;
    const x1=C+R*Math.cos(a),y1=C+R*Math.sin(a),x2=C+R*Math.cos(a2),y2=C+R*Math.sin(a2);
    const xi=C+r*Math.cos(a2),yi=C+r*Math.sin(a2),xi2=C+r*Math.cos(a),yi2=C+r*Math.sin(a),lg=f>.5?1:0;
    paths+=`<path d="M${x1} ${y1} A${R} ${R} 0 ${lg} 1 ${x2} ${y2} L${xi} ${yi} A${r} ${r} 0 ${lg} 0 ${xi2} ${yi2} Z" fill="${cssvar(it.cvar)}" stroke="var(--surface)" stroke-width="2" data-i="${i}" style="cursor:pointer"/>`; a=a2; });
  return `<svg viewBox="0 0 200 200" style="max-width:220px;margin:0 auto" role="img" aria-label="Composición">${paths}<text x="100" y="94" text-anchor="middle" class="axlab">Total</text><text x="100" y="116" text-anchor="middle" style="font-family:var(--serif);font-size:20px;fill:var(--ink)">${fmt(total)}</text></svg>`;
}
function lineChart(series,label){ // series:[{name,cvar,points:[{m,v}]}]
  const W=560,H=200,pl=46,pr=14,pt=16,pb=28,iw=W-pl-pr,ih=H-pt-pb;
  const ms=series[0].points.map(p=>p.m);
  const max=Math.max(1,...series.flatMap(s=>s.points.map(p=>p.v)))*1.15;
  const X=i=>pl+(ms.length<2?iw/2:iw*i/(ms.length-1)), Y=v=>pt+ih-v/max*ih;
  let g="",lab="";
  for(let k=0;k<=3;k++){const v=max*k/3,y=Y(v); g+=`<line class="gridln" x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}"/>`; lab+=`<text class="axlab" x="${pl-8}" y="${y+4}" text-anchor="end">${fmt0(v/1000)}k</text>`;}
  const xl=ms.map((m,i)=>`<text class="axlab" x="${X(i)}" y="${H-8}" text-anchor="middle">${MN[+m.split("-")[1]]}</text>`).join("");
  let body="";
  series.forEach(s=>{ const col=cssvar(s.cvar), single=series.length===1;
    const dl=s.points.map((p,i)=>`${X(i)},${Y(p.v)}`).join(" ");
    if(single){ const area=`${pl},${pt+ih} ${dl} ${X(s.points.length-1)},${pt+ih}`;
      body+=`<defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".26"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs><polygon points="${area}" fill="url(#ga)"/>`; }
    body+=`<polyline points="${dl}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`;
    body+=s.points.map((p,i)=>`<circle cx="${X(i)}" cy="${Y(p.v)}" r="${i===s.points.length-1&&single?5:3.5}" fill="${col}" stroke="var(--surface)" stroke-width="2" data-m="${p.m}" data-v="${p.v}" data-n="${s.name}" style="cursor:pointer"/>`).join("");
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">${g}${body}${lab}${xl}</svg>`;
}
/* barras por categoría; con prev (mes anterior) agrega una 4ta columna alineada */
function hbarsVs(items, prevLabel){ const max=Math.max(1,...items.map(i=>i.val)); const vs=prevLabel!=null;
  const head=vs?`<div class="bar-row vs bar-head"><span class="lbl"></span><span class="bar-lane"></span><span class="amt">Gasto</span><span class="prev">vs. mes anterior</span></div>`:"";
  return `<div class="bars">`+head+items.map(it=>{
    let prev="";
    if(vs){ const p=it.prev||0, dl=it.val-p; const up=dl>0;
      const d=Math.abs(dl)>=1?`<span class="d ${up?'up':'down'}">${up?'▲':'▼'}</span>`:"";
      prev=`<span class="prev" title="${prevLabel}: ${fmt(p)} (${dl>=0?'+':''}${fmt0(dl)})">${d}${fmt(p)}</span>`; }
    return `<div class="bar-row${vs?' vs':''}"><span class="lbl" title="${it.lbl}">${it.lbl}</span><div class="bar-lane"><div class="bar-fill" style="width:${it.val/max*100}%"></div></div><span class="amt">${fmt(it.val)}</span>${prev}</div>`;}).join("")+`</div>`;
}
function divBars(items){ const max=Math.max(1,...items.map(i=>Math.abs(i.dif)));
  return `<div class="bars">`+items.map(it=>{const over=it.dif>0,w=Math.abs(it.dif)/max*50,c=devColor(it.pct).fg;
    return `<div class="bar-row"><span class="lbl" title="${it.lbl}">${it.lbl}</span><div style="position:relative;height:15px"><div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--ink-3);opacity:.35"></div><div style="position:absolute;top:0;height:100%;border-radius:5px;${over?`left:50%`:`right:50%`};width:${w}%;background:${c};opacity:.85"></div></div><span class="amt" style="color:${c}">${over?'+':''}${fmt(it.dif)}</span></div>`;}).join("")+`</div>`;
}
function topN(map,n){ let a=Object.entries(map).map(([cat,gasto])=>({cat,gasto})).sort((x,y)=>y.gasto-x.gasto);
  if(a.length<=n) return a; const rest=a.slice(n).reduce((t,x)=>t+x.gasto,0); return [...a.slice(0,n),{cat:"Otros",gasto:rest}];}

/* trend: por categoría si hay 1-6 seleccionadas, si no total */
function trendSeries(getMonthCatVal){
  const win=last6(latestSel());
  if(selCats && selCats.size>=1 && selCats.size<=6){
    return [...selCats].map((cat,i)=>({name:cat,cvar:CAT_COLORS[i%8],points:win.map(m=>({m,v:getMonthCatVal(m,cat)}))}));
  }
  return [{name:"Total",cvar:"--accent",points:win.map(m=>({m,v:win&&getMonthCatVal(m,null)}))}];
}

/* -------------------------------- tabs --------------------------------- */
function renderCat(){
  const w=document.getElementById("tab-cat"); w.innerHTML="";
  const agg=aggMap(D.personal,x=>x.gasto); const total=Object.values(agg).reduce((a,b)=>a+b,0);
  const nM=selMeses.length;
  const single=nM===1; const pm=single?D.meses[D.meses.indexOf(selMeses[0])-1]:null;
  const pmap={}; if(pm)(D.personal[pm]||[]).forEach(x=>{if(catOk(x.cat))pmap[x.cat]=x.gasto;});
  const ptot=Object.values(pmap).reduce((a,b)=>a+b,0);
  const kp=el("div","kpis"); kp.style.gridTemplateColumns="repeat(2,1fr)";
  const delta=total-ptot;
  let k1 = single&&pm ? `<span class="pill ${delta>0?'up':delta<0?'down':'flat'}">${delta>0?'▲':delta<0?'▼':'—'} ${fmt0(Math.abs(delta))} vs ${mesLabel(pm)}</span>` : `<div class="sub" style="margin:6px 0 0">${nM} ${nM>1?'meses':'mes'}${selCats?' · '+selCats.size+' categorías':''}${tipoNote()}</div>`;
  kp.appendChild(el("div","kpi",`<div class="lab">Gastado · ${periodLabel()}${tipoNote()}</div><div class="val tnum">${fmt(total)}</div><div style="display:flex;gap:6px;flex-wrap:wrap">${k1}${cursoNote()}</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Promedio mensual</div><div class="val tnum">${fmt(total/nM)}</div>`));
  w.appendChild(kp);

  const mrow=el("div","money-row");
  const sc=el("div","card"); sc.appendChild(el("div",null,`<h3>Saldos estimados</h3><div class="sub">Último saldo conciliado de cada cuenta + movimientos posteriores. Aproximado.</div>`));
  const acct=s=>{ const neg=s.saldo<0;
    return el("div","acct",`<div class="body"><div class="n">${logoImg(s)}${s.cuenta}${s.grupo==='casa'?' <span class="tag">CASA</span>':''}</div><div class="s tnum">${fmtc(s.saldo,s.moneda)}</div><div class="cur">${s.base_fecha?'base al '+fechaCorta(s.base_fecha):s.moneda}</div>${neg?'<div class="warn" title="Faltan entradas sin aviso del banco (sueldo, aportes) hasta el cierre de mes">⚠ faltan entradas sin aviso</div>':''}</div>`); };
  const sg=el("div","saldos");
  D.saldos.filter(s=>s.nivel==="principal").forEach(s=>sg.appendChild(acct(s)));
  const pos=posOf(latestSel());
  if(pos) sg.appendChild(el("div","acct",`<div class="body"><div class="n">Posición de casa <span class="tag">vs LU</span></div><div class="s tnum" style="color:${pos.posicion_joaco>=0?'var(--good)':'var(--bad)'}">${pos.posicion_joaco>=0?'+':''}${fmt(pos.posicion_joaco)}</div><div class="cur">${pos.posicion_joaco>=0?'aportaste de más':'aportaste de menos'}</div></div>`));
  sc.appendChild(sg);
  const sec=D.saldos.filter(s=>s.nivel!=="principal");
  if(sec.length){ const det=el("details","sec"); det.appendChild(el("summary",null,`Otras cuentas (${sec.length})`));
    det.appendChild(el("div","sec-list",sec.map(s=>`<div class="sec-row" title="${s.saldo<0?'Faltan entradas sin aviso del banco hasta el cierre':''}">${logoImg(s)||'<span></span>'}<span>${s.cuenta}</span><span class="amt"${s.saldo<0?' style="color:var(--bad)"':''}>${fmtc(s.saldo,s.moneda)}</span><span class="f">${s.base_fecha?'base al '+fechaCorta(s.base_fecha):''}</span></div>`).join("")));
    sc.appendChild(det); }
  mrow.appendChild(sc);

  const ic=el("div","card"); ic.appendChild(el("div",null,`<h3>Inversiones</h3><div class="sub">Último saldo cargado en la planilla + giros posteriores. Sin rendimiento.</div>`));
  const il=el("div","inv"); let itot=0, ihay=false;
  (D.inversiones||[]).forEach(s=>{
    if(s.saldo==null){ il.appendChild(el("div","acct",`<div class="body"><div class="n">${logoImg(s)}${s.cuenta}</div><div class="cur">sin saldo cargado · cargalo en la planilla, pestaña Saldos</div></div>`)); return; }
    itot+=s.saldo; ihay=true;
    const ap=Math.abs(s.aportes_desde)>=0.01?` · ${s.aportes_desde>0?'+':'−'} ${fmtc(Math.abs(s.aportes_desde),s.moneda)} girados después`:"";
    il.appendChild(el("div","acct",`<div class="body"><div class="n">${logoImg(s)}${s.cuenta}</div><div class="s tnum">${fmtc(s.saldo,s.moneda)}</div><div class="cur">saldo al ${fechaCorta(s.base_fecha)}${ap}</div></div>`)); });
  if(ihay) il.appendChild(el("div","inv-tot",`<span>Total</span><b class="tnum">${fmtc(itot,"USD")}</b>`));
  ic.appendChild(il); mrow.appendChild(ic);
  w.appendChild(mrow);

  const row=el("div","cols");
  const dcard=el("div","card"); const items=topN(agg,7).map((x,i)=>({label:x.cat,value:x.gasto,cvar:CAT_COLORS[i%8]}));
  dcard.appendChild(el("div",null,`<h3>Composición</h3><div class="sub">${periodLabel()}${selCats?' · filtrado':''}${tipoNote()}</div>`));
  dcard.appendChild(el("div",null,donut(items,total)));
  const lg=el("div","legend"); items.forEach(it=>lg.appendChild(el("div","it",`<span class="sw" style="background:${cssvar(it.cvar)}"></span>${it.label} <span class="v">${fmt(it.value)}</span>`))); dcard.appendChild(lg);
  row.appendChild(dcard);
  const vs=single&&pm;
  const bcard=el("div","card"); bcard.appendChild(el("div",null,`<h3>Por categoría</h3><div class="sub">${periodLabel()}${vs?' · vs. '+mesLabel(pm):''}${tipoNote()}</div>`));
  const bitems=Object.entries(agg).map(([cat,v])=>({lbl:cat,val:v,prev:vs?(pmap[cat]||0):null})).sort((a,b)=>b.val-a.val);
  bcard.appendChild(el("div",null,hbarsVs(bitems, vs?mesLabel(pm):null))); row.appendChild(bcard); w.appendChild(row);

  const tc=el("div","card");
  if(selCats&&selCats.size===0){ tc.appendChild(el("div",null,`<h3>Evolución</h3><div class="sub">Elegí una o más categorías en el filtro para ver su evolución.</div>`)); }
  else {
    const multi=selCats&&selCats.size>=1&&selCats.size<=6;
    tc.appendChild(el("div",null,`<h3>Evolución</h3><div class="sub">${multi?'por categoría · ':''}últimos 6 meses${multi?'':' (gasto personal'+(selCats?' filtrado':'')+')'}</div>`));
    const series=trendSeries((m,cat)=>(D.personal[m]||[]).filter(x=>cat?x.cat===cat:catOk(x.cat)).reduce((t,x)=>t+x.gasto,0));
    tc.appendChild(el("div","tscroll",lineChart(series,"Evolución")));
    if(series.length>1){const lg2=el("div","legend"); series.forEach(s=>lg2.appendChild(el("div","it",`<span class="sw" style="background:${cssvar(s.cvar)}"></span>${s.name}`))); tc.appendChild(lg2);}
  }
  w.appendChild(tc);
  wireDonut(dcard,items,total); wireLine(tc);
}
function renderPre(){
  const w=document.getElementById("tab-pre"); w.innerHTML="";
  const agg={}; selMeses.forEach(m=>(D.presupuesto[m]||[]).forEach(x=>{ if(!catOk(x.cat))return; const a=agg[x.cat]||(agg[x.cat]={ppto:0,real:0}); a.ppto+=x.ppto; a.real+=x.real; }));
  const list=Object.entries(agg).map(([cat,v])=>({cat,ppto:v.ppto,real:v.real,dif:v.real-v.ppto})).filter(x=>x.ppto>0||x.real>0);
  const tp=list.reduce((t,x)=>t+x.ppto,0), tr=list.reduce((t,x)=>t+x.real,0), td=tr-tp;
  const kp=el("div","kpis"); kp.style.gridTemplateColumns="repeat(3,1fr)";
  const tpct=tp>0?tr/tp*100:null, tcol=devColor(tpct);
  kp.appendChild(el("div","kpi",`<div class="lab">Presupuesto · ${periodLabel()}${tipoNote()}</div><div class="val tnum">${fmt(tp)}</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Gastado</div><div class="val tnum">${fmt(tr)}</div>${cursoNote()}`));
  kp.appendChild(el("div","kpi",`<div class="lab">Diferencia</div><div class="val tnum" style="color:${tcol.fg}">${td>0?'+':''}${fmt(td)}</div><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="pill" style="background:${tcol.bg};color:${tcol.fg}">uso: ${tpct==null?'—':tpct.toLocaleString("es-UY",{minimumFractionDigits:1,maximumFractionDigits:1})+'%'}</span><span class="pill ${td>0?'up':'down'}">${td>0?'sobre presupuesto':'dentro'}</span></div>`));
  w.appendChild(kp);
  const pctOf=x=>x.ppto>0?x.real/x.ppto*100:null;
  const dc=el("div","card"); dc.appendChild(el("div",null,`<h3>Variación por categoría</h3><div class="sub">Izquierda = ahorraste · Derecha = te pasaste · color según % del presupuesto usado</div>`));
  dc.appendChild(el("div",null,divBars(list.filter(x=>Math.abs(x.dif)>=1).sort((a,b)=>b.dif-a.dif).map(x=>({lbl:x.cat,dif:x.dif,pct:x.ppto>0?pctOf(x):(x.real>0?999:null)}))))); w.appendChild(dc);
  const tcard=el("div","card"); tcard.appendChild(el("div",null,`<h3>Detalle</h3><div class="sub">${periodLabel()}${tipoNote()} · verde = holgado, rojo = pasado</div>`));
  const rows=list.sort((a,b)=>b.real-a.real).map(x=>{const pct=pctOf(x),over=x.dif>0,c=devColor(pct!=null?pct:(x.real>0?999:null));
    return `<tr><td>${x.cat}</td><td>${fmt(x.ppto)}</td><td>${fmt(x.real)}</td><td><span class="chip" style="background:${c.bg};color:${c.fg}">${over?'+':''}${fmt0(x.dif)}</span></td><td style="color:${c.fg};font-weight:700">${pct!=null?pct.toFixed(0)+'%':'—'}</td></tr>`;}).join("");
  tcard.appendChild(el("div",null,`<div class="tscroll"><table><thead><tr><th>Categoría</th><th>Presupuesto</th><th>Real</th><th>Diferencia</th><th>% usado</th></tr></thead><tbody>${rows}<tr class="total"><td>Total</td><td>${fmt(tp)}</td><td>${fmt(tr)}</td><td style="color:${tcol.fg}">${td>0?'+':''}${fmt0(td)}</td><td style="color:${tcol.fg}">${tpct!=null?tpct.toFixed(1)+'%':'—'}</td></tr></tbody></table></div>`)); w.appendChild(tcard);
}
function posOf(mes){ // posición del último mes con cierre <= mes seleccionado
  const ms=Object.keys(D.casa_cierre).filter(m=>m<=mes).sort(); return ms.length?D.casa_cierre[ms[ms.length-1]]:null;
}
function renderCasa(){
  const w=document.getElementById("tab-casa"); w.innerHTML="";
  let total=0,lu=0,joaco=0; const cats={};
  selMeses.forEach(m=>{const c=D.casa[m]; if(!c)return; c.cats.forEach(x=>{ if(!catOk(x.cat))return; const a=cats[x.cat]||(cats[x.cat]={total:0,lu:0,joaco:0}); a.total+=x.total; a.lu+=x.lu; a.joaco+=x.joaco; total+=x.total; lu+=x.lu; joaco+=x.joaco; });});
  let apLu=0,apJo=0; selMeses.forEach(m=>{const c=D.casa_cierre[m]; if(c){apLu+=c.aportes_lu; apJo+=c.aportes_joaco;}});
  const kp=el("div","kpis"); kp.style.gridTemplateColumns="repeat(3,1fr)";
  kp.appendChild(el("div","kpi",`<div class="lab">Gasto de casa · ${periodLabel()}${tipoNote()}</div><div class="val tnum">${fmt(total)}</div>${cursoNote()}`));
  kp.appendChild(el("div","kpi",`<div class="lab">Tu parte</div><div class="val tnum">${fmt(joaco)}</div><div class="sub" style="margin:6px 0 0">${total>0?(joaco/total*100).toFixed(0):0}% del total</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Parte de Lu</div><div class="val tnum">${fmt(lu)}</div><div class="sub" style="margin:6px 0 0">${total>0?(lu/total*100).toFixed(0):0}% del total</div>`));
  w.appendChild(kp);

  // aportes del período (feature nueva)
  const ac=el("div","card"); ac.appendChild(el("div",null,`<h3>Aportes del período</h3><div class="sub">Lo que puso cada uno a la cuenta casa en ${periodLabel()} (transferencias + gastos de casa pagados de su bolsillo)</div>`));
  const sp=el("div","split2");
  sp.appendChild(el("div","mini",`<div class="l">Vos (Joaco)</div><div class="n tnum">${fmt(apJo)}</div>`));
  sp.appendChild(el("div","mini",`<div class="l">Lu</div><div class="n tnum">${fmt(apLu)}</div>`));
  ac.appendChild(sp);
  const tap=apJo+apLu;
  if(tap>0) ac.appendChild(el("div",null,`<div class="bar-track" style="height:12px;margin-top:12px;display:flex;gap:2px;background:none"><div style="width:${apJo/tap*100}%;background:${cssvar('--c1')};border-radius:6px 0 0 6px" title="Vos"></div><div style="width:${apLu/tap*100}%;background:${cssvar('--c2')};border-radius:0 6px 6px 0" title="Lu"></div></div><div class="legend"><div class="it"><span class="sw" style="background:${cssvar('--c1')}"></span>Vos ${tap>0?(apJo/tap*100).toFixed(0):0}%</div><div class="it"><span class="sw" style="background:${cssvar('--c2')}"></span>Lu ${tap>0?(apLu/tap*100).toFixed(0):0}%</div></div>`));
  w.appendChild(ac);

  const pos=posOf(latestSel());
  if(pos){ const pc=el("div","card"); const maxp=Math.max(Math.abs(pos.posicion_lu),Math.abs(pos.posicion_joaco),1);
    pc.appendChild(el("div",null,`<h3>Posición acumulada</h3><div class="sub">Cuenta continua (sin transferencias de ajuste). Positivo = aportaste de más.</div>`));
    const mk=(nom,v)=>`<div style="flex:1"><div style="display:flex;justify-content:space-between;font-size:13px"><span>${nom}</span><b class="tnum" style="color:${v>=0?'var(--good)':'var(--bad)'}">${v>=0?'+':''}${fmt(v)}</b></div><div class="posbar"><div class="mid"></div><div style="position:absolute;top:0;bottom:0;${v>=0?`left:50%;width:${Math.abs(v)/maxp*50}%;background:var(--good)`:`right:50%;width:${Math.abs(v)/maxp*50}%;background:var(--bad)`};opacity:.85"></div></div></div>`;
    pc.appendChild(el("div",null,`<div style="display:flex;gap:24px;flex-wrap:wrap">${mk("Vos (Joaco)",pos.posicion_joaco)}${mk("Lu",pos.posicion_lu)}</div>`)); w.appendChild(pc);
  }

  const row=el("div","cols");
  const scard=el("div","card"); scard.appendChild(el("div",null,`<h3>Reparto por categoría</h3><div class="sub"><span style="color:${cssvar('--c1')}">■</span> Vos &nbsp; <span style="color:${cssvar('--c2')}">■</span> Lu</div>`));
  const cl=Object.entries(cats).map(([cat,v])=>({cat,...v})).sort((a,b)=>b.total-a.total);
  const maxc=Math.max(1,...cl.map(x=>x.total));
  scard.appendChild(el("div","bars",cl.map((x,i)=>`<div class="bar-row" data-i="${i}" style="cursor:default"><span class="lbl" title="${x.cat}">${x.cat}</span><div class="bar-track" style="display:flex;height:15px;gap:2px;background:none"><div style="width:${x.joaco/maxc*100}%;background:${cssvar('--c1')};border-radius:5px 0 0 5px"></div><div style="width:${x.lu/maxc*100}%;background:${cssvar('--c2')};border-radius:0 5px 5px 0"></div></div><span class="amt">${fmt(x.total)}</span></div>`).join(""))); row.appendChild(scard);
  wireReparto(scard,cl);
  const ec=el("div","card");
  if(selCats&&selCats.size===0){ ec.appendChild(el("div",null,`<h3>Evolución</h3><div class="sub">Elegí una o más categorías en el filtro para ver su evolución.</div>`)); }
  else {
    const multi=selCats&&selCats.size>=1&&selCats.size<=6;
    if(multi&&casaVista==="ambos") casaVista="total";   // por categoría: una sola persona a la vez
    const QUIEN={total:"gasto total",joaco:"tu parte",lu:"parte de Lu",ambos:"tu parte y la de Lu"};
    ec.appendChild(el("div",null,`<h3>Evolución</h3><div class="sub">${multi?'por categoría · ':''}${QUIEN[casaVista]} · últimos 6 meses${tipoNote()}</div>`));
    const seg=el("div","seg");
    [["total","Total"],["ambos","Vos y Lu"],["joaco","Vos"],["lu","Lu"]].forEach(([k,t])=>{
      const b=el("button",null,t); b.setAttribute("aria-pressed",casaVista===k);
      if(multi&&k==="ambos"){ b.disabled=true; b.title="Con categorías elegidas, mirá Vos o Lu por separado"; }
      b.onclick=()=>{ casaVista=k; hideTip(); renderCasa(); }; seg.appendChild(b); });
    ec.appendChild(seg);
    const val=(m,cat,campo)=>{const c=D.casa[m]; if(!c)return 0; return c.cats.filter(x=>cat?x.cat===cat:catOk(x.cat)).reduce((t,x)=>t+x[campo],0);};
    let series;
    if(casaVista==="ambos"){ const win=last6(latestSel());
      series=[{name:"Vos",cvar:"--c1",points:win.map(m=>({m,v:val(m,null,"joaco")}))},
              {name:"Lu",cvar:"--c2",points:win.map(m=>({m,v:val(m,null,"lu")}))}]; }
    else { series=trendSeries((m,cat)=>val(m,cat,casaVista));
      if(!multi&&casaVista!=="total"){ series[0].name=casaVista==="joaco"?"Vos":"Lu"; series[0].cvar=casaVista==="joaco"?"--c1":"--c2"; } }
    ec.appendChild(el("div","tscroll",lineChart(series,"Evolución casa")));
    if(series.length>1){const lg=el("div","legend"); series.forEach(s=>lg.appendChild(el("div","it",`<span class="sw" style="background:${cssvar(s.cvar)}"></span>${s.name}`))); ec.appendChild(lg);}
  }
  row.appendChild(ec); w.appendChild(row); wireLine(ec);
}
function wireReparto(card,cl){ card.querySelectorAll(".bar-row[data-i]").forEach(r=>{ r.addEventListener("mousemove",e=>{const x=cl[+r.dataset.i], t=x.total||1;
  showTip(`${x.cat}<br><span style="color:${cssvar('--c1')}">■</span> Vos <b>${fmt(x.joaco)}</b> (${(x.joaco/t*100).toFixed(0)}%)<br><span style="color:${cssvar('--c2')}">■</span> Lu <b>${fmt(x.lu)}</b> (${(x.lu/t*100).toFixed(0)}%)<br>Total <b>${fmt(x.total)}</b>`,e.clientX,e.clientY);}); r.addEventListener("mouseleave",hideTip);});}
function wireDonut(card,items,total){ card.querySelectorAll("path[data-i]").forEach(p=>{ p.addEventListener("mousemove",e=>{const it=items[+p.dataset.i]; showTip(`${it.label}<br><b>${fmt(it.value)}</b> · ${(it.value/total*100).toFixed(1)}%`,e.clientX,e.clientY);}); p.addEventListener("mouseleave",hideTip);});}
function wireLine(card){ card.querySelectorAll("circle[data-m]").forEach(c=>{ c.addEventListener("mousemove",e=>showTip(`${c.dataset.n} · ${mesLabel(c.dataset.m)}<br><b>${fmt(+c.dataset.v)}</b>`,e.clientX,e.clientY)); c.addEventListener("mouseleave",hideTip);});}

/* --------------------------- multi-select UI --------------------------- */
function multiSelect(host, {label, options, isSel, setSel, colorFor, allText="Todas"}){
  host.innerHTML="";
  const btn=el("button","ms-btn"); host.appendChild(btn);
  const panel=el("div","ms-panel"); host.appendChild(panel);
  function summary(){ const sel=options.filter(o=>isSel(o.value));
    if(!selCats && label==="Categorías") return "Todas";
    if(sel.length===0) return "Ninguna"; if(sel.length===1) return sel[0].text;
    if(sel.length===options.length) return allText; return sel.length+" seleccionadas"; }
  function paint(){ btn.innerHTML=`<span class="k">${label}</span><span class="v">${summary()}</span><span class="car">▾</span>`; }
  function repaintPanel(){ panel.querySelectorAll(".opt input").forEach((inp,i)=>{ inp.checked=isSel(options[i].value); }); }
  const quick=el("div","quick"); const bAll=el("button",null,allText); quick.appendChild(bAll);
  bAll.onclick=()=>{ setSel(options.map(o=>o.value)); repaintPanel(); };
  if(label==="Meses"){ const bMes=el("button",null,"Mes actual"); quick.appendChild(bMes); bMes.onclick=()=>{ setSel([D.mes_default]); repaintPanel(); }; }
  if(label==="Categorías"){ const bNone=el("button",null,"Ninguna"); quick.appendChild(bNone); bNone.onclick=()=>{ setSel([]); repaintPanel(); }; }
  panel.appendChild(quick);
  options.forEach(o=>{ const row=el("label","opt");
    const sw=colorFor?`<span class="sw" style="background:${colorFor(o.value)}"></span>`:"";
    row.innerHTML=`<input type="checkbox" ${isSel(o.value)?"checked":""}>${sw}<span>${o.text}</span>`;
    row.querySelector("input").onchange=e=>{ toggle(o.value, e.target.checked); };
    panel.appendChild(row); });
  btn.onclick=e=>{ e.stopPropagation(); document.querySelectorAll(".ms-panel").forEach(p=>{if(p!==panel)p.classList.remove("open");}); panel.classList.toggle("open"); };
  function toggle(v,on){ const cur=options.filter(o=>isSel(o.value)).map(o=>o.value); const s=new Set(cur); on?s.add(v):s.delete(v); setSel([...s]); }
  paint(); host._paint=paint;
}
function buildFilters(){
  const opMes=D.meses.slice().reverse().map(m=>({value:m,text:mesLabel(m)}));
  multiSelect(document.getElementById("ms-mes"),{ label:"Meses", options:opMes,
    isSel:v=>selMeses.includes(v),
    setSel:a=>{ selMeses=a.length?a:[D.mes_default]; refresh(); } });
  const opCat=D.categorias.map((c,i)=>({value:c,text:c}));
  multiSelect(document.getElementById("ms-cat"),{ label:"Categorías", options:opCat,
    isSel:v=> selCats? selCats.has(v) : true,
    colorFor:v=>cssvar(CAT_COLORS[D.categorias.indexOf(v)%8]),
    setSel:a=>{ selCats = (a.length===D.categorias.length)?null:new Set(a); refresh(); } });
  multiSelect(document.getElementById("ms-tipo"),{ label:"Tipo", allText:"Todos",
    options:TIPOS.map(t=>({value:t,text:t==="Fijo"?"Fijos":"Variables"})),
    isSel:v=>selTipos.has(v),
    setSel:a=>{ selTipos=new Set(a.length?a:TIPOS); refresh(); } });
}
document.addEventListener("click",()=>document.querySelectorAll(".ms-panel").forEach(p=>p.classList.remove("open")));

/* ------------------------------- shell --------------------------------- */
function renderAll(){ hideTip(); renderCat(); renderPre(); renderCasa(); }
function refresh(){ ["ms-mes","ms-cat","ms-tipo"].forEach(id=>{const h=document.getElementById(id); if(h&&h._paint)h._paint();}); renderAll(); }
function selectTab(t){ curTab=t; hideTip(); document.querySelectorAll(".tab").forEach(b=>b.setAttribute("aria-selected",b.dataset.tab===t)); ["cat","pre","casa"].forEach(x=>document.getElementById("tab-"+x).classList.toggle("hidden",x!==t)); }
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>selectTab(b.dataset.tab)));
document.getElementById("theme").addEventListener("click",()=>{ const root=document.documentElement; const cur=root.getAttribute("data-theme")||(matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light"); root.setAttribute("data-theme",cur==="dark"?"light":"dark"); refresh(); });
document.getElementById("genstamp").textContent=D.generado?("actualizado "+D.generado):"";
document.getElementById("foot").textContent="Generado por el pipeline · datos al "+(D.generado||"");
buildFilters(); renderAll();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
