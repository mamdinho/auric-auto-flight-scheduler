#!/usr/bin/env bash
# One-command setup for macOS / Linux.
# Creates an isolated virtual environment and installs OR-Tools.
set -e
cd "$(dirname "$0")"

echo "==> Checking Python..."
python3 --version

echo "==> Creating virtual environment (.venv)..."
python3 -m venv .venv

echo "==> Installing dependencies..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "Setup complete."
echo "Activate the environment with:   source .venv/bin/activate"
echo "Then run the planner with:        python run.py"
