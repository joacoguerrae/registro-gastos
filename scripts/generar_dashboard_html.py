"""Genera out/dashboard.html: dashboard self-contained (un archivo, sin dependencias
externas) del sistema de gastos. Lee los agregados que ya produce el pipeline y el
ledger, y emite HTML+CSS+JS inline. Se regenera en cada corrida -> siempre al dia.

Pestanas: Categorias (vista personal), Presupuesto vs real, Casa (con Lu).
Ademas: saldos estimados de cuentas bancarias + posicion de casa.
Estilo cozy/calido, claro y oscuro. Default: mes corriente; selector de mes.
"""
import csv
import json
import os
import datetime as dt
from collections import defaultdict

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(*a): return os.path.join(BASE, *a)

SEED_FECHA = "2026-07-12"   # corte de saldos_iniciales; movimientos posteriores se suman
BANCOS = [   # (cuenta en ledger, moneda, etiqueta, grupo)
    ("Santander UYU", "UYU", "Santander $ (caja ahorro)", "personal"),
    ("Cuenta Casa",   "UYU", "Cuenta Casa (corriente)",   "casa"),
    ("Santander USD", "USD", "Santander USD (ahorro)",    "personal"),
    ("BBVA USD",      "USD", "BBVA USD",                  "personal"),
]


def leer(path, **kw):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, **kw))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main():
    # ---- personal por mes/categoria (resumen_mensual = parte de Joaco) ----
    personal = defaultdict(list)
    for r in leer(P("out", "resumen_mensual.csv")):
        personal[r["mes"]].append({"cat": r["categoria"], "gasto": round(num(r["gasto_uyu"]), 2)})

    # ---- presupuesto vs real ----
    presup = defaultdict(list)
    for r in leer(P("out", "presupuesto_vs_real.csv")):
        presup[r["mes"]].append({
            "cat": r["categoria"], "ppto": round(num(r["presupuesto"]), 2),
            "real": round(num(r["gasto_real"]), 2), "dif": round(num(r["diferencia"]), 2)})

    # ---- casa por mes/categoria ----
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

    # ---- posicion de casa (ultimo cierre) ----
    posicion = {"lu": 0.0, "joaco": 0.0, "mes": ""}
    cc = leer(P("data", "cierre_casa.csv")) if os.path.exists(P("data", "cierre_casa.csv")) else []
    if cc:
        u = cc[-1]
        posicion = {"lu": round(num(u["posicion_lu"]), 2),
                    "joaco": round(num(u["posicion_joaco"]), 2), "mes": u["mes"]}

    # ---- saldos bancarios estimados: semilla (12/7) + movimientos posteriores ----
    seed = {}
    for r in leer(P("config", "saldos_iniciales.csv")):
        seed[(r["cuenta"], r["moneda"])] = num(r["saldo"])
    saldo = {(c, mon): seed.get((c, mon), 0.0) for c, mon, _, _ in BANCOS}
    max_fecha = ""
    for r in leer(P("data", "ledger.csv")):
        if r["fecha"] > max_fecha:
            max_fecha = r["fecha"]
        if r["fecha"] <= SEED_FECHA:
            continue
        mon = r["moneda"]
        mto = num(r["monto"])
        for (c, cmon, _, _) in BANCOS:
            if cmon != mon:
                continue
            if r["tipo"] == "gasto" and r["cuenta"] == c:
                saldo[(c, mon)] -= mto
            elif r["tipo"] == "ingreso" and r["cuenta"] == c:
                saldo[(c, mon)] += mto
            elif r["tipo"] == "transferencia":
                if r["cuenta"] == c:
                    saldo[(c, mon)] -= mto
                if r["cuenta_destino"] == c:
                    saldo[(c, mon)] += mto
    saldos = [{"cuenta": et, "moneda": mon, "saldo": round(saldo[(c, mon)], 2), "grupo": g}
              for (c, mon, et, g) in BANCOS]

    import calendar
    mes_curso = max_fecha[:7] if max_fecha else ""
    dia_corte = int(max_fecha[8:10]) if len(max_fecha) >= 10 else 0
    dias_mes = calendar.monthrange(int(mes_curso[:4]), int(mes_curso[5:7]))[1] if mes_curso else 30

    meses = sorted(set(personal) | set(presup) | set(casa))
    data = {
        "mes_en_curso": mes_curso, "dia_corte": dia_corte, "dias_mes": dias_mes,
        "generado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    if False else "",   # Date.now no disponible en algunos entornos; se stampa aparte
        "meses": meses,
        "mes_default": meses[-1] if meses else "",
        "personal": personal,
        "presupuesto": presup,
        "casa": {k: v for k, v in casa.items()},
        "posicion": posicion,
        "saldos": saldos,
        "seed_fecha": SEED_FECHA,
    }
    # timestamp de generacion (fuera del bloqueo Date.now del entorno de workflows)
    try:
        data["generado"] = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    except Exception:
        data["generado"] = ""

    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out = P("out", "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{out}: {len(meses)} meses, saldos {len(saldos)} cuentas, mes default {data['mes_default']}")


