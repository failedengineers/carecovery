@echo off
setlocal
cd /d "%~dp0"

REM This helper is intentionally development-only. Never copy these values to production.
set DJANGO_DEBUG=1
set DJANGO_SECRET_KEY=local-only-insecure-key-do-not-deploy-carecovery
set DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost

if not exist .venv\Scripts\python.exe (
    echo Creating Python virtual environment...
    py -3 -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt || goto :error
python manage.py makemigrations crm --noinput || goto :error
python manage.py migrate --noinput || goto :error
python manage.py seed_demo || goto :error
python manage.py check || goto :error

echo.
echo CareCovery development environment is ready. Open http://127.0.0.1:8000/
echo Demo credentials are for local evaluation only and must never be deployed.
python manage.py runserver 127.0.0.1:8000
exit /b 0

:error
echo.
echo Setup failed. Review the error above, then try again.
pause
exit /b 1
