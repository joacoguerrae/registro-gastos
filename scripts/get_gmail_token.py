"""
One-shot: obtiene el REFRESH TOKEN de Gmail para el fetch headless (GitHub Actions).
Se corre UNA sola vez en la PC de Joaco. No necesita instalar nada nuevo
(google-auth-oauthlib ya está instalado).

Pasos previos (en GCP Console, mismo proyecto que la cuenta de servicio de Sheets):
  1. APIs & Services -> Library -> habilitar "Gmail API".
  2. APIs & Services -> OAuth consent screen -> User type "External" -> Publishing status
     "In production" (NO "Testing": en Testing el refresh token vence a los 7 días).
     Agregar el scope .../auth/gmail.readonly. Único usuario: joacoguerrae@gmail.com.
  3. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type "Desktop app". Descargar el JSON.
  4. Guardar ese JSON como  config/oauth_client.json  (está en .gitignore, no sale de la PC).

Uso:
    python scripts/get_gmail_token.py

Abre el navegador -> iniciás sesión con tu cuenta -> "Permitir". El script imprime los
3 valores que van como Secrets en GitHub. NO los pegues en el chat.
"""
import os
import sys
import json

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
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

    with open(CLIENT, encoding="utf-8") as f:
        info = json.load(f)
    inst = info.get("installed") or info.get("web") or {}

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
