# Respaldo semanal cifrado a Google Drive

Fase 0.3 de la [auditoría 2026-09](auditoria-2026-09.md) (hallazgo **SA9**, decisión **D6**).

**El problema que cierra:** hasta ahora las únicas dos copias de los datos eran GitHub y la PC
de Joaco — dos copias que se pierden *juntas* ante un `push --force` mal apuntado, un
`git filter-repo` que sale mal o una cuenta suspendida. Y lo que hay adentro no se puede
volver a generar: `data/resoluciones.csv` son **756 decisiones tomadas a mano**, una por una;
hay ~150 filas del ledger (`estado_cuenta`, `manual`, `splitwise`) sin ninguna otra fuente; y
`data/raw/` tiene el export **único** de 1Money (la app ya no ofrece ese camino) y la planilla
original de casa.

| | |
|---|---|
| **Qué** | zip AES-256 de `data/` + `config/` (sin secretos) + `scripts/` + `docs/` + `tests/` + `.github/` + los archivos raíz |
| **Dónde** | Google Drive de joacoguerrae@gmail.com, carpeta **"Respaldos gastos"** |
| **Cuándo** | domingos 13:30 UTC (10:30 Uruguay), después del sync de la mañana |
| **Quién** | el workflow [`.github/workflows/respaldo.yml`](../.github/workflows/respaldo.yml) → [`scripts/backup_drive.py`](../scripts/backup_drive.py) |
| **Cuánto** | ~5,5 MB por respaldo; se retienen **26** (6 meses ≈ 140 MB de los 15 GB de la cuenta) |
| **Clave** | secret de repo `BACKUP_ZIP_PASSWORD`; local, `backup_zip_password` en `config/secrets.local.json` |

---

## Acciones de Joaco (una sola vez, en este orden)

> **Hasta que no estén hechos los pasos 1-2 y 4-7, el workflow falla todos los domingos** y la
> corrida queda en rojo (te llega mail). Eso es a propósito: un respaldo que no existe tiene
> que verse.

1. **Generá la clave del zip.** Una passphrase larga y aleatoria (≥ 30 caracteres) en el gestor
   de contraseñas, guardada con el nombre `respaldo gastos (BACKUP_ZIP_PASSWORD)`.
   ⚠️ **Si se pierde, todos los respaldos quedan inservibles a la vez.** No hay recuperación
   posible: guardala **también fuera del gestor** (papel, caja fuerte).
2. **Cargala como secret:** repo → Settings → Secrets and variables → Actions →
   New repository secret → nombre exacto **`BACKUP_ZIP_PASSWORD`**.
3. *(Opcional, solo si querés correr el respaldo desde la PC)* agregá
   `"backup_zip_password": "<la misma clave>"` a `config/secrets.local.json` (gitignoreado).
