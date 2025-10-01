@echo off
cd /d C:\PROOFER\server
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate
pip install -r requirements.txt
python server.py
echo.
echo -------------------------------
echo (If this window closes instantly, run it from an open cmd.)
pause
