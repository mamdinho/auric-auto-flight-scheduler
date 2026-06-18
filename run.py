#!/usr/bin/env python3
"""
run.py — single entry point for the Auto-Flight-schedular.

Tries OR-Tools first (better plans); if it crashes or is not installed,
falls back to the zero-dependency heuristic so you always get a result.

Usage:
    python run.py [optional_data_dir]
"""
import sys
import os
import subprocess

here = os.path.dirname(os.path.abspath(__file__))
data_arg = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    import ortools  # noqa: F401
    HAVE_ORTOOLS = True
except ImportError:
    HAVE_ORTOOLS = False

if __name__ == "__main__":
    if HAVE_ORTOOLS:
        print(">> Trying OR-Tools (production optimizer)...\n")
        cmd = [sys.executable, os.path.join(here, "auric_route_planner.py")]
        if data_arg:
            cmd.append(data_arg)
        result = subprocess.run(cmd, cwd=here)
        if result.returncode != 0:
            print(f"\n>> OR-Tools exited with code {result.returncode} "
                  f"— falling back to heuristic.\n")
            from demo_solver import main
            main()
    else:
        print(">> Engine: heuristic  (install OR-Tools via 'pip install ortools' "
              "for better plans)\n")
        from demo_solver import main
        main()
