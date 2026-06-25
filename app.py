"""
app.py — Streamlit web interface for the Auric Air Flight Scheduler.

Three tabs:
  1. Build Manifest  — select flight(s) from the route master, upload PDFs,
                       accumulate O-D demands into a single day's manifest.
  2. Run Optimizer   — run OR-Tools / heuristic on the manifest; download outputs.
  3. Manage Routes   — admin: add / edit the fixed route and leg timings for
                       every flight number.

Run locally:  streamlit run app.py
Deploy:       push to GitHub, connect at streamlit.io/cloud
"""

import io
import os
import sys
import tempfile
from datetime import datetime

import pandas as pd
import streamlit as st

here = os.path.dirname(os.path.abspath(__file__))
if here not in sys.path:
    sys.path.insert(0, here)

import planner_core as pc
import ingest
import auth


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _split_large_demands(manifest: list, fleet: list) -> list:
    """Split any demand with pax exceeding the largest aircraft ELIGIBLE to
    serve it into sub-demands that each fit.

    The seat cap must be computed per-demand, not globally across the whole
    day's fleet: a demand tagged for a flight whose own aircraft are all
    12-seat Caravans can never be served by a 50-seat Dash-8 sitting in a
    DIFFERENT flight's assignment (flight-tag ownership forbids it), so using
    the day-wide max seat count would leave it unsplit and permanently
    unservable regardless of how good the optimizer is.
    """
    def _eligible_max_seats(d) -> int:
        # Mirror evaluate_route's restriction exactly: a demand with no
        # flight_tag (e.g. a manually uploaded manifest.csv) is unrestricted,
        # so every aircraft is eligible -- do NOT fall through to the
        # next_route comparison below, where None == None would wrongly
        # match every aircraft that simply has no next_route set.
        if d.flight_tag is None:
            eligible = [ac.seats for ac in fleet]
        else:
            eligible = [
                ac.seats for ac in fleet
                if ac.flight_tag is None
                or ac.flight_tag == d.flight_tag
                or ac.next_route == d.flight_tag
            ]
        return max(eligible) if eligible else 12

    result = []
    for d in manifest:
        max_seats = _eligible_max_seats(d)
        if d.pax <= max_seats:
            result.append(d)
            continue
        pax_names = list(d.passengers) if d.passengers else []
        remaining, part = d.pax, 1
        while remaining > 0:
            cpax = min(remaining, max_seats)
            result.append(pc.Demand(
                id=f"{d.id}-p{part}",
                date=d.date,
                origin=d.origin,
                dest=d.dest,
                pax=cpax,
                connect_by=d.connect_by,
                earliest_dep=d.earliest_dep,
                passengers=pax_names[:cpax] if pax_names else None,
                flight_tag=d.flight_tag,  # preserve flight ownership through splits
            ))
            pax_names = pax_names[cpax:]
            remaining -= cpax
            part += 1
    return result


def run_solver(strips, fleet, manifest, w, lm, time_limit_s: int = 30):
    """Try OR-Tools first (it searches far more of the solution space and finds
    materially shorter/more efficient plans); fall back to the heuristic only if
    OR-Tools is unavailable or errors. Returns (routes, dropped, engine, fallback_reason)
    — fallback_reason is None when OR-Tools succeeded, so the caller can warn ops
    when they're seeing the lower-quality fallback instead of silently downgrading."""
    fallback_reason = None
    try:
        from auric_route_planner import build_and_solve
        result = build_and_solve(strips, fleet, manifest, w, lm, time_limit_s=time_limit_s)
        if result is not None:
            return result[0], result[1], "OR-Tools", None
        fallback_reason = "OR-Tools found no solution within the time limit"
    except Exception as e:
        fallback_reason = f"OR-Tools error: {e}"
    from demo_solver import solve
    routes, dropped = solve(strips, fleet, manifest, w, lm)
    return routes, dropped, "Heuristic", fallback_reason


# --------------------------------------------------------------------------- #
# Page config
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="Auric Air Scheduler", page_icon="✈", layout="wide")
st.title("Auric Air — Daily Flight Scheduler")

# --------------------------------------------------------------------------- #
# Login gate — nothing below this renders until authenticated
# --------------------------------------------------------------------------- #

# data/users.json is gitignored (it holds password hashes), so a brand-new
# deploy (e.g. a fresh Streamlit Cloud instance) starts with zero accounts.
# Bootstrap the first admin from an env var rather than a hardcoded password
# so this never crashes on a fresh checkout and never ships a credential in
# source control.
if not auth.load_users():
    _bootstrap_pw = os.environ.get("AURIC_BOOTSTRAP_PW")
    if _bootstrap_pw:
        auth.ensure_bootstrap_admin("it@auricair.com", _bootstrap_pw)
    else:
        st.error(
            "No user accounts exist yet. Set the **AURIC_BOOTSTRAP_PW** environment "
            "variable (or Streamlit Cloud secret) and reload this page to create the "
            "initial admin account (it@auricair.com)."
        )
        st.stop()

if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

if st.session_state["auth_user"] is None:
    st.subheader("Sign in")
    with st.form("login_form"):
        _login_email = st.text_input("Email")
        _login_pw = st.text_input("Password", type="password")
        _login_submit = st.form_submit_button("Log in", type="primary")
    if _login_submit:
        _user = auth.authenticate(_login_email, _login_pw)
        if _user:
            st.session_state["auth_user"] = _user
            st.rerun()
        else:
            st.error("Incorrect email or password.")
    st.stop()

_me = st.session_state["auth_user"]
_is_admin = _me["role"] == "admin"

