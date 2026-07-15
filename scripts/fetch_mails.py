"""
fetch_mails.py — Descarga determinística de mails financieros desde la API de Gmail.

Reemplaza el fetch que antes hacía Claude headless, que tenía 3 problemas:
  1. Perdía mails: enumeraba por thread (search_threads no devuelve todos los
     mensajes de un thread). Acá se enumera por MENSAJE (users.messages.list):
     el bug es estructuralmente imposible.
  2. Rendía el cuerpo distinto entre corridas (2 layouts) -> crasheaba el parser.
     Acá html_a_texto() es determinístico: mismo HTML -> mismo body_text, siempre.
  3. Ataba el pipeline a la PC (conector interactivo). Acá corre headless con un
     refresh token (GitHub Actions).

Idempotente: no re-descarga lo que ya está en data/inbox_raw/ ni en procesados.csv.
Fail-safe: baja todo mail de remitente financiero conocido salvo la denylist
explícita (config/reglas_fetch.csv). Lo desconocido se baja igual y procesar_mails.py
lo deja pendiente — nunca se pierde en silencio.

No es dueño del watermark: solo LEE config/estado_sync.json (procesar_mails.py lo escribe).

Auth (en orden): env GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN (GitHub Actions), o
                 config/gmail_token.json (corridas locales; lo genera get_gmail_token.py).
"""
import os
import sys
import csv
import json
import base64
import re
import datetime as dt
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def P(*a):
    return os.path.join(BASE, *a)


INBOX = P("data", "inbox_raw")
PROCESADOS = P("data", "staging", "procesados.csv")
ESTADO_SYNC = P("config", "estado_sync.json")
REGLAS_FETCH = P("config", "reglas_fetch.csv")
TOKEN_FILE = P("config", "gmail_token.json")

DEFAULT_AFTER = "2026/06/30"   # si no hay watermark
OVERLAP_DAYS = 2               # solapamiento seguro; la idempotencia absorbe repetidos


def creds_gmail():
    cid = os.environ.get("GMAIL_CLIENT_ID")
    csec = os.environ.get("GMAIL_CLIENT_SECRET")
    rtok = os.environ.get("GMAIL_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        if not os.path.exists(TOKEN_FILE):
            print(f"ERROR: faltan credenciales Gmail (env vars GMAIL_* o {TOKEN_FILE}).",
                  file=sys.stderr)
            sys.exit(2)
        with open(TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        cid, csec, rtok = d["client_id"], d["client_secret"], d["refresh_token"]
    creds = Credentials(None, refresh_token=rtok,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=cid, client_secret=csec, scopes=SCOPES)
    creds.refresh(Request())
    return creds


def cargar_reglas_fetch():
    senders, denylist = [], []
    with open(REGLAS_FETCH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tipo, val = row["tipo"].strip(), row["valor"].strip()
            if not val:
                continue
            if tipo == "sender":
                senders.append(val)
            elif tipo == "denylist_subject":
                denylist.append(val)
    return senders, denylist


def leer_after():
    if not os.path.exists(ESTADO_SYNC):
        return DEFAULT_AFTER
    with open(ESTADO_SYNC, encoding="utf-8") as f:
        est = json.load(f)
    iso = est.get("ultima_fecha_mail")
    if not iso:
        return DEFAULT_AFTER
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).date() - dt.timedelta(days=OVERLAP_DAYS)
    return d.strftime("%Y/%m/%d")


BLOCK_TAGS = {"p", "div", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4",
              "h5", "h6", "td", "th", "section", "article", "header", "footer",
              "blockquote"}


def html_a_texto(html):
    """HTML del mail -> texto plano determinístico, UNA línea por bloque (celda/fila),
    elementos inline unidos por espacio, SIN líneas en blanco.

    Extrae a nivel de bloque (no por nodo de texto) para no partir etiquetas como
    'VISA terminada en' cuyo interior tiene spans inline; y elimina líneas en blanco
    para que etiqueta y valor queden adyacentes. Así funcionan los dos parsers de
    procesar_mails.py: campo_bloque() (etiqueta\\nvalor\\nsiguiente en avisos) y
    campos_transferencia() (etiqueta seguida del valor en la línea siguiente)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["style", "script", "head", "title"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.append("\n")                  # salto de línea al cierre de cada bloque
    txt = soup.get_text(separator=" ")    # inline unido por espacio
    out = []
    for ln in txt.split("\n"):
        # Colapsa runs de espacio/tab/nbsp a uno solo. Necesario: get_text(separator=" ")
        # inserta un espacio entre nodos inline, generando "VISA  terminada en" (doble)
        # que si no rompe el match literal de la etiqueta en campo_bloque(). Efecto
        # colateral benigno: el padding del establecimiento crudo (campo comercio) queda
        # normalizado a un espacio — descripcion/categoria/monto/ambito no cambian.
        ln = re.sub(r"[ \t\xa0]+", " ", ln).strip()
        if ln:                            # descarta líneas vacías
            out.append(ln)
    return "\n".join(out)


def ids_ya_vistos():
    vistos = set()
    if os.path.isdir(INBOX):
        vistos |= {fn[:-5] for fn in os.listdir(INBOX) if fn.endswith(".json")}
    if os.path.exists(PROCESADOS):
        with open(PROCESADOS, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                mid = (row.get("message_id") or "").strip()
                if mid:
                    vistos.add(mid)
    return vistos


def header(payload, name):
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def extraer_cuerpo(payload):
    """Recorre las partes MIME y devuelve (html, plain)."""
    html = plain = ""

    def walk(p):
        nonlocal html, plain
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data.encode("utf-8"))
            txt = raw.decode("utf-8", errors="replace")
            if mt == "text/html" and not html:
                html = txt
            elif mt == "text/plain" and not plain:
                plain = txt
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return html, plain


def fecha_mail_iso(msg):
    ms = msg.get("internalDate")
    if ms:
        return (dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    try:
        return (parsedate_to_datetime(header(msg["payload"], "Date"))
                .astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    except Exception:
        return ""


def main():
    senders, denylist = cargar_reglas_fetch()
    after = leer_after()
    svc = build("gmail", "v1", credentials=creds_gmail(), cache_discovery=False)

    query = "(" + " OR ".join(f"from:{s}" for s in senders) + f") after:{after}"

    # Enumerar por MENSAJE (no por thread).
    ids = []
    req = svc.users().messages().list(userId="me", q=query, maxResults=500)
    while req is not None:
        resp = req.execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        req = svc.users().messages().list_next(req, resp)

    vistos = ids_ya_vistos()
    nuevos = [i for i in ids if i not in vistos]

    os.makedirs(INBOX, exist_ok=True)
    deny_re = [re.compile(p, re.I) for p in denylist]
    guardados, saltados = 0, []
    for mid in nuevos:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg["payload"]
        subject = header(payload, "Subject")
        if any(r.search(subject) for r in deny_re):
            saltados.append((mid, subject))
            continue
        html, plain = extraer_cuerpo(payload)
        body_text = html_a_texto(html) if html else plain.strip()
        rec = {
            "message_id": mid,
            "thread_id": msg.get("threadId", ""),
            "fecha_mail": fecha_mail_iso(msg),
            "from": parseaddr(header(payload, "From"))[1],
            "subject": subject,
            "body_text": body_text,
        }
        with open(os.path.join(INBOX, f"{mid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        guardados += 1

    print(f"fetch_mails: after:{after} | {len(ids)} mensajes en Gmail | "
          f"{len(nuevos)} no vistos | {guardados} guardados | {len(saltados)} saltados (denylist)")
    for mid, subj in saltados:
        print(f"  skip {mid}: {subj!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