# ============================ TEMPLATE (HTML+CSS+JS) ============================
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
@media (prefers-color-scheme:dark){
  :root{
    --ground:#201b17; --surface:#2a2420; --surface-2:#332c26; --line:#3d352d;
    --ink:#f1e9dd; --ink-2:#b7a998; --ink-3:#8a7d6d;
    --accent:#e0a458; --accent-soft:#5a4426;
    --good:#8bb27f; --bad:#e0806f; --good-soft:#33402c; --bad-soft:#43302a;
    --c1:#e0a458; --c2:#5aa6ad; --c3:#c07a92; --c4:#a3c470; --c5:#8a9bd0;
    --c6:#e08a63; --c7:#dba0b6; --c8:#b2a596;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 30px -14px rgba(0,0,0,.6);
  }
}
:root[data-theme="light"]{
  --ground:#f6f1e9; --surface:#fcfaf5; --surface-2:#f1ebe0; --line:#e6dccb;
  --ink:#2e2621; --ink-2:#6f6357; --ink-3:#9c9081;
  --accent:#c17d34; --accent-soft:#e9d4b3;
  --good:#5e8b57; --bad:#bc4b3c; --good-soft:#dfe8d6; --bad-soft:#f0d9d2;
  --c1:#c8873b; --c2:#3e7c82; --c3:#8e4e63; --c4:#7a9b52; --c5:#5b6e9e;
  --c6:#c1663e; --c7:#b87089; --c8:#a99c8e;
  --shadow:0 1px 2px rgba(60,45,30,.05), 0 8px 24px -12px rgba(90,65,35,.18);
}
:root[data-theme="dark"]{
  --ground:#201b17; --surface:#2a2420; --surface-2:#332c26; --line:#3d352d;
  --ink:#f1e9dd; --ink-2:#b7a998; --ink-3:#8a7d6d;
  --accent:#e0a458; --accent-soft:#5a4426;
  --good:#8bb27f; --bad:#e0806f; --good-soft:#33402c; --bad-soft:#43302a;
  --c1:#e0a458; --c2:#5aa6ad; --c3:#c07a92; --c4:#a3c470; --c5:#8a9bd0;
  --c6:#e08a63; --c7:#dba0b6; --c8:#b2a596;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 30px -14px rgba(0,0,0,.6);
}
*{box-sizing:border-box}
html,body{margin:0}
body{
  background:var(--ground); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
  padding-bottom:48px;
}
.wrap{max-width:1080px; margin:0 auto; padding:0 20px}
h1,h2,h3{font-family:var(--serif); font-weight:600; margin:0; text-wrap:balance}
.tnum{font-variant-numeric:tabular-nums}
.eyebrow{font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--ink-3); font-weight:600}

/* header */
header{position:sticky; top:0; z-index:20; background:color-mix(in srgb,var(--ground) 88%,transparent);
  backdrop-filter:blur(10px); border-bottom:1px solid var(--line)}
.head{display:flex; align-items:center; gap:16px; padding:14px 20px; max-width:1080px; margin:0 auto; flex-wrap:wrap}
.brand{display:flex; align-items:center; gap:10px; margin-right:auto}
.brand .dot{width:30px; height:30px; border-radius:9px; background:
  radial-gradient(120% 120% at 30% 25%, var(--c1), var(--accent) 55%, var(--c6));
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.25)}
.brand h1{font-size:20px; letter-spacing:.01em}
.brand small{display:block; font-family:var(--sans); font-size:11px; color:var(--ink-3); letter-spacing:.03em}
.msel{display:flex; align-items:center; gap:8px}
.msel label{font-size:12px; color:var(--ink-2)}
select,button.ghost{font-family:var(--sans); color:var(--ink); background:var(--surface);
  border:1px solid var(--line); border-radius:10px; padding:8px 12px; font-size:14px; cursor:pointer}
select{padding-right:28px}
button.ghost{width:38px; height:38px; display:grid; place-items:center; font-size:16px}
button.ghost:hover,select:hover{border-color:var(--accent)}

