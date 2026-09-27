# -*- coding: utf-8 -*-
"""
backup_drive.py -- Respaldo semanal CIFRADO de los datos de gastos a Google Drive.

Por que existe (auditoria 2026-09, SA9 / D6): hoy la unica copia de
data/resoluciones.csv -- 756 decisiones humanas irrecuperables -- son GitHub y la
PC de Joaco. Dos copias que se pierden juntas ante un `git push --force` mal
apuntado o un disco muerto. Esto agrega una tercera, afuera, semanal.

Por que un zip AES-256 y no gpg/7z/tar: `zipfile` de la stdlib NO puede ESCRIBIR
zips cifrados (setpassword() es solo de lectura: el zip sale con flag_bits=0 y se
lee sin clave -- verificado). Habia que agregar algo si o si, y entre las
opciones gano la que mejor se RESTAURA dentro de dos anios: un .zip AES-256 lo
abre 7-Zip en Windows con doble clic y la clave, sin instalar Python ni entender
nada. gpg pide Gpg4win y consola; 7z ata el respaldo al binario que traiga la
imagen del runner ese dia (lo contrario de la politica de pinneo del repo).

Por que el OAuth de USUARIO y no la service account de Sheets: desde el 1/6/2023
las service accounts no tienen cuota de Drive propia -- files().create() devuelve
403 storageQuotaExceeded. El respaldo sube con las MISMAS GMAIL_* de
fetch_mails.py, con el grant ampliado a drive.file (el scope lo define el grant,
no el codigo: por eso fetch_mails.py no se toca).

Lo que MAS importa de este script no es que suba: es que NUNCA suba un secreto.
Tres capas independientes (denylist por nombre, sniff de contenido, re-chequeo
del zip ya escrito antes de subir). Si cualquiera dispara, el zip se borra y no
se sube nada.

Uso:
  backup_drive.py                    arma el zip, lo sube, rota, borra el zip local
  backup_drive.py --dry-run          arma y VERIFICA el zip; no toca Drive
  backup_drive.py --conservar        sube y ademas deja el zip local
  backup_drive.py --retener N        cuantos respaldos quedan en Drive (default 26)
  backup_drive.py restaurar --zip <f> --destino <dir> [--verificar]

Codigos de salida:
  0 ok | 2 falta la clave del zip | 3 posible filtracion / el zip no verifica
  4 el arbol no pasa las guardas de integridad | 5 la subida no verifica
  6 credenciales de Drive ausentes o sin el scope drive.file
  7 falta pyzipper (la dependencia que escribe el zip cifrado)
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys

BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(BASE, *a)

CARPETA_DRIVE = "Respaldos gastos"
RETENER = 26                 # 26 semanales = 6 meses ~ 140 MB de los 15 GB de la cuenta
ESTADO = P("data", "staging", "backup_drive.json")
# out/tmp/ ya esta en .gitignore: el zip NUNCA puede terminar commiteado. Ojo que
# out/ a secas SI se commitea (sync.yml hace `git add data out ...`).
TMP = P("out", "tmp")
SECRETS = P("config", "secrets.local.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Que entra. Criterio: lo irreproducible entra si o si (data/raw = el export unico
# de 1Money y la planilla original de casa; data/estados = CSV que Joaco exporta a
# mano del portal y el banco no guarda para siempre); lo regenerable entra igual
# porque es barato. Con el codigo adentro el respaldo es autosuficiente: se
# restaura y se valida sin clonar el repo.
INCLUIR_DIRS = ["data", "config", "scripts", "docs", "tests", ".github"]
INCLUIR_ARCHIVOS = ["requirements.txt", "pytest.ini", "README.md", "CLAUDE.md"]
# out/ queda afuera: es 100% derivado de data/ (lo regenera generar_dashboard_*).
# registro_app.mmbackup tambien (2,3 MB marcados como muertos en SB2).

SALTAR_DIRS = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", "node_modules"}
SALTAR_EXT = {".pyc", ".pyo"}

# ---- Denylist de secretos: el invariante de CLAUDE.md, en codigo -------------
DENY_BASENAMES = {
    "secrets.local.json", "gcp_service_account.json",
    "gmail_token.json", "oauth_client.json", ".env",
}
DENY_PATRONES = [re.compile(p, re.I) for p in (
    r"^secrets\.local\.", r"service[_-]?account", r"token.*\.json$",
    r"\.pem$", r"\.p12$", r"\.key$", r"^\.env",
)]
# Sniff de contenido: atrapa el secreto que entra con un nombre inocente (un
# token volcado a config/backup.json, por ejemplo). Verificado que ninguno de
# estos marcadores aparece en los 450 archivos legitimos del arbol.
DENY_CONTENIDO = [
    b'"type": "service_account"', b'"private_key"', b'"refresh_token"',
    b'"client_secret"', b"-----BEGIN ",
]
EXT_SNIFF = {".json", ".csv", ".txt", ".md"}

# ---- Guardas de integridad --------------------------------------------------
# El modo de falla de este sistema es el SILENCIO, no el crash: un respaldo de un
# arbol vacio o truncado es peor que ninguno, porque da tranquilidad falsa.
OBLIGATORIOS = [
    "data/ledger.csv", "data/resoluciones.csv", "data/pendientes.csv",
    "data/participaciones.csv", "data/aportes_lu.csv",
    # Fase 1.1: la fuente de las filas que no se regeneran desde ningun lado.
    "data/fuentes/manual.csv", "data/fuentes/estado_cuenta.csv",
    "data/fuentes/splitwise.csv", "data/fuentes/carga_manual.csv",
    "config/cuentas.csv", "config/categorias.csv",
]
MIN_LEDGER_BYTES = 400 * 1024      # hoy 792 KB
MIN_LEDGER_LINEAS = 4000           # hoy 4.962
MIN_RESOLUCIONES_LINEAS = 700      # hoy 757
MIN_ARCHIVOS = 300                 # hoy ~450
MIN_ZIP_BYTES = 2 * 1024 * 1024    # hoy 5,4 MB

LEEME = """RESPALDO DEL SISTEMA DE GASTOS -- como restaurarlo

