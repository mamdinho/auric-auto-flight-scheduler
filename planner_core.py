"""
planner_core.py — shared foundation for the Auric Air route auto-planner.

This module holds everything that is independent of WHICH optimizer you run:
  * the data model (Airstrip, Aircraft, Demand)
  * CSV loaders so Ops edits plain spreadsheets, not code
  * geography (great-circle distance) -> flight-time matrix
  * the ONE objective function (the weighted blend) both engines optimize
  * a route-feasibility + costing routine (capacity by WEIGHT and SEATS,
    daylight windows, crew duty span, aircraft availability)
  * human-readable plan printing

Two engines import this:
  * demo_solver.py        -> pure-stdlib insertion heuristic (runs anywhere, today)
  * auric_route_planner.py -> Google OR-Tools PDPTW (production-grade optimizer)

Both optimize the SAME numbers, so swapping engines changes solution QUALITY,
never what "good" means.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from math import radians, sin, cos, asin, sqrt
import csv
import os

# --------------------------------------------------------------------------- #
# OBJECTIVE WEIGHTS  --  this is the P&L dial. Tuning these is a business
# decision you own, not a technical one. Raise W_EMPTY if empty legs are
# killing margin; raise W_CONNECTION if missed connections cost you guests.
# All costs are expressed in "penalty points"; only their RATIOS matter.
# --------------------------------------------------------------------------- #
@dataclass
class Weights:
    w_block: float = 1.0       # per minute of total block (flight) time
    w_empty: float = 3.0       # per minute flown with ZERO pax aboard (positioning/ferry)
    w_aircraft: float = 250.0  # fixed cost to put one more aircraft into service
                               #   -> pushes consolidation -> higher load factor, fewer ferries
    w_connection: float = 5.0  # per minute LATE to a passenger's connection deadline
    w_drop: float = 5000.0     # per passenger left unserved -> serve everyone unless infeasible


# Passenger/baggage weight model. WEIGHT, not seat count, is usually the binding
# constraint on hot/high bush strips. Edit to your real standard pax + bag weights.
@dataclass
class LoadModel:
    pax_weight_kg: float = 84.0   # average passenger incl. carry-on
    bag_kg: float = 15.0          # checked soft-bag allowance per pax
    climb_descent_pad_min: float = 6.0  # added to each flown leg for taxi/climb/descent


# --------------------------------------------------------------------------- #
# DATA MODEL
# --------------------------------------------------------------------------- #
@dataclass
class Airstrip:
    code: str
    name: str
    lat: float
    lon: float
    daylight_only: bool   # True = no night ops (no lighting / VFR only)
    open_min: int         # earliest usable arrival, minutes from midnight (local)
    close_min: int        # latest usable arrival (e.g. last light), minutes from midnight
    fuel: bool            # fuel available on field (not enforced in v1; reported)


@dataclass
class Aircraft:
    reg: str
    type: str
    base: str             # airstrip code where it starts AND ends the day
    seats: int
    payload_kg: float     # useful load available for pax + bags on a planning sector
    cruise_kts: float
    available_from: int   # duty start, minutes from midnight
    available_until: int  # duty end / last-light cap, minutes from midnight
    max_duty_min: int     # crew duty span limit (wheels-up first leg to on-blocks last)
    turnaround_min: int   # ground time per operational stop
    return_to_base: bool = True   # if False, stays at last stop rather than repositioning home
    flight_tag: str | None = None # flight this aircraft is assigned to; None = unrestricted
    next_route: str | None = None # optional follow-on flight after finishing flight_tag


@dataclass
class Demand:
    id: str
    date: str
    origin: str
    dest: str
    pax: int
    connect_by: list[int] | None = None  # one or more connecting-flight times at dest (minutes from midnight); semicolon-separated in CSV
    earliest_dep: int | None = None  # may not depart origin before this (minutes from midnight)
    passengers: list[str] | None = None  # individual passenger names from PDF; None if not available
    flight_tag: str | None = None        # flight number this demand belongs to; enforced by optimizer

    def weight_kg(self, lm: LoadModel) -> float:
        return self.pax * (lm.pax_weight_kg + lm.bag_kg)


# --------------------------------------------------------------------------- #
# LOADERS  (Ops edits these CSVs; engineers never touch the data)
# --------------------------------------------------------------------------- #
def _b(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")

def _opt_int(v):
    v = (v or "").strip()
    return int(v) if v else None

def _opt_int_list(v):
    """Parse an optional semicolon-separated list of ints (e.g. '990;1080').
    Returns None for blank, a list of ints otherwise."""
    v = (v or "").strip()
    if not v:
        return None
    return [int(p.strip()) for p in v.split(";") if p.strip()]

def load_airstrips(path: str) -> dict[str, Airstrip]:
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[r["code"].strip()] = Airstrip(
                code=r["code"].strip(), name=r["name"].strip(),
                lat=float(r["lat"]), lon=float(r["lon"]),
                daylight_only=_b(r["daylight_only"]),
                open_min=int(r["open_min"]), close_min=int(r["close_min"]),
                fuel=_b(r["fuel"]),
            )
    return out

def load_fleet(path: str) -> list[Aircraft]:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Aircraft(
                reg=r["reg"].strip(), type=r["type"].strip(), base=r["base"].strip(),
                seats=int(r["seats"]), payload_kg=float(r["payload_kg"]),
                cruise_kts=float(r["cruise_kts"]),
                available_from=int(r["available_from"]), available_until=int(r["available_until"]),
                max_duty_min=int(r["max_duty_min"]), turnaround_min=int(r["turnaround_min"]),
                return_to_base=_b(r.get("return_to_base", "1")),
                flight_tag=(r.get("flight_tag") or "").strip() or None,
                next_route=(r.get("next_route") or "").strip() or None,
            ))
    return out

def load_manifest(path: str) -> list[Demand]:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            pax_raw = (r.get("passengers") or "").strip()
            pax_names = [p.strip() for p in pax_raw.split(";") if p.strip()] or None
            out.append(Demand(
                id=r["id"].strip(), date=r["date"].strip(),
                origin=r["origin"].strip(), dest=r["dest"].strip(),
                pax=int(r["pax"]),
                connect_by=_opt_int_list(r.get("connect_by")),
                earliest_dep=_opt_int(r.get("earliest_dep")),
                passengers=pax_names,
                flight_tag=(r.get("flight_tag") or "").strip() or None,
            ))
    return out


# --------------------------------------------------------------------------- #
# GEOGRAPHY -> TIME
# --------------------------------------------------------------------------- #
_R_NM = 3440.065  # Earth radius in nautical miles

def great_circle_nm(a: Airstrip, b: Airstrip) -> float:
    if a.code == b.code:
        return 0.0
    lat1, lon1, lat2, lon2 = map(radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * _R_NM * asin(sqrt(h))

def block_minutes(a: Airstrip, b: Airstrip, cruise_kts: float, lm: LoadModel) -> float:
    """Block (flight) time for one leg. v1 = straight-line / cruise + a fixed pad.
    Replace with your real published sector times when you have them."""
    if a.code == b.code:
        return 0.0
    return (great_circle_nm(a, b) / cruise_kts) * 60.0 + lm.climb_descent_pad_min


# --------------------------------------------------------------------------- #
# ROUTE = the physical path one aircraft flies. A "stop" sits at an airstrip
# and is one of: depot start, a demand pickup, a demand delivery, depot end.
# --------------------------------------------------------------------------- #
@dataclass
class Stop:
    code: str               # airstrip code
    kind: str               # 'start' | 'pickup' | 'delivery' | 'end'
    demand_id: str | None = None

@dataclass
class LegReport:
    frm: str
    to: str
    depart_min: float
    arrive_min: float
    block_min: float
    pax_onboard: int
    empty: bool

@dataclass
class RouteResult:
    feasible: bool
    reason: str = ""
    legs: list[LegReport] = field(default_factory=list)
    total_block_min: float = 0.0
    empty_block_min: float = 0.0
    duty_span_min: float = 0.0
    connection_late_min: float = 0.0
    seats_used_peak: int = 0
    payload_peak_kg: float = 0.0
    used: bool = False  # did this aircraft fly any revenue stop?


def has_backtrack(stops: list[Stop]) -> bool:
    """Return True if any non-base airstrip is revisited after the aircraft has
    departed from it. Consecutive stops at the same airstrip (co-location
    pickup/delivery) are fine; returning to an airstrip already left is not."""
    if len(stops) < 4:
        return False
    base_code = stops[0].code
    departed_non_base: set[str] = set()
    prev_code = base_code
    for s in stops[1:-1]:          # exclude Start and End
        if s.code == base_code:    # delivering to / picking up at base is always fine
            prev_code = s.code
            continue
        if s.code != prev_code and prev_code != base_code:
            departed_non_base.add(prev_code)
        if s.code in departed_non_base:
            return True
        prev_code = s.code
    return False


def strip_time_window_demands(
    stops: list[Stop], ac: "Aircraft",
    strips: dict[str, "Airstrip"], demands: dict[str, "Demand"], lm: "LoadModel",
) -> tuple[list[Stop], set[str]]:
    """Iteratively remove demands whose deliveries violate the schedule time
    window until evaluate_route returns feasible.  Mirrors strip_backtrack_demands.

    When evaluate_route hits a 'late delivery' violation at stops[idx] it has
    appended exactly idx legs, so stops[len(res.legs)] is always the offending
    delivery stop — no need to re-walk the route.

    Returns (cleaned stops, set of removed demand IDs).
    """
    removed: set[str] = set()
    while True:
        res = evaluate_route(ac, stops, strips, demands, lm)
        if res.feasible:
            break
        if "late delivery" not in res.reason:
            break  # different hard constraint — caller handles
        v_idx = len(res.legs)
        if v_idx >= len(stops) or not stops[v_idx].demand_id:
            break
        offender = stops[v_idx].demand_id
        removed.add(offender)
        stops = [s for s in stops if s.demand_id != offender]
    return stops, removed


def strip_backtrack_demands(stops: list[Stop]) -> tuple[list[Stop], set[str]]:
    """Iteratively remove the earliest demand that causes a backtrack until the
    route is clean. Returns (cleaned stops, set of removed demand IDs)."""
    removed: set[str] = set()
    while True:
        if len(stops) < 4:
            break
        base_code = stops[0].code
        departed: set[str] = set()
        prev_code = base_code
        offender: str | None = None
        for s in stops[1:-1]:
            if s.code == base_code:
                prev_code = s.code
                continue
            if s.code != prev_code and prev_code != base_code:
                departed.add(prev_code)
            if s.code in departed and s.demand_id:
                offender = s.demand_id
                break
            prev_code = s.code
        if offender is None:
            break
        removed.add(offender)
        stops = [s for s in stops if s.demand_id != offender]
    return stops, removed


def evaluate_route(ac: Aircraft, stops: list[Stop],
                   strips: dict[str, Airstrip], demands: dict[str, Demand],
                   lm: LoadModel) -> RouteResult:
    """Walk an aircraft's stop sequence; check ALL hard constraints and tally
    everything the objective needs. Returns feasible=False with a reason on the
    first violation so the optimizer can reject the move."""
    res = RouteResult(feasible=True)
    if len(stops) <= 2:  # just start/end, no revenue work
        res.used = False
        return res
    if has_backtrack(stops):
        res.feasible = False
        res.reason = "route backtracks to a previously departed airstrip"
        return res
    res.used = True

    onboard_pax = 0
    onboard_kg = 0.0
    t = float(ac.available_from)         # depart base at duty start
    first_dep = t
    prev = strips[stops[0].code]

    for idx in range(1, len(stops)):
        st = stops[idx]

        # If the aircraft does not return to base, skip the final base-return leg
        if st.kind == "end" and not ac.return_to_base and prev.code != ac.base:
            break

        cur = strips[st.code]
        blk = block_minutes(prev, cur, ac.cruise_kts, lm)
        depart = t
        arrive = t + blk

        # --- daylight / field hours: must ARRIVE no later than field close ---
        if arrive > cur.close_min + 1e-6:
            res.feasible = False
            res.reason = f"{cur.code} closed: arrive {int(arrive)} > close {cur.close_min}"
            return res
        # if we arrive before the field opens we must wait (extends duty)
        service_start = max(arrive, float(cur.open_min)) if cur.daylight_only else arrive

        # --- aircraft availability window ---
        if service_start > ac.available_until + 1e-6:
            res.feasible = False
            res.reason = f"past aircraft availability ({int(service_start)} > {ac.available_until})"
            return res

        # record the leg (load shown is what was carried on it, i.e. before this stop's event)
        res.legs.append(LegReport(prev.code, cur.code, depart, arrive, blk,
                                   onboard_pax, empty=(onboard_pax == 0)))
        res.total_block_min += blk
        if onboard_pax == 0:
            res.empty_block_min += blk

        # --- apply the pickup/delivery event AT this stop ---
        if st.kind == "pickup":
            d = demands[st.demand_id]
            # Flight-tag restriction: aircraft assigned to a specific flight may
            # only serve demands from that flight (or its next_route follow-on).
            if ac.flight_tag is not None and d.flight_tag is not None:
                allowed = {ac.flight_tag}
                if ac.next_route:
                    allowed.add(ac.next_route)
                if d.flight_tag not in allowed:
                    res.feasible = False
                    res.reason = (
                        f"aircraft {ac.reg} (flight {ac.flight_tag}) "
                        f"cannot serve demand {d.id} (flight {d.flight_tag})"
                    )
                    return res
            if d.earliest_dep is not None and service_start < d.earliest_dep - 1e-6:
                service_start = float(d.earliest_dep)  # hold for the pax
            onboard_pax += d.pax
            onboard_kg += d.weight_kg(lm)
            # capacity is checked AFTER loading
            if onboard_pax > ac.seats:
                res.feasible = False
                res.reason = f"over seats at {cur.code} ({onboard_pax} > {ac.seats})"
                return res
            if onboard_kg > ac.payload_kg + 1e-6:
                res.feasible = False
                res.reason = f"over payload at {cur.code} ({onboard_kg:.0f} > {ac.payload_kg:.0f}kg)"
                return res
            res.seats_used_peak = max(res.seats_used_peak, onboard_pax)
            res.payload_peak_kg = max(res.payload_peak_kg, onboard_kg)
        elif st.kind == "delivery":
            d = demands[st.demand_id]
            onboard_pax -= d.pax
            onboard_kg -= d.weight_kg(lm)
            # connect_by is a HARD schedule-window constraint (±10 min of published arrival)
            if d.connect_by is not None:
                last_conn = max(d.connect_by)
                if service_start > last_conn + 1e-6:
                    res.feasible = False
                    _t = int(service_start)
                    res.reason = (
                        f"late delivery at {cur.code}: "
                        f"arrive {_t//60:02d}:{_t%60:02d} > "
                        f"window {last_conn//60:02d}:{last_conn%60:02d}"
                    )
                    return res

        # depart after ground time (no turnaround at the closing depot leg)
        turn = 0 if st.kind == "end" else ac.turnaround_min
        t = service_start + turn
        prev = cur

    res.duty_span_min = t - first_dep
    if res.duty_span_min > ac.max_duty_min + 1e-6:
        res.feasible = False
        res.reason = f"crew duty exceeded ({int(res.duty_span_min)} > {ac.max_duty_min} min)"
        return res
    return res


# --------------------------------------------------------------------------- #
# THE OBJECTIVE  --  the single source of truth both engines minimize.
# --------------------------------------------------------------------------- #
def fill_idle_aircraft(
    route_stops: dict[str, list[Stop]],
    dropped: list[Demand],
    ac_by_reg: dict[str, Aircraft],
    strips: dict[str, Airstrip],
    demands: dict[str, Demand],
    w: Weights,
    lm: LoadModel,
) -> tuple[dict[str, list[Stop]], list[Demand]]:
    """Second-pass greedy assignment for demands that the main solve dropped.
    Accepts any feasible insertion — no cost threshold — so aircraft with
    remaining duty time and capacity are never left idle while passengers are
    unserved. Connection-sensitive and larger groups are prioritised."""
    pending = sorted(
        dropped,
        key=lambda d: (d.connect_by is None,
                       min(d.connect_by) if d.connect_by else 10 ** 9,
                       -d.pax),
    )
    still_dropped: list[Demand] = []
    for d in pending:
        best: tuple | None = None
        for reg, stops in route_stops.items():
            ac = ac_by_reg[reg]
            base_res = evaluate_route(ac, stops, strips, demands, lm)
            n = len(stops)
            for i in range(1, n):
                for j in range(i + 1, n + 1):
                    cand = stops[:]
                    cand.insert(i, Stop(d.origin, "pickup", d.id))
                    cand.insert(j, Stop(d.dest, "delivery", d.id))
                    r = evaluate_route(ac, cand, strips, demands, lm)
                    if not r.feasible:
                        continue
                    delta = (
                        w.w_block * (r.total_block_min - base_res.total_block_min)
                        + w.w_empty * (r.empty_block_min - base_res.empty_block_min)
                        + w.w_connection * (r.connection_late_min - base_res.connection_late_min)
                        + w.w_aircraft * ((1 if r.used else 0) - (1 if base_res.used else 0))
                    )
                    if best is None or delta < best[0]:
                        best = (delta, reg, cand)
        if best is not None:
            _, reg, new_stops = best
            route_stops[reg] = new_stops
        else:
            still_dropped.append(d)
    return route_stops, still_dropped


def plan_cost(route_results: dict[str, RouteResult], dropped_pax: int, w: Weights):
    total_block = sum(r.total_block_min for r in route_results.values())
    empty_block = sum(r.empty_block_min for r in route_results.values())
    late = sum(r.connection_late_min for r in route_results.values())
    used = sum(1 for r in route_results.values() if r.used)
    cost = (w.w_block * total_block
            + w.w_empty * empty_block
            + w.w_aircraft * used
            + w.w_connection * late
            + w.w_drop * dropped_pax)
    breakdown = {
        "block_min": round(total_block, 1),
        "empty_block_min": round(empty_block, 1),
        "aircraft_used": used,
        "connection_late_min": round(late, 1),
        "dropped_pax": dropped_pax,
        "total_cost": round(cost, 1),
    }
    return cost, breakdown


# --------------------------------------------------------------------------- #
# PRETTY PRINTING
# --------------------------------------------------------------------------- #
def hhmm(m: float) -> str:
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"

def print_plan(routes: dict[str, tuple[Aircraft, list[Stop], RouteResult]],
               dropped: list[Demand], strips: dict[str, Airstrip],
               demands: dict[str, Demand], w: Weights, lm: LoadModel):
    print("=" * 74)
    print("AURIC AIR — DAILY ROUTE PLAN")
    print("=" * 74)
    total_pax = sum(d.pax for d in demands.values())
    served_pax = total_pax - sum(d.pax for d in dropped)
    for reg, (ac, stops, res) in routes.items():
        if not res.used:
            continue
        pax_carried = sum(demands[s.demand_id].pax for s in stops if s.kind == "pickup")
        seat_miles_avail = 0.0
        seat_miles_used = 0.0
        print(f"\n  {reg}  ({ac.type}, base {ac.base}, {ac.seats} seats / "
              f"{ac.payload_kg:.0f}kg)")
        path = " -> ".join(
            s.code + ("" if s.kind in ("start", "end")
                      else f"[{'+' if s.kind=='pickup' else '-'}{demands[s.demand_id].pax} {s.demand_id}]")
            for s in stops)
        print(f"    route : {path}")
        for lg in res.legs:
            if lg.frm == lg.to and lg.block_min == 0:
                continue  # same-field "pick up more pax" event, not a real leg
            tag = "  (empty/positioning)" if lg.empty else ""
            print(f"      {hhmm(lg.depart_min)} {lg.frm} -> {lg.to} "
                  f"{hhmm(lg.arrive_min)}  {int(lg.block_min):>3d}min  "
                  f"{lg.pax_onboard:>2d} pax{tag}")
        lf = (res.seats_used_peak / ac.seats * 100) if ac.seats else 0
        print(f"    block {int(res.total_block_min)}min | empty {int(res.empty_block_min)}min "
              f"| duty {hhmm(res.duty_span_min)} | peak {res.seats_used_peak}/{ac.seats} "
              f"seats ({lf:.0f}%) | {pax_carried} pax carried")
        if res.connection_late_min:
            print(f"    !! connection lateness: {int(res.connection_late_min)} min")

    if dropped:
        print("\n  UNSERVED (no feasible aircraft/slot — handle manually or add capacity):")
        for d in dropped:
            print(f"    {d.id}: {d.origin}->{d.dest} {d.pax}pax"
                  + (f" connect_by {';'.join(hhmm(t) for t in d.connect_by)}" if d.connect_by else ""))

    rr = {reg: res for reg, (_, _, res) in routes.items()}
    _, bd = plan_cost(rr, sum(d.pax for d in dropped), w)
    print("\n" + "-" * 74)
    print(f"  SERVED {served_pax}/{total_pax} pax  |  aircraft used {bd['aircraft_used']}  "
          f"|  total block {bd['block_min']}min  |  empty {bd['empty_block_min']}min")
    print(f"  objective cost = {bd['total_cost']}  "
          f"(block {w.w_block}xb + empty {w.w_empty}xe + ac {w.w_aircraft}xn "
          f"+ late {w.w_connection}xl + drop {w.w_drop}xd)")
    print("=" * 74)


# --------------------------------------------------------------------------- #
# CSV EXPORT  —  ops-friendly spreadsheets
# --------------------------------------------------------------------------- #
def write_csv_outputs(
    routes: dict[str, tuple[Aircraft, list[Stop], RouteResult]],
    dropped: list[Demand],
    strips: dict[str, Airstrip],
    demands: dict[str, Demand],
    output_dir: str,
):
    """Write two CSVs to output_dir:
      schedule.csv  — one row per actual flight leg, easy to brief flight crews
      bookings.csv  — one row per booking, shows which aircraft carries each group
    """
    os.makedirs(output_dir, exist_ok=True)
    date = next(iter(demands.values())).date if demands else ""

    # ---- schedule.csv -------------------------------------------------------
    sched_rows = []
    for reg, (ac, stops, res) in routes.items():
        if not res.used:
            continue
        leg_no = 0
        for lg in res.legs:
            if lg.block_min == 0:
                continue  # same-field pax event, not a real flight
            leg_no += 1
            sched_rows.append({
                "Date": date,
                "Aircraft Reg": reg,
                "Aircraft Type": ac.type,
                "Leg #": leg_no,
                "Depart": hhmm(lg.depart_min),
                "Arrive": hhmm(lg.arrive_min),
                "From Code": lg.frm,
                "From": strips[lg.frm].name if lg.frm in strips else lg.frm,
                "To Code": lg.to,
                "To": strips[lg.to].name if lg.to in strips else lg.to,
                "Pax On Board": lg.pax_onboard,
                "Block (min)": int(lg.block_min),
                "Positioning": "Yes" if lg.empty else "No",
            })

    sched_path = os.path.join(output_dir, "schedule.csv")
    with open(sched_path, "w", newline="") as f:
        if sched_rows:
            w = csv.DictWriter(f, fieldnames=list(sched_rows[0].keys()))
            w.writeheader()
            w.writerows(sched_rows)

    # ---- bookings.csv -------------------------------------------------------
    booking_rows = []
    for reg, (ac, stops, _res) in routes.items():
        for s in stops:
            if s.kind != "pickup":
                continue
            d = demands[s.demand_id]
            cb = " / ".join(hhmm(t) for t in d.connect_by) if d.connect_by else ""
            booking_rows.append({
                "Booking ID": d.id,
                "Date": d.date,
                "Pax": d.pax,
                "From Code": d.origin,
                "From": strips[d.origin].name if d.origin in strips else d.origin,
                "To Code": d.dest,
                "To": strips[d.dest].name if d.dest in strips else d.dest,
                "Aircraft": reg,
                "Connect By": cb,
                "Passengers": " | ".join(d.passengers) if d.passengers else "",
                "Status": "Served",
            })
    for d in dropped:
        cb = " / ".join(hhmm(t) for t in d.connect_by) if d.connect_by else ""
        booking_rows.append({
            "Booking ID": d.id,
            "Date": d.date,
            "Pax": d.pax,
            "From Code": d.origin,
            "From": strips[d.origin].name if d.origin in strips else d.origin,
            "To Code": d.dest,
            "To": strips[d.dest].name if d.dest in strips else d.dest,
            "Aircraft": "UNSERVED",
            "Connect By": cb,
            "Passengers": " | ".join(d.passengers) if d.passengers else "",
            "Status": "UNSERVED — handle manually",
        })

    booking_rows.sort(key=lambda r: (r["Aircraft"], r["Booking ID"]))
    bookings_path = os.path.join(output_dir, "bookings.csv")
    with open(bookings_path, "w", newline="") as f:
        if booking_rows:
            w = csv.DictWriter(f, fieldnames=list(booking_rows[0].keys()))
            w.writeheader()
            w.writerows(booking_rows)

    print(f"\nOutputs written to {output_dir}\\")
    print(f"  schedule.csv  — {len(sched_rows)} flight legs")
    print(f"  bookings.csv  — {len(booking_rows)} bookings ({len(dropped)} unserved)")
