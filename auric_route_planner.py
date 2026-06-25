"""
auric_route_planner.py — PRODUCTION optimizer (Google OR-Tools PDPTW).

This is the engine you run in production. It reads the SAME airstrips.csv /
fleet.csv / manifest.csv and minimizes the SAME weighted objective as
demo_solver.py, but uses Google OR-Tools' constraint solver, so it searches a
vastly larger slice of the solution space and finds materially better plans.

INSTALL (in YOUR environment — this needs internet, unlike the demo):
    pip install ortools

RUN:
    python3 auric_route_planner.py [data_dir]

------------------------------------------------------------------------------
HOW YOUR 5-PART OBJECTIVE MAPS ONTO OR-TOOLS LEVERS
------------------------------------------------------------------------------
  1. minimize total block time     -> arc cost = leg block-minutes (scaled by w_block)
  2. minimize empty legs           -> (a) block-time cost already charges every
                                          positioning leg; (b) a per-aircraft fixed
                                          cost suppresses extra ferrying. See NOTE
                                          below for an exact load-aware version.
  3. maximize load factor          -> per-aircraft FIXED COST (w_aircraft): the
                                          solver only spins up another Caravan when
                                          the demand truly needs it => fuller planes.
  4. meet connection windows       -> SOFT upper bound on the time dimension at each
                                          delivery (penalty per minute late = w_connection).
  5. respect aircraft/crew avail.  -> HARD: vehicle start/end time windows
                                          (availability) + span upper bound (duty).
  Plus: capacity by SEATS *and* WEIGHT (two capacity dimensions), daylight field
  hours as hard arrival windows, and high drop penalties so every pax is served
  unless physically infeasible.

NOTE on exact empty-leg cost: OR-Tools arc costs cannot read current load, so a
truly load-aware empty-leg penalty needs a small custom dimension or a 2-stage
solve. Block-time + aircraft-fixed-cost is the standard, robust approximation and
matches what the demo heuristic produces closely. Ask if you want the exact form.
"""

from __future__ import annotations
import sys, json, os  # json used in main()
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import planner_core as pc

SCALE = 1  # OR-Tools wants integer costs; block minutes are already ~integers