4. **Habilitá la Drive API.** [console.cloud.google.com](https://console.cloud.google.com) → arriba,
   elegí **el mismo proyecto** donde está la service account de Sheets → menú ☰ → *APIs & Services*
   → *Library* → buscá **"Google Drive API"** → **Enable**.
   ⚠️ **Esto va ANTES del paso 5**: el selector de scopes solo lista scopes de APIs habilitadas, así
   que si la Drive API está apagada, `drive.file` **no aparece en la lista** y parece que el scope no
   existe.
5. **Agregá los dos scopes.** Ojo: Google renombró esta pantalla en 2025. Ya no se llama
   "OAuth consent screen" a secas — entrando por ahí te redirige a **Google Auth Platform**, que está
   partida en sub-páginas (*Overview · Branding · Audience · Clients · Data access · Verification
   center*). Lo que el doc viejo llamaba "la pantalla de consentimiento" hoy son dos lugares:
   - **Publishing status** → sub-página **Audience**. Tiene que decir **"In production"**; si dice
     "Testing", el refresh token vence a los 7 días.
   - **Scopes** → sub-página **Data access** → botón **"Add or remove scopes"** → se abre un panel a
     la derecha con un buscador.

   Si el buscador no los encuentra (pasa seguido: filtra por nombre de API, no por el string del
   scope), usá la caja **"Manually add scopes"** al final del panel y pegá estos dos, uno por línea:

   ```
   https://www.googleapis.com/auth/drive.file
   https://www.googleapis.com/auth/gmail.send
   ```

   Después **"Add to table"** → **"Update"** (cierra el panel) → y **"Save"** abajo de todo en
   *Data access*. Si no tocás Save, no queda guardado.

   `gmail.send` va a aparecer como **restricted** y `drive.file` como **recommended**: es lo
   esperado y no bloquea nada. La app es propia, sin verificar, y el único usuario sos vos — igual
   que ya pasa hoy con `gmail.readonly`.
6. **Re-autorizá en la PC:** `C:/Users/joaco/anaconda3/python.exe scripts/get_gmail_token.py`.
   Se abre el navegador; entrá con joacoguerrae@gmail.com; va a aparecer otra vez la pantalla de
   "Google no verificó esta app" → Avanzado → Continuar.
   **Dejá TODAS las casillas marcadas** (Gmail lectura, Gmail envío, Drive). El script aborta con
   exit 4 si Google devuelve el token sin alguno de los tres scopes — destildar una casilla es el
   modo de falla más probable y, sin ese chequeo, recién se notaría el domingo siguiente.
7. **Actualizá el secret `GMAIL_REFRESH_TOKEN`** en GitHub con el valor nuevo que imprime el
   script. `GMAIL_CLIENT_ID` y `GMAIL_CLIENT_SECRET` no cambian.
   ⚠️ El token viejo **sigue funcionando para el fetch de mails** (tiene `gmail.readonly`), así
   que si te olvidás de este paso el sync sigue andando y **solo** falla el respaldo: una falla
   parcial fácil de no notar. Por eso el error del 403 nombra el secret exacto.
8. **Dispará la primera corrida a mano:** Actions → "Respaldo semanal" → Run workflow
   (probá primero con `dry_run = true`, después en serio). Confirmá que en tu Drive aparece la
   carpeta **"Respaldos gastos"** con el zip y el `LEEME-restaurar.txt`.
9. **Probá que podés abrirlo:** bajá el zip, abrilo con 7-Zip y la clave del gestor.
   Este es el gate real de la fase: *un respaldo que no sabés abrir no es un respaldo.*
10. **No borres ni renombres la carpeta "Respaldos gastos".** Moverla dentro de tu Drive está
    bien (el id queda guardado en `data/staging/backup_drive.json`); borrarla hace que el script
    cree una nueva y los respaldos viejos queden en la papelera.

---

## Restaurar

### Camino corto (sin Python) — para leer un archivo puntual

1. Drive → carpeta **"Respaldos gastos"** → bajá el `gastos_AAAA-MM-DD_<commit>.zip` más reciente.
2. **El Explorador de Windows NO abre zips AES.** Usá 7-Zip (7-zip.org, gratis):
   botón derecho → 7-Zip → Extraer en `gastos\` → te pide la clave.
3. Adentro, `MANIFIESTO.txt` tiene el sha256 y el tamaño de cada archivo, el commit de origen
   y la fecha.

### Camino completo (el que vale como verificación)

```bash
# 1. bajar el zip de Drive
# 2. extraer + verificar sha256 de cada archivo + correr los invariantes del ledger
python scripts/backup_drive.py restaurar \
    --zip gastos_2026-09-20_0a30de9.zip \
    --destino /tmp/restore \
    --verificar
echo $?     # 0 = los datos están intactos Y son internamente consistentes
```

Qué hace `--verificar`: recalcula el sha256 de cada archivo extraído y lo compara contra el
manifiesto, y después corre `merge_y_validar.py` (el del propio zip) con `GASTOS_BASE=/tmp/restore`,
propagando su exit code. Es decir: no solo que los bytes llegaron bien, sino que el ledger pasa
sus 14 invariantes.

`restaurar` **se niega a extraer sobre un directorio que no esté vacío**: mezclar un respaldo con
un árbol vivo deja un estado que no es ni el de antes ni el del respaldo.

### Volver a poner el sistema en pie

```bash
git clone <el repo>  repo-nuevo          # o: git init en el directorio restaurado
cp -r /tmp/restore/data /tmp/restore/config  repo-nuevo/
pip install -r requirements.txt
python scripts/merge_y_validar.py        # tiene que salir 0
```

Los cuatro secretos (`secrets.local.json`, `gcp_service_account.json`, `gmail_token.json`,
`oauth_client.json`) **no están en el respaldo a propósito** y hay que regenerarlos:
`get_gmail_token.py` para los de Google, y los otros dos desde GCP / Splitwise.

---

## Qué se respalda y qué no

**Entra todo lo que no se puede regenerar, y también lo regenerable porque es barato**
(459 archivos, ~12,7 MB crudos → 5,5 MB comprimidos):

| Raíz | Por qué |
|---|---|
| `data/` completa | `raw/` = export único de 1Money + planilla de casa (irreproducible) · `estados/` = los CSV que exportás a mano del portal, el banco no guarda historia indefinida · `staging/` = las líneas base congeladas de `--migracion` · `inbox_raw/` = los mails crudos |
| `config/` menos la denylist | sin las reglas, categorías, cuentas y parámetros el ledger no se puede reconstruir ni interpretar |
| `scripts/`, `tests/`, `docs/`, `.github/` | sin el pipeline el respaldo son CSV huérfanos; con él es autosuficiente y se valida solo |
| `requirements.txt`, `pytest.ini`, `README.md`, `CLAUDE.md` | versiones exactas + guía operativa |

**No entra:** `out/` (100% derivado de `data/`), `.git/`, `__pycache__/`, `registro_app.mmbackup`
(muerto, SB2) y **los cuatro archivos de secretos**.

### Las tres capas que impiden que un secreto salga en el zip

Es el riesgo que mata: un zip con datos financieros en Drive es un problema acotado; un zip con
el `gcp_service_account.json` adentro es una cuenta comprometida.

1. **Al armar la lista** — denylist por basename (`secrets.local.json`, `gcp_service_account.json`,
   `gmail_token.json`, `oauth_client.json`, `.env`) y por patrón (`service_account`, `token*.json`,
   `*.pem`, `*.p12`, `*.key`, `.env*`). Se excluyen y se imprime una línea por cada uno: es el
   caso esperado, no aborta.
2. **Sniff de contenido** — todo `.json`/`.csv`/`.txt`/`.md` incluido se lee buscando los cinco
   marcadores de credencial de Google (el `type` de una service account, la clave privada, el
   refresh token, el client secret y la cabecera PEM). La lista exacta está en `DENY_CONTENIDO`,
   en [`scripts/backup_drive.py`](../scripts/backup_drive.py) — **a propósito no se transcribe
   acá: este mismo archivo entra al zip y se escanea, así que copiar los literales haría fallar
   el respaldo con exit 3.** (Pasó al implementarlo: el guard cazó su propia documentación.)
   Un match **aborta con exit 3** y no escribe nada. Esta es la capa que atrapa el secreto con
   nombre inocente.
3. **Re-chequeo del zip ya escrito** — se reabre el `.zip`, se re-aplica la denylist sobre los
   nombres reales y se descifran 5 archivos (los 2 críticos + 3 al azar con semilla fija)
   comparando sha256 contra el manifiesto. Si algo falla: **se borra el zip y no se sube**.
   Esta capa chequea el artefacto, no la intención — y de paso prueba que la clave descifra
   de verdad *antes* de subir.

### Guardas de integridad

El modo de falla de este sistema es el **silencio**, no el crash: un respaldo de un árbol vacío
es peor que ninguno, porque da tranquilidad falsa. El script aborta con **exit 4** si falta
alguno de los 7 archivos obligatorios, si `ledger.csv` pesa < 400 KB o tiene < 4.000 líneas,
si `resoluciones.csv` tiene < 700 filas, si hay menos de 300 archivos o si el zip final pesa
< 2 MB.

---

## Si la corrida del domingo sale en rojo

El mail de GitHub trae el `::error::` con el código y qué significa:

| exit | Qué pasó | Qué hacer |
|---|---|---|
| 2 | falta la clave del zip | cargar el secret `BACKUP_ZIP_PASSWORD` (acción 2) |
| 3 | **posible filtración de secretos** o el zip no verifica | el zip se borró y no se subió nada. Mirar el log: dice qué archivo y qué marcador |
| 4 | el árbol no pasó las guardas | falta un archivo obligatorio o el ledger está truncado: **mirar el repo antes que el respaldo** |
| 5 | lo que quedó en Drive no coincide con el zip local, o no hay espacio | el archivo remoto se borró solo; revisar la cuota de Drive |
| 6 | credenciales / scope | el refresh token no tiene `drive.file`: acciones 4 a 7 |
| 7 | falta `pyzipper` | agregar `pyzipper==0.4.0` y `pycryptodomex==3.23.0` a `requirements.txt` |

Además, `salud_pasos.py` registra la racha: a las **3 fallas seguidas** el próximo sync abre un
Issue. Ojo que con cadencia semanal eso son **3 semanas** — por eso el workflow *además* sale
con exit 1, que es lo que dispara el mail de Actions el mismo domingo.

Para diagnosticar sin esperar: Actions → "Respaldo semanal" → Run workflow con
`dry_run = true`. Arma y verifica el zip completo **sin tocar Drive ni necesitar credenciales**.

---

## Limitaciones conocidas

- **El cifrado AES de WinZip no cifra los nombres de archivo.** El directorio central queda en
  claro: quien tenga el zip ve que existe `data/resoluciones.csv`, pero no puede leer su
  contenido sin la clave. Aceptable acá; si algún día no lo fuera, habría que ir a 7z sólido
  con `-mhe=on`, que reabre la dependencia de un binario del runner.
- **`data/raw/` se repite intacto en cada respaldo** (2,7 MB comprimidos × 26 ≈ 70 MB).
  Es deliberado: respaldarlo una sola vez crearía un respaldo que hay que armar desde dos lados.
  Si algún día molesta, se sube aparte como `gastos_fuentes_congeladas.zip` sin rotación.
- **La rotación manda a la papelera, no borra.** Drive los conserva 30 días, así que un bug de
  rotación es recuperable: es la diferencia entre perder *un* respaldo y perderlos *todos*.
- **Dos workflows commitean `data/staging/`.** Mitigado compartiendo el `concurrency group`
  `sync-gastos` con `sync.yml` (nunca se solapan) y commiteando solo dos rutas explícitas con
  `git pull --rebase`. Si igual apareciera un conflicto, el push falla, la corrida queda roja y
  el domingo siguiente se resuelve solo (el estado se reescribe entero).
- **`pyzipper` + `pycryptodomex` amplían la superficie de supply-chain** del job que tiene todos
  los secrets en el entorno. Por eso van pinneados con `==`, igual que el resto. El
  endurecimiento real (`pip install --require-hashes`) queda para la fase 6.

## Por qué estas decisiones

- **Zip AES-256 con `pyzipper` y no gpg/7z/tar.** `zipfile` de la stdlib **no puede escribir**
  zips cifrados (`setpassword()` es solo de lectura: el zip sale con `flag_bits=0` y se lee sin
  clave — verificado). Había que agregar algo sí o sí. Ganó lo que mejor se restaura dentro de
  dos años: un `.zip` AES-256 lo abre 7-Zip con doble clic; un `.tar.gz.gpg` pide Gpg4win y
  consola. Además `gpg`/`7z` atarían el respaldo a la versión que traiga la imagen del runner
  ese día, que es lo contrario de la política de pinneo del repo.
- **OAuth de usuario y no la service account.** Desde el 1/6/2023 las service accounts no tienen
  cuota de Drive propia: `files().create()` devuelve 403 `storageQuotaExceeded`. Y la del repo
  tiene scope solo de Sheets. El archivo consume los 15 GB de la cuenta de Joaco.
- **Workflow propio y no un `if` de día de la semana dentro de `sync.yml`.** Un condicional
  adentro es invisible — el mismo modo de falla que la auditoría llama "silencio": nadie puede
  responder "¿corrió el respaldo?" sin leer logs. Con workflow propio la historia se ve en la UI,
  `sync.yml` (camino crítico, timeout 15') no carga dos minutos de zip ni un secret más, y una
  corrida programada fallida manda mail.
- **El zip se arma en `out/tmp/`**, que ya está gitignoreado. En `out/` a secas lo commitearía el
  `git add data out` de `sync.yml`: 5,5 MB por corrida y, peor, un archivo cifrado con datos
  financieros en la historia de git para siempre.