/* tabs */
.tabs{display:flex; gap:4px; padding:10px 20px 0; max-width:1080px; margin:0 auto; flex-wrap:wrap}
.tab{font-family:var(--sans); font-size:14.5px; font-weight:600; color:var(--ink-2);
  background:none; border:none; padding:9px 15px; border-radius:10px 10px 0 0; cursor:pointer;
  border-bottom:2px solid transparent}
.tab:hover{color:var(--ink)}
.tab[aria-selected="true"]{color:var(--ink); background:var(--surface); border-bottom-color:var(--accent)}

/* cards & grid */
main{padding-top:22px}
.grid{display:grid; gap:16px}
.card{background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:18px 18px; box-shadow:var(--shadow)}
.card h3{font-size:16px; margin-bottom:2px}
.card .sub{font-size:12px; color:var(--ink-3); margin-bottom:14px}
.cols{display:grid; grid-template-columns:1fr 1fr; gap:16px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}

/* KPI tiles */
.kpis{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px}
@media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface); border:1px solid var(--line); border-radius:13px; padding:14px 15px; box-shadow:var(--shadow)}
.kpi .lab{font-size:11.5px; color:var(--ink-3); letter-spacing:.04em; text-transform:uppercase; font-weight:600}
.kpi .val{font-family:var(--serif); font-size:27px; margin-top:6px; letter-spacing:.01em}
.kpi .val small{font-size:15px; color:var(--ink-2); font-family:var(--sans)}
.pill{display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:600; padding:3px 9px; border-radius:20px; margin-top:8px}
.pill.up{background:var(--bad-soft); color:var(--bad)}
.pill.down{background:var(--good-soft); color:var(--good)}
.pill.flat{background:var(--surface-2); color:var(--ink-2)}

/* saldos strip */
.saldos{display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px}
.acct{display:flex; flex-direction:column; gap:3px; padding:13px 15px; border-radius:12px;
  background:var(--surface-2); border:1px solid var(--line)}
.acct .n{font-size:12.5px; color:var(--ink-2); display:flex; align-items:center; gap:7px}
.acct .n .tag{font-size:10px; font-weight:700; letter-spacing:.05em; padding:1px 6px; border-radius:6px; background:var(--surface); color:var(--ink-3)}
.acct .s{font-family:var(--serif); font-size:22px; letter-spacing:.01em}
.acct .cur{font-size:12px; color:var(--ink-3)}

/* legend */
.legend{display:flex; flex-wrap:wrap; gap:10px 16px; margin-top:8px}
.legend .it{display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--ink-2)}
.legend .sw{width:11px; height:11px; border-radius:3px; flex:none}
.legend .v{color:var(--ink); font-variant-numeric:tabular-nums; margin-left:2px}