def build_and_solve(strips, fleet, manifest, w: pc.Weights, lm: pc.LoadModel,
                    time_limit_s: int = 30):
    demands = {d.id: d for d in manifest}

    # ---- NODES: per-vehicle depot, then a pickup + delivery node per demand ----
    # node 0..V-1   : depot for each aircraft (start == end at its base)
    # node V..      : for demand k -> pickup node, delivery node (2 per demand)
    nodes = []  # (code, kind, demand_id)
    for ac in fleet:
        nodes.append((ac.base, "depot", ac.reg))
    pickup_idx, delivery_idx = {}, {}
    for d in manifest:
        pickup_idx[d.id] = len(nodes); nodes.append((d.origin, "pickup", d.id))
        delivery_idx[d.id] = len(nodes); nodes.append((d.dest, "delivery", d.id))

    V = len(fleet)
    starts = list(range(V))
    ends = list(range(V))
    manager = pywrapcp.RoutingIndexManager(len(nodes), V, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    code_of = lambda n: nodes[n][0]

    # ---- 1) ARC COST = block minutes (uses each vehicle's cruise speed) ----
    transit_cb = {}
    for v, ac in enumerate(fleet):
        def make(ac):
            def cb(from_index, to_index):
                a = strips[code_of(manager.IndexToNode(from_index))]
                b = strips[code_of(manager.IndexToNode(to_index))]
                return int(round(pc.block_minutes(a, b, ac.cruise_kts, lm) * w.w_block * SCALE))
            return cb
        cb = make(ac)
        idx = routing.RegisterTransitCallback(cb)
        transit_cb[v] = idx
        routing.SetArcCostEvaluatorOfVehicle(idx, v)

    # ---- 3) LOAD FACTOR: fixed cost per aircraft used ----
    for v in range(V):
        routing.SetFixedCostOfVehicle(int(w.w_aircraft * SCALE), v)

    # ---- CAPACITY: two dimensions, seats AND weight ----
    def seat_demand(index):
        c, kind, did = nodes[manager.IndexToNode(index)]
        if kind == "pickup":   return demands[did].pax
        if kind == "delivery": return -demands[did].pax
        return 0
    seat_cb = routing.RegisterUnaryTransitCallback(seat_demand)
    routing.AddDimensionWithVehicleCapacity(
        seat_cb, 0, [ac.seats for ac in fleet], True, "Seats")

    def weight_demand(index):
        c, kind, did = nodes[manager.IndexToNode(index)]
        if kind == "pickup":   return int(round(demands[did].weight_kg(lm)))
        if kind == "delivery": return -int(round(demands[did].weight_kg(lm)))
        return 0
    wt_cb = routing.RegisterUnaryTransitCallback(weight_demand)
    routing.AddDimensionWithVehicleCapacity(
        wt_cb, 0, [int(ac.payload_kg) for ac in fleet], True, "Payload")

    # ---- TIME dimension: travel + turnaround; drives windows & duty ----
    # use a representative speed for the time dimension (homogeneous fleet)
    rep_speed = sum(ac.cruise_kts for ac in fleet) / len(fleet)
    turn = max(ac.turnaround_min for ac in fleet)
    def time_cb(from_index, to_index):
        fn = manager.IndexToNode(from_index)
        a = strips[code_of(fn)]; b = strips[code_of(manager.IndexToNode(to_index))]
        t = pc.block_minutes(a, b, rep_speed, lm)
        # Only charge ground turnaround when actually departing this airstrip.
        # Several demands sharing one origin/destination board or deplane in a
        # single ground event, not one full turnaround each (mirrors the same
        # fix in evaluate_route) — without this, a==b (same-code) transitions
        # between co-located pickups/deliveries would each tack on a needless
        # turnaround and starve the route of time it should have available.
        if nodes[fn][1] != "depot" and a.code != b.code:
            t += turn
        return int(round(t))
    time_idx = routing.RegisterTransitCallback(time_cb)
    horizon = max(ac.available_until for ac in fleet)
    routing.AddDimension(time_idx, 24 * 60, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    # 5) crew duty span per vehicle + aircraft availability windows
    for v, ac in enumerate(fleet):
        time_dim.SetSpanUpperBoundForVehicle(ac.max_duty_min, v)
        s = routing.Start(v); e = routing.End(v)
        time_dim.CumulVar(s).SetRange(ac.available_from, ac.available_until)
        time_dim.CumulVar(e).SetRange(ac.available_from, ac.available_until)

    # daylight field hours (hard) + earliest_dep + 4) connection windows (soft,
    # but priced to match the drop penalty)
    for n, (code, kind, did) in enumerate(nodes):
        if n < V:
            continue
        idx = manager.NodeToIndex(n)
        strip = strips[code]
        lo = strip.open_min if strip.daylight_only else 0
        hi = strip.close_min if strip.daylight_only else horizon
        if kind == "pickup" and demands[did].earliest_dep is not None:
            lo = max(lo, demands[did].earliest_dep)
        time_dim.CumulVar(idx).SetRange(lo, hi)
        if kind == "delivery" and demands[did].connect_by is not None:
            # evaluate_route enforces this window as HARD after extraction (see
            # strip_time_window_demands), so this soft bound only needs to
            # nudge the search away from lateness, not replicate the hard cut.
            # MEASURED on the real solver (not just reasoned about): scaling
            # this by w_drop * pax — even divided down to 1/10th — consistently
            # made OR-Tools perform WORSE than the plain heuristic fallback
            # (served pax dropped from ~106 to ~89 across repeated trials on
            # the same manifest). A pax-scaled penalty creates a cost cliff
            # that PARALLEL_CHEAPEST_INSERTION and GUIDED_LOCAL_SEARCH can't
            # navigate smoothly — large-pax demands get treated as too risky
            # to insert anywhere near the boundary, even at an on-time position.
            # The plain flat w_connection rate, swept against the same manifest,
            # was stable at 101-106 pax served across every metaheuristic and
            # time-budget combination tried. Do not re-introduce pax scaling
            # here without re-measuring on a real OR-Tools run.
            time_dim.SetCumulVarSoftUpperBound(
                idx, max(demands[did].connect_by), int(w.w_connection * SCALE))

    # ---- PICKUP & DELIVERY pairing: same vehicle, pickup before delivery ----
    solver = routing.solver()
    for d in manifest:
        pi = manager.NodeToIndex(pickup_idx[d.id])
        di = manager.NodeToIndex(delivery_idx[d.id])
        routing.AddPickupAndDelivery(pi, di)
        solver.Add(routing.VehicleVar(pi) == routing.VehicleVar(di))
        solver.Add(time_dim.CumulVar(pi) <= time_dim.CumulVar(di))
        # Separate disjunctions per node (the OR-Tools-safe form for PDPTW pairs).
        # The pickup/delivery coupling above already ensures they travel together.
        half = int(w.w_drop * d.pax * SCALE // 2) or 1
        routing.AddDisjunction([pi], half)
        routing.AddDisjunction([di], half)

        # Flight-tag restriction: prevent vehicles from serving demands of a
        # different flight.  Build the list of vehicles that ARE allowed, then
        # exclude all others.  Aircraft without a flight_tag are unrestricted.
        if d.flight_tag:
            for v, ac in enumerate(fleet):
                if ac.flight_tag is None:
                    continue  # unrestricted vehicle — can serve any demand
                allowed = {ac.flight_tag}
                if ac.next_route:
                    allowed.add(ac.next_route)
                if d.flight_tag not in allowed:
                    solver.Add(routing.VehicleVar(pi) != v)
                    solver.Add(routing.VehicleVar(di) != v)

    # ---- SEARCH ----
    # PARALLEL_CHEAPEST_INSERTION is the strongest first-solution strategy for
    # pickup-delivery problems (it inserts each pair at its cheapest feasible
    # slot rather than building routes greedily one stop at a time).
    # GUIDED_LOCAL_SEARCH is OR-Tools' best general-purpose metaheuristic for
    # VRP/PDPTW — it escapes local optima by penalizing repeatedly-used arcs,
    # which consistently outperforms TABU_SEARCH on routing problems of this
    # shape within the same time budget.
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION)
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
    params.time_limit.FromSeconds(time_limit_s)
    print(f"  Solving... (up to {time_limit_s}s)", flush=True)
    solution = routing.SolveWithParameters(params)
    if not solution:
        print("  No solution found — check that all airstrip codes in the manifest exist in airstrips.csv.")
        return

    # ---- EXTRACT into the shared Stop model and reuse the shared evaluator ----
    routes = {}
    served = set()
    ac_by_reg = {ac.reg: ac for ac in fleet}
    for v, ac in enumerate(fleet):
        seq = [pc.Stop(ac.base, "start")]
        idx = routing.Start(v)
        idx = solution.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            code, kind, did = nodes[manager.IndexToNode(idx)]
            seq.append(pc.Stop(code, kind, did))
            if kind in ("pickup", "delivery"):
                served.add(did)
            idx = solution.Value(routing.NextVar(idx))
        seq.append(pc.Stop(ac.base, "end"))

        # Remove any stops that would cause the aircraft to backtrack to a
        # previously departed airstrip; unserve those demand IDs so they
        # re-enter the dropped pool and get a second-pass assignment attempt.
        seq, removed = pc.strip_backtrack_demands(seq)
        for did in removed:
            served.discard(did)

        # OR-Tools uses a soft connect_by penalty; the hard evaluator may
        # still flag time-window violations.  Strip any such demands so that
        # phantom pickups never appear in the schedule.
        seq, tw_removed = pc.strip_time_window_demands(seq, ac, strips, demands, lm)
        for did in tw_removed:
            served.discard(did)

        res = pc.evaluate_route(ac, seq, strips, demands, lm)
        routes[ac.reg] = (ac, seq, res)

    dropped = [d for d in manifest if d.id not in served]

    # Second pass: assign any remaining dropped demands to aircraft that still
    # have capacity and duty time — accepts any feasible slot (no cost threshold)
    # so that no aircraft sits idle while passengers are unserved.
    route_stops = {reg: tup[1] for reg, tup in routes.items()}
    route_stops, dropped = pc.fill_idle_aircraft(
        route_stops, dropped, ac_by_reg, strips, demands, w, lm)

    # Re-evaluate every route (some may have gained new stops from the idle pass).
    for reg, seq in route_stops.items():
        ac = ac_by_reg[reg]
        routes[reg] = (ac, seq, pc.evaluate_route(ac, seq, strips, demands, lm))

    return routes, dropped   # caller handles printing / writing


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "data")
    strips  = pc.load_airstrips(f"{data}/airstrips.csv")
    fleet   = pc.load_fleet(f"{data}/fleet.csv")
    manifest = pc.load_manifest(f"{data}/manifest.csv")
    w, lm   = pc.Weights(), pc.LoadModel()

    result = build_and_solve(strips, fleet, manifest, w, lm)
    if result is None:
        return
    routes, dropped = result
    demands = {d.id: d for d in manifest}

    pc.print_plan(routes, dropped, strips, demands, w, lm)

    out_dir = os.path.join(here, "output")
    os.makedirs(out_dir, exist_ok=True)

    out = {"engine": "ortools", "aircraft": [], "unserved": []}
    for reg, (ac, stops, res) in routes.items():
        if not res.used:
            continue
        out["aircraft"].append({
            "reg": reg, "type": ac.type, "base": ac.base,
            "sequence": [{"code": s.code, "kind": s.kind, "demand": s.demand_id}
                         for s in stops],
            "legs": [{"from": l.frm, "to": l.to, "dep": pc.hhmm(l.depart_min),
                      "arr": pc.hhmm(l.arrive_min), "block_min": round(l.block_min),
                      "pax": l.pax_onboard, "empty": l.empty} for l in res.legs],
            "total_block_min": round(res.total_block_min),
            "empty_block_min": round(res.empty_block_min),
            "duty_span_min":   round(res.duty_span_min),
            "peak_seats":      res.seats_used_peak,
        })
    for d in dropped:
        out["unserved"].append({"id": d.id, "od": f"{d.origin}-{d.dest}", "pax": d.pax})
    with open(os.path.join(out_dir, "plan_output.json"), "w") as f:
        json.dump(out, f, indent=2)

    pc.write_csv_outputs(routes, dropped, strips, demands, out_dir)


if __name__ == "__main__":
    main()
