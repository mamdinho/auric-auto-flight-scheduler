"""
demo_solver.py — a dependency-free PDPTW solver (runs with plain Python).

This is NOT the production optimizer. It is a greedy "cheapest-insertion +
improvement" heuristic that optimizes the EXACT SAME objective and obeys the
EXACT SAME hard constraints as the OR-Tools engine in auric_route_planner.py.

Why it exists:
  1. It runs with zero install, so you (and Ops) can see a real plan today.
  2. It proves the whole pipeline — your CSVs -> constraints -> a flyable plan.
  3. It's a permanent fallback if OR-Tools is ever unavailable.

OR-Tools will find BETTER plans (it searches far more of the solution space),
but it cannot find a DIFFERENT definition of "better" — the objective lives in
planner_core.py and both engines import it.

Usage:
    python3 demo_solver.py [data_dir]
"""

from __future__ import annotations
import sys, json, os
import planner_core as pc


def solve(strips, fleet, demand_list, w: pc.Weights, lm: pc.LoadModel):
    demands = {d.id: d for d in demand_list}
    # one route per aircraft: [start@base, end@base]
    routes: dict[str, list[pc.Stop]] = {
        ac.reg: [pc.Stop(ac.base, "start"), pc.Stop(ac.base, "end")] for ac in fleet
    }
    ac_by_reg = {ac.reg: ac for ac in fleet}

    def route_result(reg, stops):
        return pc.evaluate_route(ac_by_reg[reg], stops, strips, demands, lm)

    def current_total():
        rr = {reg: route_result(reg, st) for reg, st in routes.items()}
        return rr

    # Insertion priority: hard-deadline demands first (earliest), then heaviest.
    order = sorted(
        demand_list,
        key=lambda d: (d.connect_by is None, min(d.connect_by) if d.connect_by else 10**9, -d.pax),
    )

    dropped: list[pc.Demand] = []

    def best_insertion(d: pc.Demand):
        """Find the (reg, i, j) that inserts demand d at least added cost."""
        best = None  # (delta, reg, new_stops)
        for reg, stops in routes.items():
            base_res = route_result(reg, stops)
            base_block = base_res.total_block_min
            base_empty = base_res.empty_block_min
            base_late = base_res.connection_late_min
            base_used = 1 if base_res.used else 0
            n = len(stops)
            for i in range(1, n):          # pickup goes before position i (i.e. index i)
                for j in range(i + 1, n + 1):  # delivery before position j (after pickup)
                    cand = stops[:]
                    cand.insert(i, pc.Stop(d.origin, "pickup", d.id))
                    cand.insert(j, pc.Stop(d.dest, "delivery", d.id))
                    r = route_result(reg, cand)
                    if not r.feasible:
                        continue
                    # marginal objective cost of THIS aircraft's change
                    delta = (w.w_block * (r.total_block_min - base_block)
                             + w.w_empty * (r.empty_block_min - base_empty)
                             + w.w_connection * (r.connection_late_min - base_late)
                             + w.w_aircraft * ((1 if r.used else 0) - base_used))
                    if best is None or delta < best[0]:
                        best = (delta, reg, cand)
        return best

    for d in order:
        ins = best_insertion(d)
        # cost of dropping vs cost of best insertion
        if ins is None or ins[0] >= w.w_drop * d.pax:
            dropped.append(d)
        else:
            _, reg, new_stops = ins
            routes[reg] = new_stops

    # ---- improvement: try to relocate each demand to a cheaper slot ----
    def total_cost():
        rr = current_total()
        return pc.plan_cost(rr, sum(x.pax for x in dropped), w)[0]

    improved = True
    rounds = 0
    while improved and rounds < 6:
        improved = False
        rounds += 1
        for d in list(demands.values()):
            if d in dropped:
                continue
            # find which route currently holds d, remove it
            holder = None
            for reg, stops in routes.items():
                if any(s.demand_id == d.id for s in stops):
                    holder = reg
                    break
            if holder is None:
                continue
            before = total_cost()
            saved = routes[holder]
            routes[holder] = [s for s in saved if s.demand_id != d.id]
            ins = best_insertion(d)
            if ins is None:
                routes[holder] = saved  # revert
                continue
            _, reg, new_stops = ins
            routes[reg] = new_stops
            if total_cost() < before - 1e-6:
                improved = True
            else:
                # revert both
                routes[reg] = [s for s in routes[reg] if s.demand_id != d.id]
                routes[holder] = saved

    # ---- swap pass: exchange two demands between their routes at once ----
    # Single-demand relocation can get stuck when neither demand fits into the
    # other's route alone (e.g. tight seats) but swapping both frees enough
    # capacity on each side. Bounded to keep runtime predictable on busy days.
    served_demands = [d for d in demand_list if d not in dropped]
    if len(served_demands) <= 80:
        for _ in range(2):
            swapped_any = False
            for i in range(len(served_demands)):
                for j in range(i + 1, len(served_demands)):
                    da, db = served_demands[i], served_demands[j]
                    ra = next((reg for reg, st_ in routes.items()
                              if any(s.demand_id == da.id for s in st_)), None)
                    rb = next((reg for reg, st_ in routes.items()
                              if any(s.demand_id == db.id for s in st_)), None)
                    if ra is None or rb is None or ra == rb:
                        continue
                    before = total_cost()
                    saved_a, saved_b = routes[ra], routes[rb]
                    routes[ra] = [s for s in saved_a if s.demand_id != da.id]
                    routes[rb] = [s for s in saved_b if s.demand_id != db.id]
                    ins_a = best_insertion(da)
                    ins_b = best_insertion(db)
                    if ins_a is None or ins_b is None:
                        routes[ra], routes[rb] = saved_a, saved_b
                        continue
                    _, reg_a, stops_a = ins_a
                    routes[reg_a] = stops_a
                    _, reg_b, stops_b = ins_b
                    routes[reg_b] = stops_b
                    if total_cost() < before - 1e-6:
                        swapped_any = True
                    else:
                        routes[ra] = saved_a
                        routes[rb] = saved_b
            if not swapped_any:
                break

    # Second pass: assign any remaining dropped demands to aircraft that still
    # have capacity and duty time — accepts any feasible slot (no cost threshold)
    # so that no aircraft sits idle while passengers are unserved.
    routes, dropped = pc.fill_idle_aircraft(
        routes, dropped, ac_by_reg, strips, demands, w, lm)

    final = {reg: (ac_by_reg[reg], stops, route_result(reg, stops))
             for reg, stops in routes.items()}
    return final, dropped


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "output")
    os.makedirs(out_dir, exist_ok=True)
    data = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "data")
    strips = pc.load_airstrips(f"{data}/airstrips.csv")
    fleet = pc.load_fleet(f"{data}/fleet.csv")
    manifest = pc.load_manifest(f"{data}/manifest.csv")
    w, lm = pc.Weights(), pc.LoadModel()

    routes, dropped = solve(strips, fleet, manifest, w, lm)
    demands = {d.id: d for d in manifest}
    pc.print_plan(routes, dropped, strips, demands, w, lm)

    # structured output for downstream systems / a UI
    out = {"engine": "heuristic", "aircraft": [], "unserved": []}
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
            "duty_span_min": round(res.duty_span_min),
            "peak_seats": res.seats_used_peak,
        })
    for d in dropped:
        out["unserved"].append({"id": d.id, "od": f"{d.origin}-{d.dest}", "pax": d.pax})
    with open(os.path.join(out_dir, "plan_output.json"), "w") as f:
        json.dump(out, f, indent=2)

    pc.write_csv_outputs(routes, dropped, strips, demands, out_dir)


if __name__ == "__main__":
    main()