# --------------------------------------------------------------------------- #
# Sidebar — always-visible instructions
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.success(f"Signed in as **{_me['username']}** ({_me['role']})")
    if st.button("Log out"):
        st.session_state["auth_user"] = None
        st.rerun()

    with st.expander("Change my password"):
        with st.form("change_pw_form"):
            _cur_pw = st.text_input("Current password", type="password", key="cur_pw")
            _new_pw = st.text_input("New password", type="password", key="new_pw")
            _new_pw2 = st.text_input("Confirm new password", type="password", key="new_pw2")
            _change_submit = st.form_submit_button("Update password")
        if _change_submit:
            if auth.authenticate(_me["username"], _cur_pw) is None:
                st.error("Current password is incorrect.")
            elif _new_pw != _new_pw2:
                st.error("New passwords do not match.")
            else:
                try:
                    auth.change_password(_me["username"], _new_pw)
                    st.success("Password updated.")
                except ValueError as e:
                    st.error(str(e))

    st.divider()
    st.header("How to use this app")
    st.markdown(
        """
There are tabs at the top of the page for each part of the daily workflow:

---

### Tab 1 — Build Manifest *(daily)*
Turn today's ops documents into an optimiser-ready manifest.

1. **Upload Booking Analysis CSV** (once per day, optional — used to cross-check totals)
2. **Select a flight** from the dropdown (routes are pre-configured in Manage Routes)
3. **Upload the passenger list PDF** for that flight
4. Click **Add Flight to Manifest** — the O-D demand rows appear below
5. Repeat steps 2-4 for every flight operating today
6. Click **→ Run Optimizer** when all flights are added

---

### Tab 2 — Run Optimizer *(daily)*
- Uses the manifest built in Tab 1, or upload your own `manifest.csv`
- Click **Generate Schedule** to run OR-Tools (or heuristic fallback)
- Any unserved pax get a detailed report explaining exactly why — hard
  constraint vs. no aircraft was free — so ops knows what to fix
- Click **Save this schedule** to keep a permanent record (who generated it
  and when), retrievable later from the **Saved Schedules** tab
- Download `schedule.csv` and `bookings.csv`

---

### Tab — Saved Schedules
Browse every schedule anyone has saved: who generated it, when, and the
full detail behind it — including the unserved-pax report.

---

### Tab — Manage Routes *(one-time setup per flight)*
- Select an existing flight number or type a new one
- Enter the stop sequence and published leg times (dep/arr HH:MM)
- Click **Save Route** — the flight is now available in Tab 1

---

### Tab — Admin *(admin accounts only)*
- Create new user accounts (admin or ops role)
- View everyone who has an account and when it was created

---

### Field reference

| Field | Meaning |
|---|---|
| `open_min` | Minutes from midnight when the strip opens (390 = 06:30) |
| `close_min` | Minutes from midnight when the strip closes (1110 = 18:30) |
| `daylight_only` | 1 = bush strip, no night ops; 0 = lit runway, 24 hr |
| `connect_by` | Arrival deadline(s) in minutes from midnight, semicolon-separated |
| `earliest_dep` | Earliest departure from origin stop (set from leg timings) |
| `Positioning` | Empty ferry leg — counts as wasted block time |

**Bush-strip hours:** Sunrise ~06:30 (390 min), sunset ~18:30 (1110 min).
Paved airports (ARK, JRO, DAR, ZNZ, MWZ) operate 24 hr (0 → 1440).

---
### Tips
- **OR-Tools** gives better plans; the heuristic is a fast fallback.
- If pax show as *UNSERVED*, check seat/payload capacity, daylight
  windows, or duty limits in `fleet.csv`.
- New airstrip codes must be added to `data/airstrips.csv`.
        """
    )

# --------------------------------------------------------------------------- #
# Load static reference data
# --------------------------------------------------------------------------- #

data_dir = os.path.join(here, "data")
if not os.path.exists(f"{data_dir}/airstrips.csv") or not os.path.exists(f"{data_dir}/fleet.csv"):
    st.error("airstrips.csv or fleet.csv not found in `data/`. Make sure both files are committed.")
    st.stop()

strips     = pc.load_airstrips(f"{data_dir}/airstrips.csv")
known_codes = set(strips.keys())

# --------------------------------------------------------------------------- #
# Session-state initialisation
# --------------------------------------------------------------------------- #

if "manifest_rows" not in st.session_state:
    st.session_state.manifest_rows = []      # accumulated O-D demand dicts
if "manifest_date" not in st.session_state:
    st.session_state.manifest_date = ""
if "booking_counts" not in st.session_state:
    st.session_state.booking_counts = {}     # {flight_num: pax} from booking CSV
if "built_manifest" not in st.session_state:
    st.session_state.built_manifest = None   # DataFrame sent to optimizer tab
if "routes_action_radio" not in st.session_state:
    st.session_state["routes_action_radio"] = "Edit existing flight"
if "sched_result" not in st.session_state:
    st.session_state["sched_result"] = None
if "day_aircraft" not in st.session_state:
    st.session_state["day_aircraft"] = {}   # {flight_num: [{type, reg}, ...]}

# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #

_tab_labels = ["📋 Build Manifest", "✈ Run Optimizer", "📂 Saved Schedules", "🗺 Manage Routes"]
if _is_admin:
    _tab_labels.append("👤 Admin")
_tabs = st.tabs(_tab_labels)
tab_build, tab_optimize, tab_saved, tab_routes = _tabs[:4]
tab_admin = _tabs[4] if _is_admin else None


# =========================================================================== #
# TAB 1 — Build Manifest
# =========================================================================== #

