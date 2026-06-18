# Auto-Flight-schedular

Auric Air daily route auto-planner. Feeds on a manifest (date, origin→destination,
passenger count) and produces a flyable, aircraft-by-aircraft route plan that
**minimizes a weighted blend** of empty legs, total block time, and missed
connections, while **maximizing load factor** and respecting **hard** constraints:
seats, payload **weight**, daylight field hours, aircraft availability, and crew
duty limits.

Two engines optimize the *identical* objective:
- **Heuristic** (`demo_solver.py`) — zero dependencies, runs anywhere, instantly.
- **Production** (`auric_route_planner.py`) — Google OR-Tools PDPTW, searches far
  harder for the same goal.

`run.py` auto-selects: it uses OR-Tools if installed, otherwise the heuristic — so
the project **always** produces a plan.

---

## Project structure

```
Auto-Flight-schedular/
├── run.py                    # START HERE — auto-selects the best engine
├── planner_core.py           # shared: data model, geo→time, THE objective, costing, printer
├── demo_solver.py            # zero-dependency heuristic engine
├── auric_route_planner.py    # production OR-Tools engine (needs: pip install ortools)
├── requirements.txt          # the only external dep: ortools
├── setup.sh                  # one-command install (macOS / Linux)
├── setup.bat                 # one-command install (Windows)
├── .gitignore
├── data/                     # the ONLY files Ops edits
│   ├── airstrips.csv         # code, name, lat, lon, daylight_only, open/close, fuel
│   ├── fleet.csv             # reg, type, base, seats, payload_kg, cruise, duty limits
│   └── manifest.csv          # the daily demand: id, date, origin, dest, pax, connect_by
└── output/
    └── plan_output.json      # machine-readable plan for downstream systems / a UI
```

---

## Prerequisites

- **Python 3.10 or newer.** Check with `python3 --version`.
- Internet access **once**, to install OR-Tools (the heuristic needs nothing).

---

## Step 1 — Install dependencies

### macOS / Linux
```bash
cd Auto-Flight-schedular
bash setup.sh
source .venv/bin/activate
```

### Windows
```bat
cd Auto-Flight-schedular
setup.bat
.venv\Scripts\activate.bat
```

The setup script creates an isolated `.venv` and installs OR-Tools. Manual
equivalent if you prefer:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> You can **skip this step entirely** to just try it — `run.py` falls back to the
> heuristic engine, which has no dependencies.

---

## Step 2 — Run the planner

```bash
python run.py
```

- With OR-Tools installed → runs the production optimizer.
- Without it → runs the heuristic and tells you so.

Run a specific engine directly:
```bash
python demo_solver.py            # heuristic
python auric_route_planner.py    # production (after install)
```

Point it at a different data folder:
```bash
python run.py /path/to/another/day
```

---

## Expected results

Out of the box (sample data: 12 demands / 33 pax, 3 Caravans), you'll see a plan
that **serves all 33 passengers on 2 of the 3 aircraft** — the third is left on the
ground because the objective judged a third aircraft's empty positioning legs not
worth it. Abridged:

```
==========================================================================
AURIC AIR — DAILY ROUTE PLAN
==========================================================================

  5H-AUA  (C208B Grand Caravan, base ARK, 12 seats / 1100kg)
    route : ARK ->[+D05]->[+D01]-> SEU ->[+D03]-> LKY ->[+D04]-> ARK ...
      07:10 ARK -> SEU 07:58   47min   6 pax
      08:38 SEU -> LKY 09:12   33min   7 pax
      09:52 LKY -> ARK 10:14   22min   7 pax
    block 104min | empty 0min | duty 04:24 | peak 7/12 seats (58%) | 13 pax carried

  5H-AUC  (C208B Grand Caravan, base MWZ, 12 seats / 1100kg)
      06:50 MWZ -> GRU 07:23   32min   2 pax
      ...
      15:57 GRU -> MWZ 16:30   32min   0 pax  (empty/positioning)
    block 279min | empty 32min | duty 10:00 | peak 9/12 seats (75%) | 20 pax carried

--------------------------------------------------------------------------
  SERVED 33/33 pax  |  aircraft used 2  |  total block 384min  |  empty 33min
  objective cost = 982.8  (block 1.0xb + empty 3.0xe + ac 250xn + late 5xl + drop 5000xd)
==========================================================================
Structured plan written to .../output/plan_output.json
```

The OR-Tools engine optimizes the same objective and will typically return an
**equal-or-lower objective cost** (tighter routing, fewer empty minutes) on the
same data. Both guarantee every printed plan is feasible: no overloaded aircraft,
no after-dark arrivals, no busted duty limits.

---

## Plugging in YOUR data

Edit the three CSVs in `data/`. Columns are documented in the header row and in
`planner_core.py`. Key points:

- **`airstrips.csv`** — put your **exact** lat/longs here. `daylight_only=1` strips
  use `open_min`/`close_min` (minutes from midnight) as hard arrival windows.
- **`fleet.csv`** — `payload_kg` is useful load for pax + bags (weight is checked
  *alongside* seats). `max_duty_min` is the crew duty span cap.
- **`manifest.csv`** — one row per purchased O-D. `connect_by` (optional) is a soft
  connection deadline in minutes from midnight; `earliest_dep` (optional) holds a
  demand until a given time.

Tune the objective in `planner_core.py → Weights`. The **ratios** are a P&L
decision: raise `w_empty` if ferry cost dominates, `w_connection` if missed
connections cost you guests.

---

## Troubleshooting

- **`ModuleNotFoundError: ortools`** → you haven't installed yet (Step 1), or your
  shell isn't in the activated `.venv`. `run.py` will still work via the heuristic.
- **`python: command not found`** → use `python3`.
- **A demand shows up under "UNSERVED"** → no aircraft could carry it within the
  hard limits (weight/seats/daylight/duty). Add capacity, relax a window, or handle
  it manually. This is the planner refusing to hand you an infeasible plan.

---

## Before you trust it in operations

1. Replace the **approximate** sample coordinates with your real lat/longs, and have
   a pilot/Ops sign off the daylight windows, payloads, and duty limits — those are
   **safety inputs**, not just efficiency knobs.
2. Run it against **last month's real manifests** and compare to what dispatchers
   actually flew. Match/beat → trust earned. Mismatch → it's surfacing a constraint
   to add.

## Deferred to v2

- Fuel routing (stored per strip, not yet enforced).
- Exact load-aware empty-leg cost in OR-Tools (currently the standard block-time +
  aircraft-cost approximation).
- Real published sector times (v1 derives leg time from distance ÷ cruise + a pad).