/* bars */
.bars{display:flex; flex-direction:column; gap:9px}
.bar-row{display:grid; grid-template-columns:130px 1fr auto; align-items:center; gap:12px; font-size:13px}
@media(max-width:560px){.bar-row{grid-template-columns:104px 1fr auto; gap:8px; font-size:12px}}
.bar-row .lbl{color:var(--ink-2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.bar-track{background:var(--surface-2); border-radius:6px; height:15px; overflow:hidden}
.bar-fill{height:100%; border-radius:6px; background:linear-gradient(90deg,var(--accent),var(--c1)); transition:width .5s cubic-bezier(.2,.7,.2,1)}
.bar-row .amt{font-variant-numeric:tabular-nums; color:var(--ink); font-weight:600; white-space:nowrap}
.bar-row .amt .d{font-weight:600; font-size:11px; margin-left:6px}
.d.up{color:var(--bad)} .d.down{color:var(--good)}

/* table */
table{width:100%; border-collapse:collapse; font-size:13.5px}
.tscroll{overflow-x:auto}
th,td{padding:9px 10px; text-align:right; white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{font-size:11px; letter-spacing:.05em; text-transform:uppercase; color:var(--ink-3); font-weight:600; border-bottom:1px solid var(--line)}
tbody tr{border-bottom:1px solid var(--line)}
tbody tr:last-child{border-bottom:none}
tbody td{font-variant-numeric:tabular-nums}
tr.total td{font-weight:700; border-top:2px solid var(--line); border-bottom:none}
.chip{display:inline-block; font-size:11.5px; font-weight:600; padding:2px 8px; border-radius:7px; font-variant-numeric:tabular-nums}
.chip.good{background:var(--good-soft); color:var(--good)} .chip.bad{background:var(--bad-soft); color:var(--bad)}

/* svg chart */
svg{display:block; width:100%; overflow:visible}
.axis{stroke:var(--line); stroke-width:1}
.gridln{stroke:var(--line); stroke-width:1; stroke-dasharray:2 4; opacity:.7}
.axlab{fill:var(--ink-3); font-size:11px; font-family:var(--sans)}
.dot-end{fill:var(--accent); stroke:var(--surface); stroke-width:2}
.tip{position:fixed; z-index:60; pointer-events:none; background:var(--ink); color:var(--ground);
  font-size:12px; padding:7px 10px; border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.25); opacity:0; transition:opacity .1s; white-space:nowrap}
.tip b{font-variant-numeric:tabular-nums}
.foot{color:var(--ink-3); font-size:12px; text-align:center; margin-top:26px}
.hidden{display:none !important}
.split2{display:grid; grid-template-columns:1fr 1fr; gap:12px}
.posbar{height:26px; border-radius:8px; background:var(--surface-2); position:relative; overflow:hidden; margin-top:6px}
.posbar .mid{position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--ink-3); opacity:.4}
.posbar .fill{position:absolute; top:0; bottom:0; opacity:.85}
</style>
</head>
<body>
<div class="tip" id="tip"></div>
<header>
  <div class="head">
    <div class="brand">
      <div class="dot"></div>
      <div><h1>Mis gastos</h1><small id="genstamp"></small></div>
    </div>
    <div class="msel">
      <label for="mes">Mes</label>
      <select id="mes"></select>
    </div>
    <button class="ghost" id="theme" title="Cambiar tema" aria-label="Cambiar tema">◐</button>
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
const fmt = n => (n<0?"-":"") + "$ " + fmt0(Math.abs(n));
const fmtc = (n,cur) => (n<0?"-":"") + (cur==="USD"?"US$ ":"$ ") + Math.abs(n).toLocaleString("es-UY",{minimumFractionDigits: cur==="USD"?2:0, maximumFractionDigits: cur==="USD"?2:0});
const mesLabel = m => { const [y,mm]=m.split("-"); const N=["","ene","feb","mar","abr","may","jun","jul","ago","set","oct","nov","dic"]; return N[+mm]+" "+y; };
const prevMes = m => D.meses[D.meses.indexOf(m)-1];
const cursoBadge = () => curMes===D.mes_en_curso ? `<span class="pill flat">● en curso · día ${D.dia_corte}/${D.dias_mes}</span>` : "";
const el = (t,c,h)=>{const e=document.createElement(t); if(c)e.className=c; if(h!=null)e.innerHTML=h; return e;};

let curMes = D.mes_default, curTab = "cat";

/* ---------- tooltip ---------- */
const tip = document.getElementById("tip");
function showTip(html,x,y){ tip.innerHTML=html; tip.style.opacity=1; tip.style.left=(x+14)+"px"; tip.style.top=(y+14)+"px"; }
function hideTip(){ tip.style.opacity=0; }

/* ---------- charts (vanilla SVG) ---------- */
function donut(items, total){ // items:[{label,value,cvar}]
  const R=78, r=48, C=100, seg=[]; let a=-Math.PI/2;
  const svg=`<svg viewBox="0 0 200 200" style="max-width:230px;margin:0 auto" role="img" aria-label="Composición del gasto">`;
  let paths="";
  items.forEach((it,i)=>{
    const frac=total>0?it.value/total:0, a2=a+frac*2*Math.PI;
    const x1=C+R*Math.cos(a), y1=C+R*Math.sin(a), x2=C+R*Math.cos(a2), y2=C+R*Math.sin(a2);
    const xi=C+r*Math.cos(a2), yi=C+r*Math.sin(a2), xi2=C+r*Math.cos(a), yi2=C+r*Math.sin(a);
    const large=frac>0.5?1:0;
    paths+=`<path d="M${x1} ${y1} A${R} ${R} 0 ${large} 1 ${x2} ${y2} L${xi} ${yi} A${r} ${r} 0 ${large} 0 ${xi2} ${yi2} Z" fill="${cssvar(it.cvar)}" stroke="var(--surface)" stroke-width="2" data-i="${i}" style="cursor:pointer"/>`;
    a=a2;
  });
  return svg+paths+`<text x="100" y="94" text-anchor="middle" class="axlab" style="font-size:11px">Total mes</text>`+
    `<text x="100" y="116" text-anchor="middle" style="font-family:var(--serif);font-size:22px;fill:var(--ink)">${fmt(total)}</text></svg>`;
}

function lineChart(pts, label){ // pts:[{m,v}]
  const W=560,H=190,pl=44,pr=14,pt=16,pb=28, iw=W-pl-pr, ih=H-pt-pb;
  const vals=pts.map(p=>p.v), max=Math.max(...vals,1)*1.15, min=0;
  const X=i=>pl+(pts.length<2?iw/2:iw*i/(pts.length-1)), Y=v=>pt+ih-(v-min)/(max-min)*ih;
  let grid="",lab="";
  for(let g=0; g<=3; g++){ const v=max*g/3, y=Y(v); grid+=`<line class="gridln" x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}"/>`; lab+=`<text class="axlab" x="${pl-8}" y="${y+4}" text-anchor="end">${fmt0(v/1000)}k</text>`; }
  let dl=pts.map((p,i)=>`${X(i)},${Y(p.v)}`).join(" ");
  const area=`${pl},${pt+ih} `+dl+` ${X(pts.length-1)},${pt+ih}`;
  let xl=pts.map((p,i)=>`<text class="axlab" x="${X(i)}" y="${H-8}" text-anchor="middle">${mesLabel(p.m).slice(0,3)}</text>`).join("");
  let dots=pts.map((p,i)=>`<circle cx="${X(i)}" cy="${Y(p.v)}" r="${i===pts.length-1?5:3.5}" class="${i===pts.length-1?'dot-end':''}" fill="${i===pts.length-1?'':'var(--accent)'}" data-m="${p.m}" data-v="${p.v}" style="cursor:pointer"/>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">
    <defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".28"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    ${grid}<polygon points="${area}" fill="url(#ga)"/>
    <polyline points="${dl}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}${lab}${xl}</svg>`;
}

function hbars(items, opts){ // items:[{lbl,val,delta?}]
  opts=opts||{}; const max=Math.max(...items.map(i=>i.val),1);
  const rows=items.map(it=>{
    let d="";
    if(it.delta!=null && isFinite(it.delta)){ const up=it.delta>0; const cls=up?"up":"down"; const s=up?"▲":"▼";
      if(Math.abs(it.delta)>=1) d=`<span class="d ${cls}">${s} ${fmt0(Math.abs(it.delta))}</span>`; }
    return `<div class="bar-row"><span class="lbl" title="${it.lbl}">${it.lbl}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${it.val/max*100}%"></div></div>
      <span class="amt">${fmt(it.val)}${d}</span></div>`;
  }).join("");
  return `<div class="bars">${rows}</div>`;
}

function divBars(items){ // presupuesto: items:[{lbl,dif}] dif>0 = sobregasto
  const max=Math.max(...items.map(i=>Math.abs(i.dif)),1);
  const rows=items.map(it=>{
    const over=it.dif>0, w=Math.abs(it.dif)/max*50;
    return `<div class="bar-row" style="grid-template-columns:130px 1fr auto">
      <span class="lbl" title="${it.lbl}">${it.lbl}</span>
      <div style="position:relative;height:15px">
        <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--ink-3);opacity:.35"></div>
        <div style="position:absolute;top:0;height:100%;border-radius:5px;${over?`left:50%;width:${w}%;background:var(--bad)`:`right:50%;width:${w}%;background:var(--good)`};opacity:.85"></div>
      </div>
      <span class="amt" style="color:${over?'var(--bad)':'var(--good)'}">${over?'+':''}${fmt(it.dif)}</span></div>`;
  }).join("");
  return `<div class="bars">${rows}</div>`;
}

/* ---------- render tabs ---------- */
function topN(list, n){ // fold categories beyond n into "Otros"
  const s=[...list].sort((a,b)=>b.gasto-a.gasto);
  if(s.length<=n) return s;
  const head=s.slice(0,n), rest=s.slice(n).reduce((t,x)=>t+x.gasto,0);
  return [...head, {cat:"Otros", gasto:rest}];
}
function last6(mes, getter){ const i=D.meses.indexOf(mes); const out=[]; for(let k=Math.max(0,i-5);k<=i;k++){ const m=D.meses[k]; out.push({m, v:getter(m)}); } return out; }

function renderCat(){
  const wrap=document.getElementById("tab-cat"); wrap.innerHTML="";
  const list=D.personal[curMes]||[]; const total=list.reduce((t,x)=>t+x.gasto,0);
  const pm=prevMes(curMes); const plist=pm?(D.personal[pm]||[]):[]; const ptotal=plist.reduce((t,x)=>t+x.gasto,0);
  const pmap={}; plist.forEach(x=>pmap[x.cat]=x.gasto);
  const delta=total-ptotal, dpct=ptotal>0?delta/ptotal*100:0;
  const trend=last6(curMes,m=>(D.personal[m]||[]).reduce((t,x)=>t+x.gasto,0));
  const avg=trend.reduce((t,x)=>t+x.v,0)/(trend.length||1);

  // KPIs
  const kp=el("div","kpis");
  const enCurso = curMes===D.mes_en_curso;
  let k1extra;
  if(enCurso){ const proy = D.dia_corte>0? total/D.dia_corte*D.dias_mes : total;
    k1extra = `<span class="pill flat">● en curso · día ${D.dia_corte}/${D.dias_mes}</span><div class="sub" style="margin:7px 0 0;font-size:12px;color:var(--ink-3)">proyección fin de mes ≈ ${fmt(proy)}</div>`;
  } else { k1extra = pm?`<span class="pill ${delta>0?'up':delta<0?'down':'flat'}">${delta>0?'▲':delta<0?'▼':'—'} ${fmt0(Math.abs(delta))} (${dpct>0?'+':''}${dpct.toFixed(0)}%)</span>`:""; }
  kp.appendChild(el("div","kpi",`<div class="lab">Gastado en ${mesLabel(curMes)}</div><div class="val tnum">${fmt(total)}</div>${k1extra}`));
  kp.appendChild(el("div","kpi",`<div class="lab">vs mes anterior</div><div class="val tnum">${pm?fmt(ptotal):'—'}</div><div class="sub" style="margin:6px 0 0;font-size:12px;color:var(--ink-3)">${pm?mesLabel(pm):'sin dato previo'}</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Promedio 6 meses</div><div class="val tnum">${fmt(avg)}</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Categorías</div><div class="val tnum">${list.length}</div>`));
  wrap.appendChild(kp);

  // saldos
  const sc=el("div","card");
  sc.appendChild(el("div",null,`<h3>Saldos estimados</h3><div class="sub">Desde tu último saldo real (${D.seed_fecha.split("-").reverse().join("/")}) + movimientos. Aproximado hasta el cierre mensual.</div>`));
  const sg=el("div","saldos");
  D.saldos.forEach(s=>{
    sg.appendChild(el("div","acct",`<div class="n">${s.cuenta} <span class="tag">${s.grupo==='casa'?'CASA':'PERSONAL'}</span></div><div class="s tnum">${fmtc(s.saldo,s.moneda)}</div><div class="cur">${s.moneda}</div>`));
  });
  const pos=D.posicion;
  sg.appendChild(el("div","acct",`<div class="n">Posición de casa <span class="tag">vs LU</span></div><div class="s tnum" style="color:${pos.joaco>=0?'var(--good)':'var(--bad)'}">${pos.joaco>=0?'+':''}${fmt(pos.joaco)}</div><div class="cur">${pos.joaco>=0?'aportaste de más':'aportaste de menos'} · Lu ${pos.lu>=0?'+':''}${fmt0(pos.lu)}</div>`));
  sc.appendChild(sg); wrap.appendChild(sc);

  // charts row
  const row=el("div","cols");
  const dcard=el("div","card"); const items=topN(list,7).map((x,i)=>({label:x.cat,value:x.gasto,cvar:CAT_COLORS[i%8]}));
  dcard.appendChild(el("div",null,`<h3>Composición del mes</h3><div class="sub">En qué se fue tu gasto personal</div>`));
  dcard.appendChild(el("div",null,donut(items,total)));
  const lg=el("div","legend"); items.forEach(it=>lg.appendChild(el("div","it",`<span class="sw" style="background:${cssvar(it.cvar)}"></span>${it.label}<span class="v">${fmt(it.value)}</span>`))); dcard.appendChild(lg);
  row.appendChild(dcard);

  const bcard=el("div","card");
  bcard.appendChild(el("div",null,`<h3>Por categoría</h3><div class="sub">Con variación vs ${pm?mesLabel(pm):'—'}</div>`));
  const bitems=[...list].sort((a,b)=>b.gasto-a.gasto).map(x=>({lbl:x.cat,val:x.gasto,delta:pm?(x.gasto-(pmap[x.cat]||0)):null}));
  bcard.appendChild(el("div",null,hbars(bitems))); row.appendChild(bcard);
  wrap.appendChild(row);

  // trend
  const tc=el("div","card");
  tc.appendChild(el("div",null,`<h3>Tendencia</h3><div class="sub">Gasto personal · últimos 6 meses</div>`));
  tc.appendChild(el("div","tscroll",lineChart(trend,"Tendencia de gasto personal"))); wrap.appendChild(tc);

  wireDonut(dcard, items, total); wireLine(tc);
}

function renderPre(){
  const wrap=document.getElementById("tab-pre"); wrap.innerHTML="";
  const list=(D.presupuesto[curMes]||[]).filter(x=>x.ppto>0||x.real>0);
  const tp=list.reduce((t,x)=>t+x.ppto,0), tr=list.reduce((t,x)=>t+x.real,0), td=tr-tp;
  const kp=el("div","kpis"); kp.style.gridTemplateColumns="repeat(3,1fr)";
  kp.appendChild(el("div","kpi",`<div class="lab">Presupuesto ${mesLabel(curMes)}</div><div class="val tnum">${fmt(tp)}</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Gastado</div><div class="val tnum">${fmt(tr)}</div>`));
  const preBadge = curMes===D.mes_en_curso ? cursoBadge() : `<span class="pill ${td>0?'up':'down'}">${td>0?'sobre presupuesto':'dentro'}</span>`;
  kp.appendChild(el("div","kpi",`<div class="lab">Diferencia</div><div class="val tnum" style="color:${td>0?'var(--bad)':'var(--good)'}">${td>0?'+':''}${fmt(td)}</div>${preBadge}`));
  wrap.appendChild(kp);

  const dc=el("div","card");
  dc.appendChild(el("div",null,`<h3>Variación por categoría</h3><div class="sub">Izquierda = ahorraste · Derecha = te pasaste</div>`));
  const dv=list.filter(x=>Math.abs(x.dif)>=1).sort((a,b)=>b.dif-a.dif).map(x=>({lbl:x.cat,dif:x.dif}));
  dc.appendChild(el("div",null,divBars(dv))); wrap.appendChild(dc);

  const tcard=el("div","card");
  tcard.appendChild(el("div",null,`<h3>Detalle</h3><div class="sub">Presupuesto vs real del mes</div>`));
  let rows=list.sort((a,b)=>b.real-a.real).map(x=>{
    const pct=x.ppto>0?(x.real/x.ppto*100):(x.real>0?999:0);
    const over=x.dif>0;
    return `<tr><td>${x.cat}</td><td>${fmt(x.ppto)}</td><td>${fmt(x.real)}</td>
      <td><span class="chip ${over?'bad':'good'}">${over?'+':''}${fmt0(x.dif)}</span></td>
      <td>${x.ppto>0?pct.toFixed(0)+'%':'—'}</td></tr>`;
  }).join("");
  const tbl=`<div class="tscroll"><table><thead><tr><th>Categoría</th><th>Presupuesto</th><th>Real</th><th>Diferencia</th><th>% usado</th></tr></thead>
    <tbody>${rows}<tr class="total"><td>Total</td><td>${fmt(tp)}</td><td>${fmt(tr)}</td><td style="color:${td>0?'var(--bad)':'var(--good)'}">${td>0?'+':''}${fmt0(td)}</td><td>${tp>0?(tr/tp*100).toFixed(0)+'%':'—'}</td></tr></tbody></table></div>`;
  tcard.appendChild(el("div",null,tbl)); wrap.appendChild(tcard);
}

function renderCasa(){
  const wrap=document.getElementById("tab-casa"); wrap.innerHTML="";
  const c=D.casa[curMes]||{total:0,lu:0,joaco:0,cats:[]};
  const kp=el("div","kpis"); kp.style.gridTemplateColumns="repeat(3,1fr)";
  kp.appendChild(el("div","kpi",`<div class="lab">Gasto de casa ${mesLabel(curMes)}</div><div class="val tnum">${fmt(c.total)}</div>${cursoBadge()}`));
  kp.appendChild(el("div","kpi",`<div class="lab">Tu parte</div><div class="val tnum">${fmt(c.joaco)}</div><div class="sub" style="margin:6px 0 0;font-size:12px;color:var(--ink-3)">${c.total>0?(c.joaco/c.total*100).toFixed(0):0}% del total</div>`));
  kp.appendChild(el("div","kpi",`<div class="lab">Parte de Lu</div><div class="val tnum">${fmt(c.lu)}</div><div class="sub" style="margin:6px 0 0;font-size:12px;color:var(--ink-3)">${c.total>0?(c.lu/c.total*100).toFixed(0):0}% del total</div>`));
  wrap.appendChild(kp);

  // posicion acumulada
  const pc=el("div","card"); const pos=D.posicion;
  const maxp=Math.max(Math.abs(pos.lu),Math.abs(pos.joaco),1);
  pc.appendChild(el("div",null,`<h3>Posición acumulada</h3><div class="sub">Cuenta continua (sin transferencias de ajuste) · cierre ${pos.mes?mesLabel(pos.mes):''}. Positivo = aportaste de más.</div>`));
  const mkpos=(nom,v)=>`<div style="flex:1"><div style="display:flex;justify-content:space-between;font-size:13px"><span>${nom}</span><b class="tnum" style="color:${v>=0?'var(--good)':'var(--bad)'}">${v>=0?'+':''}${fmt(v)}</b></div>
    <div class="posbar"><div class="mid"></div><div class="fill" style="${v>=0?`left:50%;width:${Math.abs(v)/maxp*50}%;background:var(--good)`:`right:50%;width:${Math.abs(v)/maxp*50}%;background:var(--bad)`}"></div></div></div>`;
  pc.appendChild(el("div",null,`<div style="display:flex;gap:24px;flex-wrap:wrap">${mkpos("Vos (Joaco)",pos.joaco)}${mkpos("Lu",pos.lu)}</div>`));
  wrap.appendChild(pc);

  // split por categoria (grouped) + evolucion
  const row=el("div","cols");
  const scard=el("div","card");
  scard.appendChild(el("div",null,`<h3>Reparto por categoría</h3><div class="sub"><span style="color:${cssvar('--c1')}">■</span> Vos &nbsp; <span style="color:${cssvar('--c2')}">■</span> Lu</div>`));
  const maxc=Math.max(...c.cats.map(x=>x.total),1);
  const sb=c.cats.map(x=>`<div class="bar-row" style="grid-template-columns:130px 1fr auto"><span class="lbl" title="${x.cat}">${x.cat}</span>
    <div class="bar-track" style="display:flex;height:15px;gap:2px;background:none"><div style="width:${x.joaco/maxc*100}%;background:${cssvar('--c1')};border-radius:5px 0 0 5px"></div><div style="width:${x.lu/maxc*100}%;background:${cssvar('--c2')};border-radius:0 5px 5px 0"></div></div>
    <span class="amt">${fmt(x.total)}</span></div>`).join("");
  scard.appendChild(el("div","bars",sb)); row.appendChild(scard);

  const ec=el("div","card");
  ec.appendChild(el("div",null,`<h3>Evolución</h3><div class="sub">Gasto de casa · últimos 6 meses</div>`));
  const tr=last6(curMes,m=>(D.casa[m]||{total:0}).total);
  ec.appendChild(el("div","tscroll",lineChart(tr,"Evolución del gasto de casa"))); row.appendChild(ec);
  wrap.appendChild(row);
  wireLine(ec);
}

/* ---------- interactivity wiring ---------- */
function wireDonut(card, items, total){
  card.querySelectorAll("path[data-i]").forEach(p=>{
    p.addEventListener("mousemove",e=>{const it=items[+p.dataset.i]; showTip(`${it.label}<br><b>${fmt(it.value)}</b> · ${(it.value/total*100).toFixed(1)}%`,e.clientX,e.clientY);});
    p.addEventListener("mouseleave",hideTip);
  });
}
function wireLine(card){
  card.querySelectorAll("circle[data-m]").forEach(c=>{
    c.addEventListener("mousemove",e=>showTip(`${mesLabel(c.dataset.m)}<br><b>${fmt(+c.dataset.v)}</b>`,e.clientX,e.clientY));
    c.addEventListener("mouseleave",hideTip);
  });
}

/* ---------- shell ---------- */
function renderAll(){ renderCat(); renderPre(); renderCasa(); }
function selectTab(t){ curTab=t;
  document.querySelectorAll(".tab").forEach(b=>b.setAttribute("aria-selected",b.dataset.tab===t));
  ["cat","pre","casa"].forEach(x=>document.getElementById("tab-"+x).classList.toggle("hidden",x!==t));
}
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>selectTab(b.dataset.tab)));

const sel=document.getElementById("mes");
D.meses.slice().reverse().forEach(m=>{const o=el("option"); o.value=m; o.textContent=mesLabel(m); sel.appendChild(o);});
sel.value=curMes; sel.addEventListener("change",()=>{curMes=sel.value; renderAll();});

// theme toggle (para archivo local; en el host de artifacts el toggle propio también sirve)
const root=document.documentElement;
document.getElementById("theme").addEventListener("click",()=>{
  const cur=root.getAttribute("data-theme")|| (matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light");
  root.setAttribute("data-theme", cur==="dark"?"light":"dark"); renderAll();
});
document.getElementById("genstamp").textContent = D.generado?("actualizado "+D.generado):"";
document.getElementById("foot").textContent = "Generado por el pipeline · datos al "+(D.generado||"");

renderAll();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