with tab_build:

    routes_master = ingest.load_flight_routes()
    configured_flights = sorted(routes_master.keys())

    # ------------------------------------------------------------------ #
    # Booking Analysis (once per day)
    # ------------------------------------------------------------------ #
    with st.expander("Upload Booking Analysis CSV (optional — used for pax cross-check)", expanded=False):
        booking_file = st.file_uploader(
            "Booking_Analysis.csv",
            type=["csv"],
            key="booking_csv",
            help="One row per date, columns = flight numbers (UI001…)",
        )
        if booking_file:
            try:
                bdate, bcounts = ingest.parse_booking_analysis(booking_file)
                st.session_state.booking_counts = bcounts
                if not st.session_state.manifest_date and bdate:
                    st.session_state.manifest_date = bdate
                st.success(
                    f"Booking Analysis loaded: {bdate} — "
                    f"{len(bcounts)} flights with pax booked"
                )
            except Exception as e:
                st.error(f"Booking Analysis error: {e}")

    st.divider()

    # ------------------------------------------------------------------ #
    # Add one flight at a time
    # ------------------------------------------------------------------ #
    st.subheader("Add a flight to today's manifest")

    if not configured_flights:
        st.warning(
            "No flight routes are configured yet. "
            "Go to the **Manage Routes** tab to add your flight numbers and stop sequences."
        )
    else:
        col_sel, col_prev = st.columns([1, 2])

        with col_sel:
            selected_flight = st.selectbox(
                "Select flight number",
                configured_flights,
                help="Routes are configured in the Manage Routes tab",
            )
            _ac_type = ingest.route_aircraft_type(routes_master, selected_flight)
            if _ac_type and _ac_type in ingest.AIRCRAFT_TYPE_OPTS:
                _info = ingest.AIRCRAFT_TYPE_OPTS[_ac_type]
                st.caption(f"Aircraft: **{_info['name']}**  ·  {_info['seats']} seats")

        with col_prev:
            stops = ingest.route_stop_sequence(routes_master, selected_flight)
            if stops:
                legs = routes_master[selected_flight]
                st.markdown(f"**{selected_flight} route** ({len(legs)} legs)")
                stop_line = " → ".join(stops)
                st.code(stop_line, language=None)
                if legs and legs[0].get("dep"):
                    first_dep = legs[0]["dep"]
                    last_arr  = legs[-1].get("arr", "?")
                    st.caption(f"Scheduled: {first_dep} → {last_arr}")
            else:
                st.caption("No legs configured for this flight yet — add them in Manage Routes.")

        # Aircraft assignment for this flight today
        _save_day = st.session_state.manifest_date or "unknown"
        _saved_ac = ingest.load_flight_aircraft(_save_day, selected_flight)
        _default_type = ingest.route_aircraft_type(routes_master, selected_flight) or "C208B"
        # Build list of friendly labels "Caravan (C208B)" for the type selectbox
        _type_labels = [
            f"{v['short_name']} ({k})"
            for k, v in ingest.AIRCRAFT_TYPE_OPTS.items()
        ]
        _label_to_key = {
            f"{v['short_name']} ({k})": k
            for k, v in ingest.AIRCRAFT_TYPE_OPTS.items()
        }
        _key_to_label = {k: v for v, k in _label_to_key.items()}

        _other_flights = [f for f in configured_flights if f != selected_flight]

        with st.expander(
            f"Aircraft available today for {selected_flight} (required for capacity planning)",
            expanded=not _saved_ac,
        ):
            _ac_count = st.number_input(
                "Number of aircraft assigned to this flight today",
                min_value=1, max_value=10,
                value=max(len(_saved_ac), 1),
                key=f"ac_count_{selected_flight}",
                help="How many aircraft will operate this flight today?",
            )

            _ac_rows = []
            for _i in range(_ac_count):
                if _i < len(_saved_ac):
                    _sv = _saved_ac[_i]
                    _ac_rows.append({
                        "type":            _key_to_label.get(_sv.get("type", _default_type),
                                                             _key_to_label.get(_default_type, _type_labels[0])),
                        "reg":             _sv.get("reg", ""),
                        "return_to_base":  bool(_sv.get("return_to_base", True)),
                        "next_route":      _sv.get("next_route", ""),
                    })
                else:
                    _ac_rows.append({
                        "type":           _key_to_label.get(_default_type, _type_labels[0]),
                        "reg":            "",
                        "return_to_base": True,
                        "next_route":     "",
                    })

            _edited_ac = st.data_editor(
                pd.DataFrame(_ac_rows),
                column_config={
                    "type": st.column_config.SelectboxColumn(
                        "Aircraft Type",
                        options=_type_labels,
                        required=True,
                        help="Caravan = C208B (12 pax), Pilatus = PC-12 (8 pax), Dash-8 = up to 50 pax",
                    ),
                    "reg": st.column_config.TextColumn(
                        "Registration (optional)",
                        help="e.g. 5H-AUB — leave blank to auto-assign",
                    ),
                    "return_to_base": st.column_config.CheckboxColumn(
                        "Return to Base",
                        help="If unchecked, aircraft stays at its last stop instead of flying back to base",
                        default=True,
                    ),
                    "next_route": st.column_config.SelectboxColumn(
                        "Continue to Route",
                        options=[""] + _other_flights,
                        help="If set, this aircraft continues to the selected route when finished here (automatically disables 'Return to Base')",
                    ),
                },
                num_rows="fixed",
                width='stretch',
                hide_index=True,
                key=f"ac_editor_{selected_flight}_{_ac_count}",
            )

            if st.button(f"💾 Save aircraft for {selected_flight}", key=f"btn_save_ac_{selected_flight}"):
                _ac_list = []
                for _, _row in _edited_ac.iterrows():
                    _label = str(_row.get("type") or _type_labels[0]).strip()
                    _type_key = _label_to_key.get(_label, _default_type)
                    _next = str(_row.get("next_route") or "").strip()
                    _rtb  = bool(_row.get("return_to_base", True)) if not _next else False
                    _ac_list.append({
                        "type":            _type_key,
                        "reg":             str(_row.get("reg") or "").strip(),
                        "return_to_base":  _rtb,
                        "next_route":      _next,
                    })
                ingest.save_flight_aircraft(_save_day, selected_flight, _ac_list)
                st.session_state["day_aircraft"][selected_flight] = _ac_list
                _seat_str = " / ".join(
                    f"{ingest.AIRCRAFT_TYPE_OPTS.get(a['type'], {}).get('seats', '?')} seats"
                    for a in _ac_list
                )
                st.success(
                    f"Saved {len(_ac_list)} aircraft for {selected_flight} on {_save_day}. "
                    f"Capacities: {_seat_str}"
                )
            elif _saved_ac:
                _summary = []
                for a in _saved_ac:
                    _t = ingest.AIRCRAFT_TYPE_OPTS.get(a.get("type",""), {}).get("short_name", a.get("type","?"))
                    _r = a.get("reg","") or ""
                    _rtb = "" if a.get("return_to_base", True) else " [stays]"
                    _nr  = f" → {a['next_route']}" if a.get("next_route") else ""
                    _summary.append(f"{_t}{(' ' + _r) if _r else ''}{_rtb}{_nr}")
                st.caption(f"Previously saved: {', '.join(_summary)}")

        # PDF upload for this flight
        pax_file = st.file_uploader(
            f"Passenger list PDF for {selected_flight}",
            type=["pdf"],
            key=f"pdf_{selected_flight}",
            help="Standard Auric Air passenger list — section headers like 'GTZ - ARK'",
        )

        if pax_file and st.button(
            f"Add {selected_flight} to Manifest", type="primary", key="btn_add_flight"
        ):
            pdf_bytes = pax_file.read()

            # Show raw text in expander for debugging
            raw_lines = ingest.extract_pdf_text(io.BytesIO(pdf_bytes))
            with st.expander("Raw text extracted from PDF (expand to debug format issues)", expanded=False):
                st.code("\n".join(raw_lines), language=None)

            # Parse PDF
            flight_num, pdf_date, od_list = ingest.parse_passenger_pdf(io.BytesIO(pdf_bytes))

            if not od_list:
                st.error(
                    "Could not find any O-D demand groups in the PDF. "
                    "Open the raw text expander above and check that section headers "
                    "appear as standalone lines like `GTZ - ARK`."
                )
            else:
                # Resolve date
                day = pdf_date or st.session_state.manifest_date or "unknown"
                if not st.session_state.manifest_date and day != "unknown":
                    st.session_state.manifest_date = day

                # Use master route timings for this flight
                timings = ingest.routes_to_timings(routes_master, [selected_flight])

                # Build manifest rows
                df_new = ingest.build_manifest(od_list, timings, day, selected_flight)

                # Pax cross-check against booking analysis
                bc = st.session_state.booking_counts
                fn  = flight_num or selected_flight
                manifest_pax = df_new["pax"].sum()
                if fn in bc:
                    if bc[fn] != manifest_pax:
                        st.warning(
                            f"Pax mismatch for {fn}: Booking Analysis = {bc[fn]}, "
                            f"PDF total = {manifest_pax}. Check the passenger list."
                        )
                    else:
                        st.info(f"Pax cross-check OK: {manifest_pax} pax matches Booking Analysis.")

                # Warn on unknown airstrip codes
                all_codes = set(df_new["origin"]) | set(df_new["dest"])
                unknown = all_codes - known_codes
                if unknown:
                    st.warning(
                        f"Unknown airstrip codes (add to airstrips.csv before running optimiser): "
                        f"**{', '.join(sorted(unknown))}**"
                    )

                # Tag each row with its source flight so it can be removed later
                _new_rows = df_new.to_dict("records")
                for _r in _new_rows:
                    _r["_src_flight"] = selected_flight
                # Remove any previously added rows for this flight (replace, don't double-add)
                st.session_state.manifest_rows = [
                    r for r in st.session_state.manifest_rows
                    if r.get("_src_flight") != selected_flight
                ]
                st.session_state.manifest_rows.extend(_new_rows)

                st.success(
                    f"Added {selected_flight}: {len(od_list)} O-D groups, "
                    f"{manifest_pax} pax — manifest now has "
                    f"{len(st.session_state.manifest_rows)} total rows."
                )

                # Save PDF to disk so it stays accessible for the day
                _plist_dir = os.path.join(here, "data", "passenger_lists")
                os.makedirs(_plist_dir, exist_ok=True)
                _safe_date = (day or "unknown").replace("/", "-").replace("\\", "-")
                _fn_key    = (flight_num or selected_flight).replace("/", "-")
                _pdf_path  = os.path.join(_plist_dir, f"{_safe_date}_{_fn_key}.pdf")
                with open(_pdf_path, "wb") as _pdf_fh:
                    _pdf_fh.write(pdf_bytes)
                st.caption(f"Passenger list saved: {os.path.basename(_pdf_path)}")

    # ------------------------------------------------------------------ #
    # Today's accumulated manifest
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("Today's manifest")

    rows = st.session_state.manifest_rows
    if not rows:
        st.info("No flights added yet. Select a flight above and upload its PDF.")
    else:
        df_manifest = pd.DataFrame(rows)
        # Show table without internal tracking column
        _display_cols = [c for c in df_manifest.columns if c != "_src_flight"]
        st.dataframe(df_manifest[_display_cols], width='stretch', hide_index=True)
        st.caption(
            f"{len(rows)} demand rows · "
            f"{df_manifest['pax'].sum()} total pax · "
            f"date: {st.session_state.manifest_date or 'unknown'}"
        )

        # Per-flight remove buttons
        _flights_in_manifest = sorted({r.get("_src_flight", "?") for r in rows})
        if len(_flights_in_manifest) > 1 or _flights_in_manifest != [selected_flight]:
            st.markdown("**Remove a flight from manifest:**")
            _rm_cols = st.columns(min(len(_flights_in_manifest), 5))
            for _ci, _fn in enumerate(_flights_in_manifest):
                _fn_pax = sum(r["pax"] for r in rows if r.get("_src_flight") == _fn)
                if _rm_cols[_ci % 5].button(
                    f"✖ {_fn} ({_fn_pax} pax)", key=f"rm_{_fn}"
                ):
                    st.session_state.manifest_rows = [
                        r for r in st.session_state.manifest_rows
                        if r.get("_src_flight") != _fn
                    ]
                    st.rerun()

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])

        with btn_col1:
            if st.button("🗑 Clear all flights", key="btn_clear"):
                st.session_state.manifest_rows = []
                st.session_state.manifest_date = ""
                st.rerun()

        with btn_col2:
            st.download_button(
                "⬇ Download manifest.csv",
                df_manifest[_display_cols].to_csv(index=False).encode(),
                file_name="manifest.csv",
                mime="text/csv",
            )

        with btn_col3:
            if st.button("✈ Send to Optimizer →", type="primary", key="btn_send"):
                st.session_state.built_manifest = df_manifest[_display_cols].copy()
                st.session_state["sched_result"] = None  # clear any previous schedule
                st.success("Manifest sent. Switch to the **Run Optimizer** tab above.")

    # ------------------------------------------------------------------ #
    # Saved Passenger Lists
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("Saved Passenger Lists")
    _plist_dir = os.path.join(here, "data", "passenger_lists")
    _saved = sorted(
        [f for f in os.listdir(_plist_dir) if f.endswith(".pdf")]
    ) if os.path.exists(_plist_dir) else []

    if not _saved:
        st.info("No passenger lists saved yet — upload a PDF above to create a record.")
    else:
        st.caption(
            "Upload a new PDF for the same flight above to replace its entry. "
            "Delete individual files below or clear all at once."
        )

        _pdf_top1, _pdf_top2 = st.columns([1, 4])
        if _pdf_top1.button("🗑 Delete All PDFs", key="btn_del_all_pdfs"):
            for _f in _saved:
                try:
                    os.remove(os.path.join(_plist_dir, _f))
                except OSError:
                    pass
            st.rerun()

        _hdr = st.columns([3, 1, 1, 1])
        _hdr[0].markdown("**File**")
        _hdr[1].markdown("**Size**")
        _hdr[2].markdown("**Download**")
        _hdr[3].markdown("**Delete**")
        for _fname in _saved:
            _fpath = os.path.join(_plist_dir, _fname)
            _fsize = os.path.getsize(_fpath)
            _col_name, _col_size, _col_dl, _col_del = st.columns([3, 1, 1, 1])
            _col_name.write(f"📄 {_fname}")
            _col_size.write(f"{_fsize // 1024} KB" if _fsize >= 1024 else f"{_fsize} B")
            with open(_fpath, "rb") as _fh:
                _col_dl.download_button(
                    "⬇",
                    _fh.read(),
                    file_name=_fname,
                    mime="application/pdf",
                    key=f"dl_plist_{_fname}",
                )
            if _col_del.button("❌", key=f"del_pdf_{_fname}", help=f"Delete {_fname}"):
                try:
                    os.remove(_fpath)
                except OSError:
                    pass
                st.rerun()


