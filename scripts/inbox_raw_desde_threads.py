# -*- coding: utf-8 -*-
"""Convierte respuestas crudas de get_thread (MCP Gmail, FULL_CONTENT) en
data/inbox_raw/<message_id>.json — un archivo por MENSAJE (contrato de
docs/pipeline-mails.md: message_id, thread_id, fecha_mail, from, subject, body_text).

Uso: python scripts/inbox_raw_desde_threads.py <thread.json> [<thread2.json> ...]

Cada <thread.json> es la respuesta literal del tool get_thread:
  {"id": "<threadId>", "messages": [{"id","date","sender","subject","htmlBody"...}]}

Si el mensaje solo trae htmlBody se renderiza a texto plano: se descartan
<style>/<script>/<head> y la leyenda de privacidad de Santander; se preservan
los espacios internos de los valores (ej. establecimiento crudo con VIS3).
Idempotente: no pisa archivos existentes.
"""
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "data" / "inbox_raw"
BLOQUES = {"div", "p", "td", "tr", "table", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul"}
MARCAS_CORTE = ("PRIVACIDAD DE ESTE MENSAJE", "-----------------------------------------")


class Render(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []
        self.saltar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.saltar += 1
        if tag == "br":
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self.saltar:
            self.saltar -= 1
        if tag in BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if self.saltar:
            return
        lineas = [l.strip() for l in data.split("\n")]
        texto = " ".join(l for l in lineas if l)
        if texto:
            self.partes.append(texto + " ")


def html_a_texto(html: str) -> str:
    r = Render()
    r.feed(html)
    lineas = []
    for linea in "".join(r.partes).split("\n"):
        linea = linea.strip()
        if not linea:
            continue
        if any(m in linea for m in MARCAS_CORTE):
            break
        lineas.append(linea)
    return "\n".join(lineas)


def cuerpo(msg: dict) -> str:
    for campo in ("plaintextBody", "plainTextBody", "textBody"):
        if msg.get(campo):
            return msg[campo]
    if msg.get("htmlBody"):
        return html_a_texto(msg["htmlBody"])
    return ""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("uso: inbox_raw_desde_threads.py <thread.json> [...]")
    DESTINO.mkdir(parents=True, exist_ok=True)
    nuevos = existentes = 0
    for arg in sys.argv[1:]:
        data = json.loads(Path(arg).read_text(encoding="utf-8"))
        thread_id = data["id"]
        for m in data.get("messages", []):
            body = cuerpo(m)
            if not body:
                raise SystemExit(f"mensaje {m.get('id', '?')}: sin cuerpo")
            out = DESTINO / f"{m['id']}.json"
            if out.exists():
                existentes += 1
                continue
            payload = {
                "message_id": m["id"],
                "thread_id": thread_id,
                "fecha_mail": m["date"],
                "from": m["sender"],
                "subject": m["subject"],
                "body_text": body,
            }
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            nuevos += 1
    total = len(list(DESTINO.glob("*.json")))
    print(f"nuevos={nuevos} ya_existian={existentes} total_en_carpeta={total}")


if __name__ == "__main__":
    main()
