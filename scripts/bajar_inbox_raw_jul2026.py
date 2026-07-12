# -*- coding: utf-8 -*-
"""
Backfill jul-2026: materializa data/inbox_raw/<message_id>.json a partir de los
dumps crudos de get_thread/get_message del conector Gmail (MCP).

La descarga desde Gmail la hace Claude via MCP (ver docs/pipeline-mails.md).
Los resultados crudos de esas llamadas quedan capturados en:
  1. el transcript de la sesion (JSONL) — resultados inline, y
  2. el directorio tool-results de la sesion — resultados grandes persistidos.

Este script recorre ambas fuentes, junta cada mensaje por message_id segun el
MANIFEST (universo exacto devuelto por las 4 busquedas del backfill, sin
marketing), y escribe un JSON por MENSAJE:
  {"message_id","thread_id","fecha_mail","from","subject","body_text"}
body_text = plaintextBody si la herramienta lo devolvio; si no, htmlBody
renderizado a texto plano (parser HTML estandar, sin dependencias).

Uso:
  python bajar_inbox_raw_jul2026.py [--session-dir DIR] [--out DIR]
"""
import argparse
import io
import json
import os
import re
import sys
from html.parser import HTMLParser

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# MANIFEST del backfill jul-2026 (after:2026/06/30, congelado 2026-07-12).
# busqueda 1: from:notificaciones@santander.com.uy
# busqueda 2: recibos Apple/Google/Meli/MP (sin marketing)
# busqueda 3: facturas Antel/DirecTV -> 0 resultados
# busqueda 4: estados de cuenta (Santander MC / BBVA)
# Marketing salteado (busqueda 2): 19f4c93109b05ae2 (cambio condiciones Google
# Play), 19f47dc85cbe48d9 (promo Meli+ envios gratis).
# thread_id -> (busqueda, [message_ids])
MANIFEST = {
    "19f4cc50713be2b9": (1, ["19f4cc50713be2b9"]),
    "19f4784a7c042a72": (1, ["19f4784a7c042a72"]),
    "19f445a43684d79f": (1, ["19f445a43684d79f"]),
    "19f44033457195fa": (1, ["19f44033457195fa", "19f465b363be7949"]),
    "19f438b63c1805e8": (1, ["19f438b63c1805e8", "19f43b203b6b8b42",
                              "19f43b489c608f2f", "19f4438ae31bede9",
                              "19f44e1dec5f6d1d"]),
    "19f38502b7c18bb0": (1, ["19f38502b7c18bb0"]),
    "19f34b8d77cfb1bf": (1, ["19f34b8d77cfb1bf", "19f34d4279af5f5b",
                              "19f358f2f39664d1"]),
    "19f33b6377cc5be5": (1, ["19f33b6377cc5be5"]),
    "19f2e704c046e913": (1, ["19f2e704c046e913"]),
    "19f2e6bbeffc5abc": (1, ["19f2e6bbeffc5abc"]),
    "19f2e5e5c9fc5eb5": (1, ["19f2e5e5c9fc5eb5"]),
    "19f2de450ebb16f2": (1, ["19f2de450ebb16f2", "19f2dede8df3ed86",
                              "19f2df63e026ff24", "19f2e01153573e3a",
                              "19f2e1a332dd299e"]),
    "19f2d724ca7a73f5": (1, ["19f2d724ca7a73f5", "19f2d7e47451b37a"]),
    "19f2895c2c4bf5af": (1, ["19f2895c2c4bf5af"]),
    "19f2563d431a6f60": (1, ["19f2563d431a6f60", "19f2605580da913e",
                              "19f27c35c2b77ce0", "19f28065b7fa79e0",
                              "19f2a11e53d1151c"]),
    "19f1ef221dc9e335": (1, ["19f1ef221dc9e335", "19f1fd1618659ed1",
                              "19f1fe09f7628dce", "19f2003ab0b02f41",
                              "19f2003eeee734dc"]),
    "19f1bf6bc007a2d4": (1, ["19f1bf6bc007a2d4", "19f1e0a3bdedcd85"]),
    "19f1b25dd3bc6509": (1, ["19f1b25dd3bc6509", "19f1b5e1a7a3a034"]),
    "19f18100c7737d66": (1, ["19f18100c7737d66"]),
    "19f232bbb569d81d": (2, ["19f232bbb569d81d"]),
    "19f1ea948535c030": (2, ["19f1ea948535c030"]),
    "19f3ff031a0a6abe": (4, ["19f3ff031a0a6abe"]),
}

MSG2THREAD = {m: t for t, (_, ms) in MANIFEST.items() for m in ms}
MSG2SEARCH = {m: s for t, (s, ms) in MANIFEST.items() for m in ms}