# =========================================================================== #
# TAB 2 — Run Optimizer
# =========================================================================== #

with tab_optimize:
    st.subheader("Generate schedule from manifest")

    prefilled = st.session_state.get("built_manifest")
    if prefilled is not None:
        st.info("Using the manifest built in the Build Manifest tab.")

    uploaded = st.file_uploader(
        "Or upload manifest.csv directly",
        type=["csv"],
        key="manifest_upload",
        help="Columns: id, date, origin, dest, pax, connect_by (opt), earliest_dep (opt)",
    )

    df_manifest = None
    if uploaded:
        df_manifest = pd.read_csv(uploaded)
    elif prefilled is not None:
        df_manifest = prefilled
    else:
        st.info("Build a manifest in the Build Manifest tab, or upload one here.")

    if df_manifest is not None:

        with st.expander("Manifest preview", expanded=True):
            st.dataframe(df_manifest, width='stretch', hide_index=True)

        _n_demands = len(df_manifest)
        _auto_time = max(15, min(90, 10 + 2 * _n_demands))
        _effort_opts = {
            f"Auto — recommended (~{_auto_time}s)": _auto_time,
            "Fast (~15s)": 15,
            "Thorough (~90s)": 90,
        }
        _effort = st.selectbox(
            "Optimization effort",
            list(_effort_opts.keys()),
            index=0,
            help="More search time lets OR-Tools explore a larger solution space and "
                 "find a shorter, more efficient plan. Auto scales with the size of "
                 "today's manifest — use Thorough for a big multi-flight day.",
        )
        _time_limit = _effort_opts[_effort]

        if st.button("Generate Schedule", type="primary", key="btn_optimize"):

            with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
                df_manifest.to_csv(f, index=False)
                manifest_path = f.name

            _load_ok = True
            try:
                # Fleet: prefer per-day aircraft assignments over static fleet.csv
                _day_str  = st.session_state.get("manifest_date") or "unknown"
                _day_ac   = ingest.load_all_day_aircraft(_day_str)
                _rt_master = ingest.load_flight_routes()

                if _day_ac:
                    _specs = ingest.build_fleet_specs(_day_ac, _rt_master)
                    if _specs:
                        fleet = [
                            pc.Aircraft(
                                reg=s["reg"], type=s["type"], base=s["base"],
                                seats=s["seats"], payload_kg=s["payload_kg"],
                                cruise_kts=s["cruise_kts"],
                                available_from=s["available_from"],
                                available_until=s["available_until"],
                                max_duty_min=s["max_duty_min"],
                                turnaround_min=s["turnaround_min"],
                                return_to_base=s.get("return_to_base", True),
                                flight_tag=s.get("flight_tag"),
                                next_route=s.get("next_route"),
                            )
                            for s in _specs
                        ]
                        _ac_labels = []
                        for s in _specs:
                            _sn = ingest.AIRCRAFT_TYPE_OPTS.get(s["type"], {}).get("short_name", s["type"])
                            _nr = f"→{s['next_route']}" if s.get("next_route") else ""
                            _rtb = "" if s.get("return_to_base", True) else "[stays]"
                            _ac_labels.append(f"{_sn} {s['reg']} {_rtb}{_nr}".strip())
                        st.info(
                            f"Using {len(fleet)} aircraft from today's assignments: "
                            f"{', '.join(_ac_labels)}"
                        )
                    else:
                        fleet = pc.load_fleet(f"{data_dir}/fleet.csv")
                else:
                    fleet = pc.load_fleet(f"{data_dir}/fleet.csv")

                manifest = pc.load_manifest(manifest_path)
            except Exception as e:
                st.error(f"Could not load data: {e}")
                _load_ok = False
            finally:
                try:
                    os.unlink(manifest_path)
                except OSError:
                    pass

            if _load_ok:
                # Split demands that exceed the largest aircraft ELIGIBLE to serve
                # them (per flight-tag ownership), not the day-wide fleet max.
                manifest = _split_large_demands(manifest, fleet)
                demands = {d.id: d for d in manifest}
                w, lm   = pc.Weights(), pc.LoadModel()

                with st.spinner(f"Optimising routes… (up to {_time_limit}s)"):
                    routes, dropped, engine, fallback_reason = run_solver(
                        strips, fleet, manifest, w, lm, time_limit_s=_time_limit
                    )

                total_pax   = sum(d.pax for d in demands.values())
                served_pax  = total_pax - sum(d.pax for d in dropped)
                ac_used     = sum(1 for _, (_, _, res) in routes.items() if res.used)
                total_block = sum(res.total_block_min for _, _, res in routes.values())
                empty_block = sum(res.empty_block_min for _, _, res in routes.values())
                _load_factors = [
                    res.seats_used_peak / ac.seats
                    for _, (ac, _, res) in routes.items()
                    if res.used and res.feasible and ac.seats
                ]
                avg_load_factor = (sum(_load_factors) / len(_load_factors) * 100) if _load_factors else 0.0

                # WHY each unserved booking couldn't be flown, in terms ops can
                # act on: a hard constraint (loosen the window/limit to fix) vs.
                # a fleet-size limit (add or free up an aircraft to fix).
                _drop_diagnoses = pc.diagnose_dropped_demands(dropped, fleet, strips, lm)
                dropped_diagnoses = [
                    {
                        "demand_id": diag.demand_id, "origin": diag.origin, "dest": diag.dest,
                        "pax": diag.pax, "flight_tag": diag.flight_tag,
                        "category": diag.category, "detail": diag.detail,
                    }
                    for diag in _drop_diagnoses
                ]
                _generated_by = _me["username"]
                _generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

                sched_rows = []
                for reg, (ac, stops_seq, res) in routes.items():
                    if not res.used or not res.feasible:
                        continue

                    # Aggregate per-location pax events for this aircraft so
                    # each leg's destination row can show what happens there.
                    _loc_ev: dict[str, dict[str, int]] = {}
                    for _s in stops_seq:
                        if _s.kind not in ("pickup", "delivery"):
                            continue
                        _d = demands[_s.demand_id]
                        _ev = _loc_ev.setdefault(_s.code, {"dropped": 0, "picked": 0})
                        if _s.kind == "pickup":
                            _ev["picked"] += _d.pax
                        else:
                            _ev["dropped"] += _d.pax

                    leg_no = 0
                    for lg in res.legs:
                        if lg.block_min == 0:
                            continue
                        leg_no += 1
                        _to_ev = _loc_ev.get(lg.to, {"dropped": 0, "picked": 0})

                        if lg.empty:
                            if _to_ev["picked"] > 0:
                                _pos = f"Pickup at {lg.to}"
                            elif lg.to == ac.base:
                                _pos = "Return to base"
                            else:
                                _pos = "Positioning"
                        else:
                            _pos = ""

                        sched_rows.append({
                            "Aircraft":    reg,
                            "Leg":         leg_no,
                            "Depart":      pc.hhmm(lg.depart_min),
                            "Arrive":      pc.hhmm(lg.arrive_min),
                            "From":        f"{lg.frm}  {strips[lg.frm].name if lg.frm in strips else ''}",
                            "To":          f"{lg.to}  {strips[lg.to].name if lg.to in strips else ''}",
                            "Pax on Leg":  lg.pax_onboard,
                            "Dropped":     _to_ev["dropped"],
                            "Picked Up":   _to_ev["picked"],
                            "Block (min)": int(lg.block_min),
                            "Positioning": _pos,
                        })

                booking_rows = []
                for reg, (ac, stops_seq, res) in routes.items():
                    if not res.used or not res.feasible:
                        continue
                    for s in stops_seq:
                        if s.kind != "pickup":
                            continue
                        d  = demands[s.demand_id]
                        cb = " / ".join(pc.hhmm(t) for t in d.connect_by) if d.connect_by else ""
                        booking_rows.append({
                            "Booking ID": d.id,
                            "Pax":        d.pax,
                            "From":       f"{d.origin}  {strips[d.origin].name if d.origin in strips else ''}",
                            "To":         f"{d.dest}  {strips[d.dest].name if d.dest in strips else ''}",
                            "Aircraft":   reg,
                            "Connect By": cb,
                            "Passengers": " | ".join(d.passengers) if d.passengers else "",
                            "Status":     "Served",
                        })
                for d in dropped:
                    cb = " / ".join(pc.hhmm(t) for t in d.connect_by) if d.connect_by else ""
                    booking_rows.append({
                        "Booking ID": d.id,
                        "Pax":        d.pax,
                        "From":       f"{d.origin}  {strips[d.origin].name if d.origin in strips else ''}",
                        "To":         f"{d.dest}  {strips[d.dest].name if d.dest in strips else ''}",
                        "Aircraft":   "UNSERVED",
                        "Connect By": cb,
                        "Passengers": " | ".join(d.passengers) if d.passengers else "",
                        "Status":     "UNSERVED — handle manually",
                    })

                df_sched    = pd.DataFrame(sched_rows)
                df_bookings = pd.DataFrame(booking_rows).sort_values(["Aircraft", "Booking ID"])

                # Store in session state so results survive download-button reruns
                st.session_state["sched_result"] = {
                    "engine":            engine,
                    "fallback_reason":   fallback_reason,
                    "served_pax":        served_pax,
                    "total_pax":         total_pax,
                    "ac_used":           ac_used,
                    "total_block":       int(total_block),
                    "empty_block":       int(empty_block),
                    "avg_load_factor":   avg_load_factor,
                    "dropped_pax":       sum(d.pax for d in dropped),
                    "dropped_items": [
                        f"{d.id}: {d.origin} → {d.dest}, {d.pax} pax"
                        for d in dropped
                    ],
                    "dropped_diagnoses": dropped_diagnoses,
                    "generated_by":      _generated_by,
                    "generated_at":      _generated_at,
                    "manifest_date":     st.session_state.get("manifest_date") or "unknown",
                    "saved_as":          None,  # set once the user clicks Save
                    "df_sched":    df_sched,
                    "df_bookings": df_bookings,
                }

        # ---- Persistent results display (survives download-button reruns) ----
        sr = st.session_state.get("sched_result")
        if sr is not None:
            if sr.get("fallback_reason"):
                st.warning(
                    f"OR-Tools could not be used ({sr['fallback_reason']}) — used the "
                    "heuristic fallback instead. Plans from OR-Tools are usually shorter "
                    "and more efficient; consider re-running once the issue is resolved."
                )
            st.success(f"Schedule generated using the **{sr['engine']}** engine.")
            st.caption(f"Generated by **{sr['generated_by']}** on **{sr['generated_at']}**")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Pax Served",           f"{sr['served_pax']} / {sr['total_pax']}")
            c2.metric("Aircraft Used",         sr["ac_used"])
            c3.metric("Total Block Time",      f"{sr['total_block']} min")
            c4.metric(
                "Positioning (empty) Time",
                f"{sr['empty_block']} min",
                delta=f"-{sr['empty_block']} min wasted" if sr["empty_block"] else "0 min",
                delta_color="inverse" if sr["empty_block"] else "off",
            )
            c5.metric("Avg Load Factor", f"{sr.get('avg_load_factor', 0):.0f}%")

            _diagnoses = sr.get("dropped_diagnoses") or []
            if _diagnoses:
                _infeasible  = [x for x in _diagnoses if x["category"] == "infeasible"]
                _no_aircraft = [x for x in _diagnoses if x["category"] in ("no_free_aircraft", "no_aircraft")]
                st.warning(
                    f"{sr['dropped_pax']} pax on {len(_diagnoses)} booking(s) could not be "
                    f"served — {sum(x['pax'] for x in _infeasible)} pax cannot be flown under "
                    f"current constraints, {sum(x['pax'] for x in _no_aircraft)} pax were flyable "
                    f"but no aircraft was free. See the detailed report below."
                )
                with st.expander(
                    f"📋 Unserved-passenger report ({len(_diagnoses)} bookings) — for ops review",
                    expanded=True,
                ):
                    st.markdown(
                        "**Hard constraint** — not flyable by any eligible aircraft as currently "
                        "configured (schedule window, capacity, daylight, or duty limit). Fix by "
                        "loosening the constraint or the published schedule window."
                    )
                    _df_infeasible = pd.DataFrame([
                        {"Booking ID": x["demand_id"], "From→To": f"{x['origin']}→{x['dest']}",
                         "Pax": x["pax"], "Flight": x["flight_tag"] or "", "Reason": x["detail"]}
                        for x in _infeasible
                    ])
                    if not _df_infeasible.empty:
                        st.dataframe(_df_infeasible, width='stretch', hide_index=True)
                    else:
                        st.caption("None.")

                    st.markdown(
                        "**Fleet-size limit** — physically flyable on schedule, but every "
                        "eligible aircraft was already committed to other passengers. Fix by "
                        "adding another aircraft to the flight, or freeing one up."
                    )
                    _df_no_ac = pd.DataFrame([
                        {"Booking ID": x["demand_id"], "From→To": f"{x['origin']}→{x['dest']}",
                         "Pax": x["pax"], "Flight": x["flight_tag"] or "", "Detail": x["detail"]}
                        for x in _no_aircraft
                    ])
                    if not _df_no_ac.empty:
                        st.dataframe(_df_no_ac, width='stretch', hide_index=True)
                    else:
                        st.caption("None.")

            t1, t2 = st.tabs(["Flight Schedule", "Bookings"])
            with t1:
                st.dataframe(sr["df_sched"], width='stretch', hide_index=True)
            with t2:
                st.dataframe(sr["df_bookings"], width='stretch', hide_index=True)

            st.divider()
            dl1, dl2, dl3, dl4 = st.columns([2, 2, 2, 1])
            dl1.download_button(
                "⬇ Download schedule.csv",
                sr["df_sched"].to_csv(index=False).encode(),
                file_name="schedule.csv",
                mime="text/csv",
                key="dl_sched",
            )
            dl2.download_button(
                "⬇ Download bookings.csv",
                sr["df_bookings"].to_csv(index=False).encode(),
                file_name="bookings.csv",
                mime="text/csv",
                key="dl_bookings",
            )
            with dl3:
                if sr.get("saved_as"):
                    st.success(f"Saved as {sr['saved_as']}")
                elif st.button("💾 Save this schedule", key="btn_save_sched"):
                    _fname = ingest.save_schedule(
                        manifest_date=sr["manifest_date"],
                        generated_by=sr["generated_by"],
                        generated_at=sr["generated_at"],
                        engine=sr["engine"],
                        summary={
                            "served_pax": sr["served_pax"], "total_pax": sr["total_pax"],
                            "ac_used": sr["ac_used"], "total_block": sr["total_block"],
                            "empty_block": sr["empty_block"], "avg_load_factor": sr["avg_load_factor"],
                            "dropped_pax": sr["dropped_pax"],
                        },
                        schedule_rows=sr["df_sched"].to_dict("records"),
                        booking_rows=sr["df_bookings"].to_dict("records"),
                        dropped_diagnoses=_diagnoses,
                    )
                    sr["saved_as"] = _fname
                    st.rerun()
            with dl4:
                if st.button("🗑 Clear", key="btn_clear_sched",
                             help="Remove results to start a fresh run"):
                    st.session_state["sched_result"] = None
                    st.rerun()


