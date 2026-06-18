@echo off
REM One-command setup for Windows.
cd /d "%~dp0"

echo ==^> Checking Python...
python --version

echo ==^> Creating virtual environment (.venv)...
python -m venv .venv

echo ==^> Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo Setup complete.
echo Activate the environment with:   .venv\Scripts\activate.bat
echo Then run the planner with:        python run.py
