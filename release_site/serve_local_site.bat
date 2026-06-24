@echo off
setlocal
cd /d "%~dp0"
echo Traffic Light local release site
echo.
echo Home page:
echo   http://127.0.0.1:8000/
echo.
echo Client update URL:
echo   http://127.0.0.1:8000/releases/latest/update.json
echo.
python -m http.server 8000