# =========================================================================== #
# TAB 3 — Manage Routes
# =========================================================================== #

with tab_routes:
    st.subheader("Flight Route Master")
    st.caption(
        "Configure the fixed stop sequence and published leg times for each flight number. "
        "This is a one-time setup — routes are reused every day until the schedule changes."
    )

    # Apply post-save redirect BEFORE any widgets are created
    if st.session_state.get("_post_save_flight"):
        _psf = st.session_state.pop("_post_save_flight")
        st.session_state["routes_action_radio"] = "Edit existing flight"
        st.session_state["new_flight_input_box"] = ""
        st.session_state["sel_edit_flight"] = _psf

    routes_master_edit = ingest.load_flight_routes()
    configured = sorted(routes_master_edit.keys())

    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.markdown("**Select or create a flight**")

        action = st.radio(
            "Action",
            ["Edit existing flight", "Add new flight"],
            horizontal=True,
            label_visibility="collapsed",
            key="routes_action_radio",
        )

        if action == "Edit existing flight":
            if not configured:
                st.info("No flights configured yet. Choose 'Add new flight'.")
                edit_flight = None
            else:
                edit_flight = st.selectbox("Flight number", configured, key="sel_edit_flight")
        else:
            raw = st.text_input(
                "New flight number (3–6 uppercase letters/digits)",
                placeholder="e.g. UI001",
                key="new_flight_input_box",
            ).strip().upper()
            edit_flight = raw if raw else None
            if raw and raw in configured:
                st.warning(f"{raw} already exists — switch to 'Edit existing flight' to modify it.")

        if edit_flight:
            st.divider()
            st.markdown(f"**Route summary for {edit_flight}**")
            stops = ingest.route_stop_sequence(routes_master_edit, edit_flight)
            if stops:
                st.code(" → ".join(stops), language=None)
                legs_ex = routes_master_edit.get(edit_flight, [])
                if legs_ex and legs_ex[0].get("dep"):
                    st.caption(
                        f"{legs_ex[0]['dep']} dep  ·  "
                        f"{legs_ex[-1].get('arr', '?')} arr  ·  "
                        f"{len(legs_ex)} legs"
                    )
            else:
                st.caption("No legs yet — add them in the table.")

            # Delete flight button
            if edit_flight in routes_master_edit:
                if st.button(f"🗑 Delete {edit_flight}", key="btn_del_flight"):
                    del routes_master_edit[edit_flight]
                    ingest.save_flight_routes(routes_master_edit)
                    st.success(f"{edit_flight} deleted.")
                    st.rerun()

    with col_right:
        if edit_flight:
            st.markdown(f"**Legs for {edit_flight}** — edit cells, add or delete rows, then save")

            # Aircraft type selector
            _type_keys = list(ingest.AIRCRAFT_TYPE_OPTS.keys())
            _curr_type = ingest.route_aircraft_type(routes_master_edit, edit_flight)
            _type_idx  = _type_keys.index(_curr_type) if _curr_type in _type_keys else 0
            sel_ac_type = st.selectbox(
                "Scheduled aircraft type",
                _type_keys,
                index=_type_idx,
                format_func=lambda k: (
                    f"{ingest.AIRCRAFT_TYPE_OPTS[k]['name']}  ·  "
                    f"{ingest.AIRCRAFT_TYPE_OPTS[k]['seats']} seats"
                ),
                key=f"actype_{edit_flight}",
                help="Aircraft type that operates this flight — used for capacity display",
            )

            existing_legs = routes_master_edit.get(edit_flight, [])
            df_legs = pd.DataFrame(
                [{"from": l["from"], "to": l["to"], "dep": l["dep"], "arr": l["arr"]}
                 for l in existing_legs]
                if existing_legs
                else [{"from": "", "to": "", "dep": "", "arr": ""}]
            )

            edited = st.data_editor(
                df_legs,
                column_config={
                    "from": st.column_config.TextColumn("From", width="small",
                        help="3-letter airstrip code, e.g. SEU"),
                    "to":   st.column_config.TextColumn("To",   width="small",
                        help="3-letter airstrip code, e.g. ZNZ"),
                    "dep":  st.column_config.TextColumn("Depart (HH:MM)", width="small",
                        help="Published departure time, e.g. 10:00"),
                    "arr":  st.column_config.TextColumn("Arrive (HH:MM)", width="small",
                        help="Published arrival time, e.g. 11:20"),
                },
                num_rows="dynamic",
                width='stretch',
                hide_index=True,
                key=f"editor_{edit_flight}",
            )

            if st.button("💾 Save Route", type="primary", key="btn_save_route"):
                new_legs = []
                for _, row in edited.iterrows():
                    frm = str(row.get("from") or "").strip().upper()
                    to  = str(row.get("to")   or "").strip().upper()
                    if frm and to:
                        new_legs.append({
                            "from": frm,
                            "to":   to,
                            "dep":  str(row.get("dep") or "").strip(),
                            "arr":  str(row.get("arr") or "").strip(),
                            "aircraft_type": sel_ac_type,
                        })

                if not new_legs:
                    st.error("No valid legs to save — each row needs at least a From and To code.")
                else:
                    # Validate airstrip codes
                    all_codes_in_route = {l["from"] for l in new_legs} | {l["to"] for l in new_legs}
                    unknown = all_codes_in_route - known_codes
                    if unknown:
                        st.warning(
                            f"These codes are not in airstrips.csv — add them before running "
                            f"the optimiser: **{', '.join(sorted(unknown))}**"
                        )

                    routes_master_edit[edit_flight] = new_legs
                    ingest.save_flight_routes(routes_master_edit)
                    st.success(
                        f"Route for **{edit_flight}** saved — "
                        f"{len(new_legs)} legs · "
                        f"{ingest.AIRCRAFT_TYPE_OPTS.get(sel_ac_type, {}).get('name', sel_ac_type)} · "
                        f"{' → '.join(ingest.route_stop_sequence(routes_master_edit, edit_flight))}"
                    )
                    # Reset form: switch back to Edit mode, pre-select the just-saved flight.
                    # Use a flag instead of setting the widget key directly (Streamlit 1.35+
                    # raises StreamlitAPIException if you set a rendered widget's key).
                    st.session_state["_post_save_flight"] = edit_flight
                    st.rerun()
        else:
            st.info("Select or create a flight on the left to edit its route.")

    # ---- All configured routes at a glance --------------------------------
    st.divider()
    st.markdown("**All configured flights**")
    if not routes_master_edit:
        st.info("No flights configured yet.")
    else:
        summary_rows = []
        for fn in sorted(routes_master_edit):
            legs  = routes_master_edit[fn]
            stops = ingest.route_stop_sequence(routes_master_edit, fn)
            ac    = ingest.route_aircraft_type(routes_master_edit, fn)
            ac_display = (
                ingest.AIRCRAFT_TYPE_OPTS[ac]["name"]
                if ac in ingest.AIRCRAFT_TYPE_OPTS else (ac or "—")
            )
            summary_rows.append({
                "Flight":   fn,
                "Aircraft": ac_display,
                "Seats":    ingest.AIRCRAFT_TYPE_OPTS.get(ac, {}).get("seats", "—"),
                "Legs":     len(legs),
                "From":     stops[0]  if stops else "",
                "To":       stops[-1] if stops else "",
                "Dep":      legs[0].get("dep", "") if legs else "",
                "Arr":      legs[-1].get("arr", "") if legs else "",
                "Route":    " → ".join(stops),
            })
        st.dataframe(
            pd.DataFrame(summary_rows),
            width='stretch',
            hide_index=True,
        )


