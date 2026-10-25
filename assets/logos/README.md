# Logos de cuentas del dashboard

`generar_dashboard_html.py` embebe estos archivos en `out/dashboard.html` como data URI (el dashboard es un solo HTML, sin recursos externos).

- Los que vienen son placeholders: un cuadrado con el color de la marca y la inicial.
- Para usar el logo oficial, reemplazá el archivo **con el mismo nombre** (`itau`, `santander`, `bbva`, `gletir`, `ibkr`, `casa`) por un `.svg` o `.png` chico. Si hay `.svg` y `.png`, gana el `.svg`. Máximo 60 KB: más grande, el generador frena con un mensaje.
- Si falta el archivo, la tarjeta sale sin logo. No se rompe nada.
