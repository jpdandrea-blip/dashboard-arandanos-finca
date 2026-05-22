@echo off
title Dashboard Arandanos - Finca Leon Rouges

cd /d "C:\Users\juanp\OneDrive\Documents\Claude cowork"

echo Iniciando dashboard meteorologico...
echo.

:: Verificar si ya esta corriendo en puerto 8501
netstat -an | find "8501" | find "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Dashboard ya en ejecucion.
    start "" "http://localhost:8501"
    exit /b
)

:: Iniciar Streamlit en segundo plano
start /min "" python -m streamlit run dashboard_arandanos.py --server.port 8501 --server.headless true --browser.gatherUsageStats false

echo Esperando que el servidor arranque...
timeout /t 5 /nobreak >nul

start "" "http://localhost:8501"
echo Dashboard abierto en http://localhost:8501
