"""
One-shot: obtiene el REFRESH TOKEN de Google para el pipeline headless (GitHub Actions).
Se corre UNA sola vez en la PC de Joaco. No necesita instalar nada nuevo
(google-auth-oauthlib ya está instalado).

El token que sale de acá lo usan TRES cosas, con un solo grant:
  - scripts/fetch_mails.py     -> gmail.readonly   (baja los mails financieros)
  - scripts/backup_drive.py    -> drive.file       (respaldo semanal cifrado, fase 0.3)
  - el resumen semanal por mail -> gmail.send      (fase 4; todavía no existe)

OJO si ya tenías un token: el viejo SIGUE sirviendo para Gmail, así que el fetch de
mails no se rompe — pero NO tiene permiso de Drive, y el respaldo va a fallar con 403
hasta que reemplaces el secret GMAIL_REFRESH_TOKEN por el valor nuevo que imprime
este script.

Pasos previos (en GCP Console, mismo proyecto que la cuenta de servicio de Sheets):
  1. APIs & Services -> Library -> habilitar "Gmail API" Y "Google Drive API".
  2. La vieja "OAuth consent screen" hoy es "Google Auth Platform" (Google la
     renombró en 2025) y está partida en sub-páginas:
       - Audience    -> User type "External" y Publishing status "In production"
                        (NO "Testing": ahí el refresh token vence a los 7 días).
       - Data access -> "Add or remove scopes" -> agregar los tres scopes de
                        abajo. Si el buscador no los encuentra, pegarlos en
                        "Manually add scopes" y despues Save.
     Único usuario: joacoguerrae@gmail.com. El paso 1 va ANTES: el selector solo
     lista scopes de APIs habilitadas, asi que con la Drive API apagada
     drive.file no aparece y parece que el scope no existe.
     (gmail.readonly ya era "restricted"; drive.file y gmail.send no cambian el
     régimen: sigue apareciendo la pantalla de "app no verificada".)
  3. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type "Desktop app". Descargar el JSON.
  4. Guardar ese JSON como  config/oauth_client.json  (está en .gitignore, no sale de la PC).

Uso:
    python scripts/get_gmail_token.py

Abre el navegador -> iniciás sesión con tu cuenta -> "Permitir". DEJÁ TODAS LAS
CASILLAS MARCADAS: la pantalla de consentimiento de Google permite destildar
permisos de a uno, y un scope faltante recién se nota cuando algo falla días
después. El script aborta en el momento si falta alguno.

Imprime los 3 valores que van como Secrets en GitHub. NO los pegues en el chat.
"""
import os
import sys
import json

# oauthlib aborta con un `Warning: Scope has changed from X to Y` cuando Google
# devuelve un conjunto de scopes distinto al pedido -- que es EXACTAMENTE el caso
# de una casilla destildada. Ese error sale como traceback crudo y no dice cual
# falta. Lo relajamos para que el flujo termine y la verificacion de abajo de un
# mensaje que se entienda, nombrando el permiso que falto.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",   # fetch_mails.py
    "https://www.googleapis.com/auth/gmail.send",       # resumen semanal (fase 4)
    "https://www.googleapis.com/auth/drive.file",       # backup_drive.py (fase 0.3)
]
BASE = os.environ.get("GASTOS_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(BASE, "config", "oauth_client.json")


def main():
    if not os.path.exists(CLIENT):
        print(f"ERROR: falta {CLIENT}\n"
              f"Descargá el OAuth client (tipo 'Desktop app') de GCP y guardalo ahí.",
              file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT, SCOPES)
    # access_type=offline (default en InstalledAppFlow) + prompt=consent => devuelve refresh_token
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        print("ERROR: Google no devolvió refresh_token. Revocá el acceso previo en "
              "https://myaccount.google.com/permissions y reintentá.", file=sys.stderr)
        return 3

    # El modo de falla más probable de todo esto: destildar una casilla en la
    # pantalla de consentimiento. El token sale igual, parece que funcionó, y el
    # respaldo recién falla el domingo siguiente. Se corta acá.
    concedidos = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    faltan = [s for s in SCOPES if s not in concedidos]
    if faltan:
        print("ERROR: Google no concedió estos permisos (¿destildaste alguna casilla?):",
              file=sys.stderr)
        for s in faltan:
            print("   -", s, file=sys.stderr)
        print("Reintentá y dejá TODAS las casillas marcadas.", file=sys.stderr)
        return 4

    with open(CLIENT, encoding="utf-8") as f:
        info = json.load(f)
    inst = info.get("installed") or info.get("web") or {}

    # Guardar token local (gitignored) para que fetch_mails.py corra en la PC.
    token_file = os.path.join(BASE, "config", "gmail_token.json")
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump({"client_id": inst.get("client_id", ""),
                   "client_secret": inst.get("client_secret", ""),
                   "refresh_token": creds.refresh_token}, f, indent=2)
    print(f"\nGuardado {token_file} (gitignored) para las corridas locales.")

    print("\nPermisos concedidos:")
    for s in SCOPES:
        print("   OK  ", s)

    print("\n" + "=" * 64)
    print("SECRETOS PARA GITHUB  (repo -> Settings -> Secrets and variables -> Actions)")
    print("=" * 64)
    print("GMAIL_CLIENT_ID")
    print("   ", inst.get("client_id", "(no encontrado)"))
    print("GMAIL_CLIENT_SECRET")
    print("   ", inst.get("client_secret", "(no encontrado)"))
    print("GMAIL_REFRESH_TOKEN")
    print("   ", creds.refresh_token)
    print("=" * 64)
    print("Cargá cada uno como un Secret con ESE nombre exacto. No los pegues en el chat.")
    print("El que CAMBIA es GMAIL_REFRESH_TOKEN: reemplazalo o el respaldo a Drive "
          "va a seguir fallando con 403.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