# =========================================================================== #
# TAB — Saved Schedules
# =========================================================================== #

with tab_saved:
    st.subheader("Saved Schedules")
    st.caption(
        "Schedules are only saved when someone clicks **Save this schedule** in the "
        "Run Optimizer tab — nothing here happens automatically."
    )

    _saved_list = ingest.list_saved_schedules()
    if not _saved_list:
        st.info("No schedules have been saved yet.")
    else:
        for _entry in _saved_list:
            _summary = _entry.get("summary", {})
            _label = (
                f"{_entry['manifest_date']} — generated by {_entry['generated_by']} "
                f"on {_entry['generated_at']} ({_entry['engine']}) — "
                f"{_summary.get('served_pax', '?')}/{_summary.get('total_pax', '?')} pax served"
            )
            with st.expander(_label):
                _data = ingest.load_saved_schedule(_entry["file"])
                if _data is None:
                    st.error("Could not load this saved schedule (file missing).")
                    continue

                sc1, sc2, sc3, sc4 = st.columns(4)
                sc1.metric("Pax Served", f"{_summary.get('served_pax','?')} / {_summary.get('total_pax','?')}")
                sc2.metric("Aircraft Used", _summary.get("ac_used", "?"))
                sc3.metric("Total Block", f"{_summary.get('total_block','?')} min")
                sc4.metric("Avg Load Factor", f"{_summary.get('avg_load_factor', 0):.0f}%")

                _diag = _data.get("dropped_diagnoses") or []
                if _diag:
                    st.warning(f"{_summary.get('dropped_pax', 0)} pax on {len(_diag)} booking(s) were unserved.")
                    st.dataframe(
                        pd.DataFrame([
                            {"Booking ID": x["demand_id"], "From→To": f"{x['origin']}→{x['dest']}",
                             "Pax": x["pax"], "Category": x["category"], "Detail": x["detail"]}
                            for x in _diag
                        ]),
                        width='stretch', hide_index=True,
                    )

                _vt1, _vt2 = st.tabs(["Flight Schedule", "Bookings"])
                _df_sched_v   = pd.DataFrame(_data.get("schedule_rows") or [])
                _df_bookings_v = pd.DataFrame(_data.get("booking_rows") or [])
                with _vt1:
                    st.dataframe(_df_sched_v, width='stretch', hide_index=True)
                with _vt2:
                    st.dataframe(_df_bookings_v, width='stretch', hide_index=True)

                _bcol1, _bcol2, _bcol3 = st.columns([2, 2, 1])
                _bcol1.download_button(
                    "⬇ Download schedule.csv", _df_sched_v.to_csv(index=False).encode(),
                    file_name=f"schedule_{_entry['manifest_date']}.csv", mime="text/csv",
                    key=f"dl_sched_{_entry['file']}",
                )
                _bcol2.download_button(
                    "⬇ Download bookings.csv", _df_bookings_v.to_csv(index=False).encode(),
                    file_name=f"bookings_{_entry['manifest_date']}.csv", mime="text/csv",
                    key=f"dl_bookings_{_entry['file']}",
                )
                if _is_admin:
                    if _bcol3.button("🗑 Delete", key=f"del_sched_{_entry['file']}"):
                        ingest.delete_saved_schedule(_entry["file"])
                        st.rerun()


# =========================================================================== #
# TAB — Admin (only visible to admin users)
# =========================================================================== #

if tab_admin is not None:
    with tab_admin:
        st.subheader("User Management")
        st.caption("Only admins can see this tab and create new accounts.")

        st.markdown("**Existing users**")
        st.dataframe(pd.DataFrame(auth.list_users()), width='stretch', hide_index=True)

        st.divider()
        st.markdown("**Create a new user**")
        with st.form("create_user_form"):
            _new_email = st.text_input("Email")
            _new_role = st.selectbox("Role", auth.ROLES, help="admin = can create users; ops = normal access")
            _new_pw1 = st.text_input("Password", type="password", help="At least 8 characters")
            _new_pw2 = st.text_input("Confirm password", type="password")
            _create_submit = st.form_submit_button("Create user", type="primary")
        if _create_submit:
            if _new_pw1 != _new_pw2:
                st.error("Passwords do not match.")
            else:
                try:
                    auth.create_user(_new_email, _new_pw1, _new_role, created_by=_me["username"])
                    st.success(f"Created user {_new_email.strip().lower()} ({_new_role}).")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
