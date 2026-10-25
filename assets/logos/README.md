# Logos de cuentas del dashboard

`generar_dashboard_html.py` embebe estos archivos en `out/dashboard.html` como data URI (el dashboard es un solo HTML, sin recursos externos).

- `santander`, `gletir` e `ibkr` son el símbolo de cada marca redibujado en SVG a partir de los logos que pasó Joaco (25/10/2026). Va solo el símbolo, sin el texto, porque en la tarjeta se ve a 22 px.
- `itau` y `bbva` siguen siendo placeholders: un cuadrado con el color de la marca y el nombre. `casa` es un ícono propio.
- Para cambiar uno, reemplazá el archivo **con el mismo nombre** por un `.svg` o `.png` chico y cuadrado. Si hay `.svg` y `.png`, gana el `.svg`. Máximo 60 KB: más grande, el generador frena con un mensaje.
- Si falta el archivo, la tarjeta sale sin logo. No se rompe nada.
