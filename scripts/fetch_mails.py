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
ESTADOS = P("data", "estados")
PROCESADOS = P("data", "staging", "procesados.csv")
ESTADO_SYNC = P("config", "estado_sync.json")
REGLAS_FETCH = P("config", "reglas_fetch.csv")
TOKEN_FILE = P("config", "gmail_token.json")

DEFAULT_AFTER = "2026/06/30"   # si no hay watermark
OVERLAP_DAYS = 2               # solapamiento seguro; la idempotencia absorbe repetidos
ESTADOS_LOOKBACK_DIAS = 90     # ventana para estados de cuenta (son mensuales)

# Reintentos ante errores transitorios de la API (5xx, rate limit). googleapiclient
# los hace con backoff exponencial. Sin esto, un 503 de Google tiraba la corrida
# entera y los mails esperaban hasta la siguiente (auditoria 2026-07 B4).
REINTENTOS = 3


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
    """-> (senders, denylist, estado_senders). Los `sender_estado` son remitentes de
    estados de cuenta: se bajan como cualquier mail Y ademas se les guarda el PDF
    adjunto en data/estados/ (es la unica via para la Mastercard y la BBVA, que no
    mandan avisos por transaccion)."""
    senders, denylist, estado_senders = [], [], []
    with open(REGLAS_FETCH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tipo, val = row["tipo"].strip(), row["valor"].strip()
            if not val:
                continue
            if tipo == "sender":
                senders.append(val)
            elif tipo == "sender_estado":
                senders.append(val)
                estado_senders.append(val)
            elif tipo == "denylist_subject":
                denylist.append(val)
    return senders, denylist, estado_senders


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


def decodificar(raw, content_type):
    """Bytes de una parte MIME -> str.

    UTF-8 PRIMERO, aunque el header declare otra cosa. Los mails de Mercado
    Libre y Mercado Pago declaran charset=iso-8859-1 y mandan UTF-8 igual:
    verificado el 16/7 sobre los mails reales ya bajados, donde los acentos
    llegan perfectos (U+00E1, U+00F3...) justamente porque hoy se decodifica
    utf-8 a la fuerza. Confiar en el charset declarado los ROMPERIA: "ó"
    (bytes C3 B3) leido como iso-8859-1 da "Ã³".

    El fallback al charset del header cubre el caso que la auditoria B5 queria
    atacar -- un remitente que de verdad mande iso-8859-1 -- sin tocar lo que
    hoy funciona: si los bytes son UTF-8 validos, gana UTF-8 y el body_text
    sale identico (invariante de paridad, CLAUDE.md).
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    m = re.search(r'charset="?([\w-]+)"?', content_type or "", re.I)
    candidatos = ([m.group(1)] if m else []) + ["cp1252", "latin-1"]
    for cs in candidatos:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")   # inalcanzable: latin-1 nunca falla


def extraer_cuerpo(payload):
    """Recorre las partes MIME y devuelve (html, plain)."""
    html = plain = ""

    def walk(p):
        nonlocal html, plain
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data.encode("utf-8"))
            txt = decodificar(raw, header(p, "Content-Type"))
            if mt == "text/html" and not html:
                html = txt
            elif mt == "text/plain" and not plain:
                plain = txt
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return html, plain


def emisor_slug(frm):
    f = frm.lower()
    if "bbva" in f:
        return "bbva"
    if "santander" in f:
        return "santander"
    if "itau" in f:
        return "itau"
    return re.sub(r"[^a-z0-9]+", "", f.split("@")[-1].split(".")[0])[:12] or "otro"


def bajar_estados_pdf(svc, estado_senders, dias=ESTADOS_LOOKBACK_DIAS):
    """Guarda en data/estados/ los PDF adjuntos de los mails de estado de cuenta.

    Ventana propia (90d) y NO el watermark: los estados son mensuales, con la
    ventana de 2 dias del fetch solo se cazarian el dia exacto que llegan.
    Idempotente por nombre de archivo, e independiente del skip de mensajes ya
    vistos: si el mail ya estaba en inbox_raw pero el PDF falta, igual lo baja.
    El nombre lleva fecha + emisor porque BBVA manda siempre 'EC Visa .pdf'
    (sin prefijo se pisarian los meses)."""
    if not estado_senders:
        return []
    os.makedirs(ESTADOS, exist_ok=True)
    after = (dt.date.today() - dt.timedelta(days=dias)).strftime("%Y/%m/%d")
    query = "(" + " OR ".join(f"from:{s}" for s in estado_senders) + f") has:attachment after:{after}"
    ids = []
    req = svc.users().messages().list(userId="me", q=query, maxResults=100)
    while req is not None:
        resp = req.execute(num_retries=REINTENTOS)
        ids += [m["id"] for m in resp.get("messages", [])]
        req = svc.users().messages().list_next(req, resp)

    guardados = []
    for mid in ids:
        msg = svc.users().messages().get(userId="me", id=mid,
                                         format="full").execute(num_retries=REINTENTOS)
        payload = msg["payload"]
        slug = emisor_slug(parseaddr(header(payload, "From"))[1])
        fecha = fecha_mail_iso(msg)[:10] or "sinfecha"
        pend = [payload]
        while pend:
            p = pend.pop()
            pend += p.get("parts", []) or []
            fn = (p.get("filename") or "").strip()
            if not fn.lower().endswith(".pdf"):
                continue                      # ignora logos/imagenes del mail
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fn).strip("_")
            destino = os.path.join(ESTADOS, f"{fecha}_{slug}_{safe}")
            tam = p.get("body", {}).get("size")
            if os.path.exists(destino):
                if tam is None or os.path.getsize(destino) == tam:
                    continue                  # ya lo tenemos: nada que hacer
                # Mismo nombre, contenido DISTINTO: son dos estados distintos del
                # mismo emisor y dia. BBVA manda siempre 'EC Visa .pdf', sin nada
                # que los diferencie, asi que el segundo caia en este `continue` y
                # se perdia EN SILENCIO (auditoria 2026-07 B12). Se desambigua con
                # el message_id, y los nombres ya existentes quedan intactos: si se
                # renombraran todos, la proxima corrida re-bajaria cada PDF.
                destino = os.path.join(ESTADOS, f"{fecha}_{slug}_{mid[:8]}_{safe}")
                if os.path.exists(destino):
                    continue
            aid = p.get("body", {}).get("attachmentId")
            if not aid:
                continue
            adj = svc.users().messages().attachments().get(
                userId="me", messageId=mid, id=aid).execute(num_retries=REINTENTOS)
            with open(destino, "wb") as f:
                f.write(base64.urlsafe_b64decode(adj["data"].encode("utf-8")))
            guardados.append(os.path.basename(destino))
    return guardados


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
    senders, denylist, estado_senders = cargar_reglas_fetch()
    after = leer_after()
    svc = build("gmail", "v1", credentials=creds_gmail(), cache_discovery=False)

    query = "(" + " OR ".join(f"from:{s}" for s in senders) + f") after:{after}"

    # Enumerar por MENSAJE (no por thread).
    ids = []
    req = svc.users().messages().list(userId="me", q=query, maxResults=500)
    while req is not None:
        resp = req.execute(num_retries=REINTENTOS)
        ids += [m["id"] for m in resp.get("messages", [])]
        req = svc.users().messages().list_next(req, resp)

    vistos = ids_ya_vistos()
    nuevos = [i for i in ids if i not in vistos]

    os.makedirs(INBOX, exist_ok=True)
    deny_re = [re.compile(p, re.I) for p in denylist]
    guardados, saltados = 0, []
    for mid in nuevos:
        msg = svc.users().messages().get(userId="me", id=mid,
                                         format="full").execute(num_retries=REINTENTOS)
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
        # newline="\n": inbox_raw/ esta commiteado y es la fuente de los tests de
        # caracterizacion; sin esto una corrida local reescribe todo en CRLF
        # (auditoria 2026-09 SB1).
        with open(os.path.join(INBOX, f"{mid}.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        guardados += 1

    print(f"fetch_mails: after:{after} | {len(ids)} mensajes en Gmail | "
          f"{len(nuevos)} no vistos | {guardados} guardados | {len(saltados)} saltados (denylist)")
    for mid, subj in saltados:
        print(f"  skip {mid}: {subj!r}")

    # estados de cuenta: guardar los PDF adjuntos (unica via para Mastercard/BBVA,
    # que no mandan avisos por transaccion). Los procesa conciliar_estado.py.
    pdfs = bajar_estados_pdf(svc, estado_senders)
    if pdfs:
        print(f"estados de cuenta: {len(pdfs)} PDF nuevos en data/estados/")
        for p in pdfs:
            print(f"  + {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