BLOCK_TAGS = {"div", "p", "tr", "td", "th", "table", "li", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "header", "footer", "blockquote", "pre", "title"}
SKIP_TAGS = {"style", "script", "head", "meta", "link"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_a_texto(html):
    p = _Text()
    p.feed(html)
    raw = "".join(p.parts)
    lines = []
    for ln in raw.split("\n"):
        ln = ln.strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def registros_de_objeto(obj):
    """Devuelve lista de (thread_id, message_dict) desde un dump crudo."""
    out = []
    if not isinstance(obj, dict):
        return out
    if "messages" in obj and isinstance(obj["messages"], list):
        tid = obj.get("id", "")
        for m in obj["messages"]:
            if isinstance(m, dict) and "id" in m:
                out.append((tid, m))
    elif ("htmlBody" in obj or "plaintextBody" in obj) and "id" in obj:
        out.append((obj.get("threadId", ""), obj))
    return out


def try_parse(texto):
    try:
        return json.loads(texto)
    except (ValueError, TypeError):
        return None


def recolectar(session_dir):
    """Junta message_id -> (thread_id, message_dict) de transcript+tool-results."""
    encontrados = {}

    def absorber(obj):
        for tid, m in registros_de_objeto(obj):
            mid = m["id"]
            if mid in MSG2THREAD and mid not in encontrados:
                encontrados[mid] = (tid or MSG2THREAD[mid], m)

    tr_dir = os.path.join(session_dir, "tool-results")
    if os.path.isdir(tr_dir):
        for fn in os.listdir(tr_dir):
            if not fn.endswith(".txt"):
                continue
            with open(os.path.join(tr_dir, fn), encoding="utf-8",
                      errors="replace") as f:
                absorber(try_parse(f.read()))

    # transcripts: el de la sesion (hermano del dir) + subagentes anidados
    transcripts = []
    hermano = session_dir.rstrip("\\/") + ".jsonl"
    if os.path.isfile(hermano):
        transcripts.append(hermano)
    for raiz, _dirs, files in os.walk(session_dir):
        for fn in files:
            if fn.endswith(".jsonl"):
                transcripts.append(os.path.join(raiz, fn))
    for jsonl in transcripts:
        with open(jsonl, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "htmlBody" not in line and "plaintextBody" not in line:
                    continue
                rec = try_parse(line)
                if rec is None:
                    continue
                pila = [rec]
                while pila:
                    x = pila.pop()
                    if isinstance(x, dict):
                        pila.extend(x.values())
                    elif isinstance(x, list):
                        pila.extend(x)
                    elif isinstance(x, str) and ("htmlBody" in x
                                                 or "plaintextBody" in x):
                        absorber(try_parse(x))
    return encontrados


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-dir", default=os.path.join(
        os.path.expanduser("~"), ".claude", "projects",
        "C--Users-joaco-Desktop-proyectos-claude-gastos",
        "53ed7a45-776a-4acd-92c4-385125e84007"))
    ap.add_argument("--out", default=os.path.join(base, "data", "inbox_raw"))
    args = ap.parse_args()

    encontrados = recolectar(args.session_dir)
    os.makedirs(args.out, exist_ok=True)

    guardados, faltantes = [], []
    for mid in MSG2THREAD:
        if mid not in encontrados:
            faltantes.append(mid)
            continue
        tid, m = encontrados[mid]
        body = m.get("plaintextBody") or html_a_texto(m.get("htmlBody", ""))
        rec = {
            "message_id": mid,
            "thread_id": tid,
            "fecha_mail": m.get("date", ""),
            "from": m.get("sender", ""),
            "subject": (m.get("subject") or "").strip(),
            "body_text": body,
        }
        destino = os.path.join(args.out, mid + ".json")
        with open(destino, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        guardados.append(rec)

    # ---- reporte de control -------------------------------------------------
    por_busqueda = {}
    for r in guardados:
        por_busqueda.setdefault(MSG2SEARCH[r["message_id"]], []).append(r)
    print("GUARDADOS_TOTAL=%d" % len(guardados))
    for s in sorted(por_busqueda):
        print("BUSQUEDA_%d=%d" % (s, len(por_busqueda[s])))
    print("FALTANTES=%s" % (",".join(faltantes) if faltantes else "ninguno"))

    subj = {}
    tarjetas = {}
    ejemplos = {}
    for r in guardados:
        st = r["subject"]
        subj[st] = subj.get(st, 0) + 1
        ejemplos.setdefault(st, []).append(r["message_id"])
        mt = re.search(r"terminada en\s+(\d{4})", r["body_text"])
        if mt:
            tarjetas[mt.group(1)] = tarjetas.get(mt.group(1), 0) + 1
    print("--- por asunto ---")
    for k in sorted(subj):
        print("%s => %d | ej: %s" % (k, subj[k], ",".join(ejemplos[k][:3])))
    print("--- por tarjeta (desde cuerpos) ---")
    for k in sorted(tarjetas):
        print("tarjeta_%s=%d" % (k, tarjetas[k]))
    fechas = sorted(r["fecha_mail"] for r in guardados if r["fecha_mail"])
    if fechas:
        print("RANGO_FECHAS=%s -> %s" % (fechas[0], fechas[-1]))
    ok = not faltantes and len(guardados) == len(MSG2THREAD)
    print("OK=%s" % ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
