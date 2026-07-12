@echo off
rem Punto de entrada unico del pipeline (lo invoca /sync-gastos, tambien sirve a mano).
rem Corre: procesar mails -> datos de dashboard -> validacion -> commit.
cd /d "C:\Users\joaco\Desktop\proyectos claude\gastos"
C:\Users\joaco\anaconda3\python.exe scripts\procesar_mails.py || exit /b 1
C:\Users\joaco\anaconda3\python.exe scripts\generar_dashboard_data.py || exit /b 1
C:\Users\joaco\anaconda3\python.exe scripts\merge_y_validar.py || exit /b 1
git add -A
git commit -m "sync automatico %date% %time%" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" || echo (sin cambios para commitear)
exit /b 0