El archivo gastos_AAAA-MM-DD_<commit>.zip esta cifrado con AES-256.
La clave esta en tu gestor de contrasenias como "respaldo gastos (BACKUP_ZIP_PASSWORD)".
El Explorador de Windows NO abre zips AES: usa 7-Zip (7-zip.org, gratis).

1. Bajate el zip mas reciente.
2. Boton derecho -> 7-Zip -> Extraer en "gastos\\" -> te pide la clave.
3. Adentro esta MANIFIESTO.txt con el sha256 de cada archivo y el commit de origen.
4. Para volver a tener el sistema andando: instalar Python 3.12, y en la carpeta
   extraida:  pip install -r requirements.txt  y  python scripts\\merge_y_validar.py
   Si imprime OK en todas las lineas y sale con codigo 0, los datos estan sanos.

Lo irrecuperable que hay adentro: data/resoluciones.csv (las decisiones tomadas a
mano, una por una), data/fuentes/ (las filas del ledger que no se regeneran desde
ningun lado: manual / estado_cuenta / splitwise, y la copia de la planilla "Carga
manual") y data/raw/ (el export unico de 1Money y la planilla de casa). Si el
ledger no valida: python scripts\\reconstruir_ledger.py --forzar lo arma de nuevo.

OJO: el cifrado AES de WinZip NO cifra los NOMBRES de archivo. Quien tenga el zip
ve que existe data/resoluciones.csv, pero no puede leer su contenido sin la clave.
"""


# ============================ utilidades =====================================

def ahora_utc():
    return dt.datetime.now(dt.timezone.utc)


def sha256_archivo(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def md5_archivo(ruta):
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def contar_lineas(ruta):
    n = 0
    with open(ruta, "rb") as f:
        for _ in f:
            n += 1
    return n


def git(*args):
    """-> stdout limpio, o None. Nunca revienta: el respaldo no depende de git."""
    try:
        r = subprocess.run(["git", "-C", BASE, *args], capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def importar_pyzipper():
    try:
        import pyzipper
        return pyzipper
    except ImportError:
        print("ERROR: falta el paquete 'pyzipper'.\n"
              "  Es el que escribe el zip cifrado: zipfile de la stdlib NO puede\n"
              "  (setpassword() es solo de lectura).\n"
              "  Instalalo con:  pip install -r requirements.txt\n"
              "  o directamente: pip install pyzipper==0.4.0\n"
              "  (Si falla en Actions: falta la linea pyzipper==0.4.0 en requirements.txt.)",
              file=sys.stderr)
        # Codigo propio y no 2: que el mail de GitHub distinga 'falta la clave'
        # de 'falta la dependencia' sin tener que abrir el log.
        sys.exit(7)


def clave():
    """La clave del zip: env (Actions) o secrets.local.json (PC). Sin ella no se
    escribe NADA -- un zip sin cifrar con la historia financiera completa es peor
    que no tener respaldo."""
    pwd = (os.environ.get("BACKUP_ZIP_PASSWORD") or "").strip()
    if pwd:
        return pwd
    if os.path.exists(SECRETS):
        try:
            # utf-8-sig y no utf-8: si el archivo se edito con un editor de
            # Windows queda con BOM y json.load revienta con un error que no
            # dice nada util.
            with open(SECRETS, encoding="utf-8-sig") as f:
                d = json.load(f)
            pwd = (d.get("backup_zip_password") or "").strip()
        except (json.JSONDecodeError, OSError) as e:
            print(f"AVISO: no se pudo leer {SECRETS} ({e}).", file=sys.stderr)
            pwd = ""
        if pwd:
            return pwd
    print("ERROR: falta la clave del zip.\n"
          "  Que falta exactamente:\n"
          "    - en GitHub Actions: el repository secret  BACKUP_ZIP_PASSWORD\n"
          "      (repo -> Settings -> Secrets and variables -> Actions -> New repository secret)\n"
          "    - en la PC: la clave  \"backup_zip_password\"  dentro de config/secrets.local.json\n"
          "      (gitignoreado; mismo patron que ci_pdf_password)\n"
          "  La clave la elige Joaco: una passphrase larga (>= 30 caracteres) guardada en el\n"
          "  gestor de contrasenias como \"respaldo gastos (BACKUP_ZIP_PASSWORD)\" Y tambien\n"
          "  fuera de el: si se pierde, TODOS los respaldos quedan inservibles.",
          file=sys.stderr)
    sys.exit(2)


# ============================ denylist =======================================

def denegado_por_nombre(nombre):
    """-> motivo (str) si el basename cae en la denylist, None si esta limpio."""
    if nombre in DENY_BASENAMES:
        return f"basename en la denylist ({nombre})"
    for rx in DENY_PATRONES:
        if rx.search(nombre):
            return f"matchea el patron /{rx.pattern}/"
    return None


def denegado_por_contenido(ruta):
    """-> motivo si el archivo contiene un marcador de secreto, None si no.
    Solo se miran los tipos de texto: un .db o un .xlsx no se parsean aca."""
    if os.path.splitext(ruta)[1].lower() not in EXT_SNIFF:
        return None
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
    except OSError:
        return None
    for marca in DENY_CONTENIDO:
        if marca in datos:
            return f"contiene el marcador {marca.decode('utf-8', 'replace')!r}"
    return None


# ============================ recoleccion ====================================

def recolectar():
    """-> [(ruta_absoluta, ruta_dentro_del_zip)] ordenado. Aplica las capas 1 y 2
    de la denylist. La capa 1 no aborta (excluir los 4 secretos de config/ es el
    caso ESPERADO); la capa 2 si: un secreto con nombre inocente es un bug."""
    archivos = []
    excluidos = []
    fugas = []

    def considerar(abs_, arc):
        motivo = denegado_por_nombre(os.path.basename(abs_))
        if motivo:
            excluidos.append((arc, motivo))
            return
        motivo = denegado_por_contenido(abs_)
        if motivo:
            fugas.append((arc, motivo))
            return
        archivos.append((abs_, arc))

    for d in INCLUIR_DIRS:
        raiz = P(d)
        if not os.path.isdir(raiz):
            print(f"aviso: {d}/ no existe en {BASE}, se saltea")
            continue
        for dirpath, dirnames, filenames in os.walk(raiz):
            dirnames[:] = sorted(x for x in dirnames if x not in SALTAR_DIRS)
            for nombre in sorted(filenames):
                if os.path.splitext(nombre)[1].lower() in SALTAR_EXT:
                    continue
                abs_ = os.path.join(dirpath, nombre)
                # Rutas del zip siempre con "/": asi se extraen igual en Windows
                # y en Linux (un "\" literal en el nombre no es un separador).
                arc = os.path.relpath(abs_, BASE).replace("\\", "/")
                considerar(abs_, arc)

    for nombre in INCLUIR_ARCHIVOS:
        abs_ = P(nombre)
        if not os.path.isfile(abs_):
            print(f"aviso: {nombre} no existe en {BASE}, se saltea")
            continue
        considerar(abs_, nombre)

    for arc, motivo in excluidos:
        print(f"excluido por denylist: {arc} -- {motivo}")

    if fugas:
        print("\nERROR: hay archivos con pinta de secreto que NO estan en la denylist "
              "por nombre. No se escribe ningun zip.", file=sys.stderr)
        for arc, motivo in fugas:
            print(f"  {arc} -- {motivo}", file=sys.stderr)
        print("Que hacer: si son secretos, agregalos a .gitignore y a DENY_BASENAMES.\n"
              "Si es un doc que MENCIONA el marcador (pasa: este mismo chequeo escanea "
              "docs/), reescribi esa linea para que no contenga el literal -- no aflojes "
              "DENY_CONTENIDO por una mencion en prosa.", file=sys.stderr)
        sys.exit(3)

    archivos.sort(key=lambda t: t[1])
    return archivos


def verificar_obligatorios(archivos):
    """Guardas de integridad: que el arbol que estamos por respaldar sea el arbol
    real y no uno truncado a la mitad."""
    por_arc = {arc: abs_ for abs_, arc in archivos}
    problemas = []

    for obl in OBLIGATORIOS:
        if obl not in por_arc:
            problemas.append(f"falta un archivo obligatorio: {obl}")

    ledger = por_arc.get("data/ledger.csv")
    if ledger:
        n_bytes = os.path.getsize(ledger)
        n_lineas = contar_lineas(ledger)
        if n_bytes < MIN_LEDGER_BYTES:
            problemas.append(f"data/ledger.csv pesa {n_bytes} bytes (< {MIN_LEDGER_BYTES})")
        if n_lineas < MIN_LEDGER_LINEAS:
            problemas.append(f"data/ledger.csv tiene {n_lineas} lineas (< {MIN_LEDGER_LINEAS})")

    reso = por_arc.get("data/resoluciones.csv")
    if reso:
        n_lineas = contar_lineas(reso)
        if n_lineas < MIN_RESOLUCIONES_LINEAS:
            problemas.append(f"data/resoluciones.csv tiene {n_lineas} lineas "
                             f"(< {MIN_RESOLUCIONES_LINEAS})")

    if len(archivos) < MIN_ARCHIVOS:
        problemas.append(f"solo {len(archivos)} archivos para respaldar (< {MIN_ARCHIVOS})")

    if problemas:
        print("\nERROR: el arbol no pasa las guardas de integridad. NO se respalda: "
              "un respaldo truncado da tranquilidad falsa.", file=sys.stderr)
        for p in problemas:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(4)


# ============================ manifiesto =====================================

MARCA_INI = "--- ARCHIVOS (sha256  bytes  ruta) ---"
MARCA_FIN = "--- FIN ARCHIVOS ---"


def armar_manifiesto(archivos, commit, rama):
    total = sum(os.path.getsize(a) for a, _ in archivos)
    lineas = [
        "MANIFIESTO DEL RESPALDO DEL SISTEMA DE GASTOS",
        "",
        f"fecha_utc: {ahora_utc().isoformat(timespec='seconds')}",
        f"commit:    {commit}",
        f"rama:      {rama}",
        f"archivos:  {len(archivos)}",
        f"bytes:     {total}",
        f"origen:    {BASE}",
        "",
        MARCA_INI,
    ]
    for abs_, arc in archivos:
        lineas.append(f"{sha256_archivo(abs_)}  {os.path.getsize(abs_)}  {arc}")
    lineas += [MARCA_FIN, "", LEEME, ""]
    return "\n".join(lineas)


def leer_manifiesto(texto):
    """-> {ruta: (sha256, bytes)} a partir del texto del MANIFIESTO."""
    filas = {}
    dentro = False
    for linea in texto.splitlines():
        if linea.strip() == MARCA_INI:
            dentro = True
            continue
        if linea.strip() == MARCA_FIN:
            break
        if not dentro or not linea.strip():
            continue
        partes = linea.split(None, 2)
        if len(partes) != 3:
            continue
        sha, n, ruta = partes
        filas[ruta] = (sha, int(n))
    return filas


# ============================ zip ============================================

def escribir_zip(destino, pwd, archivos, manifiesto, pyzipper):
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with pyzipper.AESZipFile(destino, "w", compression=pyzipper.ZIP_DEFLATED,
                             compresslevel=6, encryption=pyzipper.WZ_AES) as zf:
        zf.setencryption(pyzipper.WZ_AES, nbits=256)
        zf.setpassword(pwd.encode("utf-8"))
        zf.writestr("MANIFIESTO.txt", manifiesto)
        for abs_, arc in archivos:
            zf.write(abs_, arc)


def verificar_zip(destino, pwd, archivos, shas, pyzipper):
    """Capa 3: se chequea el ARTEFACTO, no la intencion. Reabre el zip ya escrito,
    re-aplica la denylist sobre los nombres reales y descifra unos cuantos
    archivos comparando sha256 contra el manifiesto -- que es la unica prueba de
    que la clave descifra de verdad ANTES de subir nada.
    Cualquier falla: se borra el zip y se sale != 0."""
    problemas = []
    try:
        with pyzipper.AESZipFile(destino, "r") as zf:
            zf.setpassword(pwd.encode("utf-8"))
            nombres = zf.namelist()

            for n in nombres:
                if n == "MANIFIESTO.txt":
                    continue
                motivo = denegado_por_nombre(os.path.basename(n))
                if motivo:
                    problemas.append(f"FILTRACION: el zip contiene {n} -- {motivo}")

            esperadas = len(archivos) + 1
            if len(nombres) != esperadas:
                problemas.append(f"el zip tiene {len(nombres)} entradas, se esperaban {esperadas}")

            # Muestra con semilla fija (reproducible entre corridas) + los dos
            # archivos que no se pueden regenerar desde ninguna otra fuente.
            criticos = [a for a in ("data/ledger.csv", "data/resoluciones.csv") if a in shas]
            resto = sorted(set(shas) - set(criticos))
            muestra = criticos + random.Random(1234).sample(resto, min(3, len(resto)))
            for arc in muestra:
                try:
                    datos = zf.read(arc)
                except (RuntimeError, KeyError) as e:
                    problemas.append(f"no se pudo releer {arc} del zip: {e}")
                    continue
                real = hashlib.sha256(datos).hexdigest()
                if real != shas[arc][0]:
                    problemas.append(f"sha256 distinto al del manifiesto en {arc}")
            verificados = len(muestra)
    except Exception as e:                       # zip corrupto, clave mal, etc.
        problemas.append(f"no se pudo reabrir el zip: {e}")
        verificados = 0

    n_bytes = os.path.getsize(destino) if os.path.exists(destino) else 0
    if n_bytes < MIN_ZIP_BYTES:
        problemas.append(f"el zip pesa {n_bytes} bytes (< {MIN_ZIP_BYTES}): algo falto")

    if problemas:
        print("\nERROR: el zip NO pasa la verificacion. Se borra y no se sube nada.",
              file=sys.stderr)
        for p in problemas:
            print(f"  - {p}", file=sys.stderr)
        try:
            os.remove(destino)
        except OSError:
            pass
        sys.exit(3)

    print(f"zip verificado: {len(archivos) + 1} entradas, sin secretos, "
          f"{verificados} archivos releidos y comparados contra el manifiesto")


# ============================ estado =========================================

def leer_estado():
    if not os.path.exists(ESTADO):
        return {}
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def escribir_estado(d):
    os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ============================ Drive ==========================================

def creds_drive():
    """Mismas env vars que fetch_mails.creds_gmail(), otro scope. El scope real lo
    define el GRANT que autorizo Joaco, no esta lista: por eso ampliar el
    consentimiento a drive.file no obliga a tocar fetch_mails.py."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError
    except ImportError:
        print("ERROR: faltan las librerias de Google (google-auth). "
              "Instalalas con: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(6)

    cid = os.environ.get("GMAIL_CLIENT_ID")
    csec = os.environ.get("GMAIL_CLIENT_SECRET")
    rtok = os.environ.get("GMAIL_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        token_file = P("config", "gmail_token.json")
        if not os.path.exists(token_file):
            print("ERROR: faltan las credenciales de Google para subir a Drive.\n"
                  "  Que falta exactamente:\n"
                  "    - en GitHub Actions: los secrets GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET\n"
                  "      y GMAIL_REFRESH_TOKEN (ya existen para el fetch de mails)\n"
                  f"    - en la PC: el archivo {token_file}\n"
                  "  Se genera con:  python scripts/get_gmail_token.py\n"
                  "  (Para probar el zip sin credenciales: corre con --dry-run.)",
                  file=sys.stderr)
            sys.exit(6)
        with open(token_file, encoding="utf-8") as f:
            d = json.load(f)
        cid, csec, rtok = d["client_id"], d["client_secret"], d["refresh_token"]

    creds = Credentials(None, refresh_token=rtok,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=cid, client_secret=csec, scopes=SCOPES)
    try:
        creds.refresh(Request())
    except RefreshError as e:
        print("ERROR: el refresh token no sirve para Drive.\n"
              f"  Detalle de Google: {e}\n"
              "  Casi seguro es que el token todavia tiene solo el scope de Gmail.\n"
              "  Como se arregla (una sola vez):\n"
              "    1. GCP Console -> habilitar la Google Drive API en el proyecto.\n"
              "    2. OAuth consent screen -> agregar el scope "
              ".../auth/drive.file (y .../auth/gmail.send).\n"
              "    3. En la PC:  python scripts/get_gmail_token.py   (dejar TODAS "
              "las casillas marcadas).\n"
              "    4. Reemplazar el secret GMAIL_REFRESH_TOKEN en GitHub con el valor nuevo.\n"
              "  Ojo: el token viejo sigue andando para el fetch de mails, asi que "
              "hasta el paso 4 solo falla el respaldo.", file=sys.stderr)
        sys.exit(6)
    return creds


def servicio_drive():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        print("ERROR: falta google-api-python-client. "
              "Instalalo con: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(6)
    return build("drive", "v3", credentials=creds_drive(), cache_discovery=False)


def carpeta(svc):
    """El id se persiste porque con scope drive.file la app solo ve lo que ella
    misma creo: si Joaco mueve la carpeta dentro de su Drive, el id sigue valido."""
    from googleapiclient.errors import HttpError

    est = leer_estado()
    fid = est.get("carpeta_id")
    if fid:
        try:
            m = svc.files().get(fileId=fid, fields="id,trashed").execute()
            if not m.get("trashed"):
                return fid
            print(f"la carpeta guardada ({fid}) esta en la papelera: se busca o crea otra")
        except HttpError as e:
            if e.resp.status != 404:
                raise
            print(f"la carpeta guardada ({fid}) ya no existe: se busca o crea otra")

    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name='{CARPETA_DRIVE}'")
    r = svc.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    if r.get("files"):
        return r["files"][0]["id"]
    m = svc.files().create(
        body={"name": CARPETA_DRIVE, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute()
    print(f"carpeta creada en Drive: {CARPETA_DRIVE} ({m['id']})")
    return m["id"]


def asegurar_leeme(svc, fid):
    """El procedimiento de restauracion vive AL LADO de los zips, en claro: si
    estuviera solo adentro del zip cifrado haria falta la clave para leer como se
    usa la clave."""
    from googleapiclient.http import MediaInMemoryUpload

    q = (f"'{fid}' in parents and name='LEEME-restaurar.txt' and trashed=false")
    r = svc.files().list(q=q, fields="files(id)", pageSize=5).execute()
    if r.get("files"):
        return
    media = MediaInMemoryUpload(LEEME.encode("utf-8"), mimetype="text/plain")
    svc.files().create(body={"name": "LEEME-restaurar.txt", "parents": [fid]},
                       media_body=media, fields="id").execute()
    print("subido LEEME-restaurar.txt a la carpeta de Drive")


def subir(svc, fid, destino):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(destino, mimetype="application/zip", resumable=True)
    try:
        f = svc.files().create(
            body={"name": os.path.basename(destino), "parents": [fid]},
            media_body=media,
            fields="id,name,size,md5Checksum,createdTime").execute()
    except HttpError as e:
        motivo = str(e)
        if e.resp.status == 403 and "storageQuota" in motivo:
            print("ERROR: la cuenta de Google no tiene espacio libre en Drive. "
                  "Libera espacio o baja la constante RETENER.", file=sys.stderr)
            sys.exit(5)
        if e.resp.status == 403:
            print("ERROR: Google rechazo la subida por permisos (403). Casi seguro el "
                  "refresh token no tiene el scope drive.file: re-autorizar con "
                  "scripts/get_gmail_token.py y actualizar el secret GMAIL_REFRESH_TOKEN.",
                  file=sys.stderr)
            sys.exit(6)
        raise

    # Verificacion post-subida: que Drive tenga EXACTAMENTE los mismos bytes.
    # Un zip cifrado truncado no da error al subir, solo al restaurar -- dentro
    # de dos anios y con el original ya perdido.
    local_bytes = os.path.getsize(destino)
    local_md5 = md5_archivo(destino)
    remoto_bytes = int(f.get("size") or 0)
    remoto_md5 = f.get("md5Checksum")
    if remoto_bytes != local_bytes or (remoto_md5 and remoto_md5 != local_md5):
        print(f"ERROR: lo que quedo en Drive no coincide con el zip local "
              f"(local {local_bytes} B / {local_md5}, remoto {remoto_bytes} B / {remoto_md5}). "
              f"Se borra el archivo remoto.", file=sys.stderr)
        try:
            svc.files().delete(fileId=f["id"]).execute()
        except HttpError:
            print("(ademas no se pudo borrar el archivo remoto: borralo a mano)",
                  file=sys.stderr)
        sys.exit(5)
    print(f"subido a Drive: {f['name']} ({local_bytes} bytes, md5 {local_md5})")
    return f


def rotar(svc, fid, retener):
    """A la PAPELERA, no borrado definitivo: Drive los conserva 30 dias, asi que
    un bug de rotacion es recuperable. Es la diferencia entre perder un respaldo
    y perderlos todos."""
    q = f"'{fid}' in parents and trashed=false and name contains 'gastos_'"
    r = svc.files().list(q=q, orderBy="createdTime desc", pageSize=100,
                         fields="files(id,name,createdTime)").execute()
    todos = r.get("files", [])
    for f in todos[retener:]:
        svc.files().update(fileId=f["id"], body={"trashed": True}).execute()
        print(f"rotado a la papelera: {f['name']}")
    return min(len(todos), retener)


# ============================ respaldar ======================================

def resumen_por_directorio(archivos):
    cuentas = {}
    for _, arc in archivos:
        top = arc.split("/")[0] if "/" in arc else "(raiz)"
        if top == "data" and arc.count("/") >= 2:
            top = "data/" + arc.split("/")[1]
        cuentas[top] = cuentas.get(top, 0) + 1
    return cuentas


def respaldar(args):
    pyzipper = importar_pyzipper()
    pwd = clave()

    # Las credenciales se chequean ANTES de armar el zip (2 minutos de trabajo):
    # mientras Joaco no re-autorice Google, esto falla todos los domingos, y el
    # mensaje que explica que hacer tiene que ser lo PRIMERO del log, no lo
    # ultimo despues de 460 sha256.
    svc = None if args.dry_run else servicio_drive()

    archivos = recolectar()
    verificar_obligatorios(archivos)

    commit = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD") or "desconocido"
    rama = (os.environ.get("GITHUB_REF_NAME")
            or git("rev-parse", "--abbrev-ref", "HEAD") or "desconocida")

    manifiesto = armar_manifiesto(archivos, commit, rama)
    shas = leer_manifiesto(manifiesto)

    hoy = ahora_utc().date()
    corto = commit[:7] if commit != "desconocido" else "nocommit"
    destino = os.path.join(TMP, f"gastos_{hoy:%Y-%m-%d}_{corto}.zip")

    crudo = sum(os.path.getsize(a) for a, _ in archivos)
    print(f"a respaldar: {len(archivos)} archivos, {crudo / 1e6:.2f} MB crudos")
    for top, n in sorted(resumen_por_directorio(archivos).items()):
        print(f"  {top:20s} {n:5d}")

    escribir_zip(destino, pwd, archivos, manifiesto, pyzipper)
    verificar_zip(destino, pwd, archivos, shas, pyzipper)
    n_bytes = os.path.getsize(destino)
    print(f"zip: {destino} ({n_bytes / 1e6:.2f} MB, ratio {n_bytes / crudo:.0%})")

    if args.dry_run:
        print("--dry-run: NO se sube nada a Drive. El zip queda en out/tmp/ "
              "(gitignoreado) para que lo revises.")
        return 0

    fid = carpeta(svc)
    f = subir(svc, fid, destino)
    asegurar_leeme(svc, fid)
    quedan = rotar(svc, fid, args.retener)

    escribir_estado({
        "carpeta_id": fid,
        "ultimo_ok": ahora_utc().isoformat(timespec="seconds"),
        "ultimo_archivo": f["name"],
        "ultimo_commit": commit,
        "bytes": n_bytes,
        "md5": f.get("md5Checksum") or md5_archivo(destino),
        "archivos_en_drive": quedan,
    })

    if not args.conservar:
        try:
            os.remove(destino)
        except OSError:
            pass

    print(f"respaldo OK: {f['name']} en '{CARPETA_DRIVE}' "
          f"({quedan} respaldos guardados, se retienen {args.retener})")
    return 0


# ============================ restaurar ======================================

def restaurar(args):
    pyzipper = importar_pyzipper()
    pwd = clave()

    zip_path = os.path.abspath(args.zip)
    destino = os.path.abspath(args.destino)
    if not os.path.exists(zip_path):
        print(f"ERROR: no existe {zip_path}", file=sys.stderr)
        return 1
    # Extraer sobre un arbol vivo mezclaria archivos de dos epocas y dejaria un
    # estado que no es ni el del respaldo ni el de antes.
    if os.path.isdir(destino) and os.listdir(destino):
        print(f"ERROR: {destino} existe y no esta vacio. Usa un directorio limpio.",
              file=sys.stderr)
        return 1

    with pyzipper.AESZipFile(zip_path, "r") as zf:
        zf.setpassword(pwd.encode("utf-8"))
        try:
            # Leer el manifiesto ANTES de crear el destino: si la clave esta mal,
            # no queda un directorio vacio dando vueltas.
            manifiesto = zf.read("MANIFIESTO.txt").decode("utf-8")
        except RuntimeError:
            print("ERROR: la clave no abre este zip (o el zip esta corrupto). "
                  "Es la de 'respaldo gastos (BACKUP_ZIP_PASSWORD)' en el gestor.",
                  file=sys.stderr)
            return 1
        os.makedirs(destino, exist_ok=True)
        zf.extractall(destino)

    esperados = leer_manifiesto(manifiesto)
    malos = []
    for arc, (sha, n) in sorted(esperados.items()):
        ruta = os.path.join(destino, arc.replace("/", os.sep))
        if not os.path.exists(ruta):
            malos.append(f"{arc}: no se extrajo")
            continue
        if sha256_archivo(ruta) != sha:
            malos.append(f"{arc}: sha256 distinto al del manifiesto")
    if malos:
        print(f"ERROR: {len(malos)} archivo(s) no coinciden con el manifiesto:",
              file=sys.stderr)
        for m in malos:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print(f"{len(esperados)}/{len(esperados)} archivos verificados contra el manifiesto")
    print(f"restaurado en: {destino}")

    if not args.verificar:
        print("(corre con --verificar para ademas validar los invariantes del ledger)")
        return 0

    script = os.path.join(destino, "scripts", "merge_y_validar.py")
    if not os.path.exists(script):
        print(f"ERROR: el respaldo no trae {script}", file=sys.stderr)
        return 1
    env = dict(os.environ, GASTOS_BASE=destino, PYTHONIOENCODING="utf-8")
    print(f"\ncorriendo merge_y_validar.py con GASTOS_BASE={destino} ...")
    sys.stdout.flush()      # sin esto la salida del hijo sale ANTES que la nuestra
    r = subprocess.run([sys.executable, script], env=env)
    print(f"merge_y_validar.py salio con exit {r.returncode}")
    return r.returncode


# ============================ CLI ============================================

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "restaurar":
        ap = argparse.ArgumentParser(
            prog="backup_drive.py restaurar",
            description="extrae un respaldo cifrado y verifica sha256 contra el manifiesto")
        ap.add_argument("--zip", required=True, help="el .zip bajado de Drive")
        ap.add_argument("--destino", required=True, help="directorio VACIO donde extraer")
        ap.add_argument("--verificar", action="store_true",
                        help="ademas corre merge_y_validar.py sobre lo restaurado")
        return restaurar(ap.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(
        description="respaldo semanal cifrado (AES-256) de los datos de gastos a Google Drive")
    ap.add_argument("--dry-run", action="store_true",
                    help="arma y verifica el zip pero NO sube nada (no necesita credenciales)")
    ap.add_argument("--conservar", action="store_true",
                    help="deja el zip local en out/tmp/ despues de subirlo")
    ap.add_argument("--retener", type=int, default=RETENER,
                    help=f"cuantos respaldos quedan en Drive (default {RETENER})")
    return respaldar(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
