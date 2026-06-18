"""
ingest.py — Convert Auric Air ops documents into manifest.csv rows.

Accepted inputs
---------------
1. Booking_Analysis.csv  — one row per date, columns = flight numbers (UI001…)
2. Passenger list PDF    — passenger names grouped by O-D section header
3. Timings CSV           — leg dep/arr times per flight

Output: a manifest DataFrame ready for the route optimiser.
"""
from __future__ import annotations
import os
import re
from datetime import datetime
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_CSV = os.path.join(_HERE, "data", "flight_routes.csv")

# Airstrip code aliases — maps names used in passenger PDFs to the internal
# code used in airstrips.csv and flight_routes.csv.
# Add a new entry any time ops uses a different name in their PDFs.
CODE_ALIASES: dict[str, str] = {
    "MIGORI": "MGQ",
}


def _resolve_aliases(od_list: list[dict]) -> list[dict]:
    """Replace any aliased codes with their canonical 3-letter internal codes."""
    if not CODE_ALIASES:
        return od_list
    return [
        {**od,
         "origin": CODE_ALIASES.get(od["origin"], od["origin"]),
         "dest":   CODE_ALIASES.get(od["dest"],   od["dest"])}
        for od in od_list
    ]


# Aircraft type catalogue — keys match fleet.csv `type` column and
# flight_routes.csv `aircraft_type` column.
AIRCRAFT_TYPE_OPTS: dict[str, dict] = {
    "C208B":    {"name": "Caravan (C208B)",    "seats": 13},
    "DHC8-100": {"name": "Dash-8 Series 100",  "seats": 37},
    "DHC8-200": {"name": "Dash-8 Series 200",  "seats": 39},
    "DHC8-300": {"name": "Dash-8 Series 300",  "seats": 50},
    "PC12":     {"name": "PC-12",              "seats": 8},
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _hhmm_to_min(hhmm: str) -> int | None:
    """'09:50' → 590.  Returns None if unparseable."""
    m = re.match(r"(\d{1,2}):(\d{2})", str(hhmm).strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _parse_date(raw: str) -> str:
    """Try common date formats; return YYYY-MM-DD or the raw string."""
    raw = raw.strip()
    for fmt in (
        "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
        "%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d %b %y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


# --------------------------------------------------------------------------- #
# 1) Booking Analysis
# --------------------------------------------------------------------------- #

def parse_booking_analysis(f) -> tuple[str | None, dict[str, int]]:
    """
    Parse Booking_Analysis.csv.

    Returns
    -------
    (date_str, {flight_num: pax_count})
    Only flights with pax > 0 are included.
    date_str is 'YYYY-MM-DD' when parseable, else the raw cell text.
    """
    df = pd.read_csv(f)
    date_col = df.columns[0]
    total_cols = [c for c in df.columns if "total" in c.lower()]
    flight_cols = [c for c in df.columns if c not in [date_col] + total_cols]

    # Take first actual-date row (skips any trailing 'Total' summary row)
    mask = df[date_col].astype(str).str.match(r"\d{1,2}[/\-]")
    rows = df[mask] if mask.any() else df.head(1)
    if rows.empty:
        return None, {}

    row = rows.iloc[0]
    date_str = _parse_date(str(row[date_col]))

    counts: dict[str, int] = {}
    for col in flight_cols:
        try:
            v = int(float(row[col]))
            if v > 0:
                counts[col.strip()] = v
        except (ValueError, TypeError):
            pass

    return date_str, counts


# --------------------------------------------------------------------------- #
# 2) Passenger list PDF
# --------------------------------------------------------------------------- #

def _extract_name(line: str) -> str:
    """Pull the passenger name from a numbered entry line."""
    s = line.strip()
    m = _PAX_NAME_AURIC.match(s)
    if m:
        return m.group(1)
    m = _PAX_NAME_GENERIC.match(s)
    if m:
        return m.group(1).strip()
    return ""


def extract_pdf_text(f) -> list[str]:
    """Return all text lines from a PDF file (used for debugging)."""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber not installed — run:  pip install pdfplumber")
    lines: list[str] = []
    with pdfplumber.open(f) as pdf:
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").splitlines())
    return lines


# Non-capturing group for the separator between airport codes
# Matches:  -  –  —  /  (with optional surrounding spaces)
# or:       to  →  ->  (with surrounding spaces)
_SEP = r"(?:\s*[-–—/]\s*|\s+(?:to|→|->)\s+)"
# Airport code: 3–6 uppercase letters (covers standard 3-char codes + MIGORI-style names)
_CODE = r"[A-Z]{3,6}"

# Pattern A — header only:  "GTZ - ARK"  or  "GTZ-ARK"  (nothing else on line)
_HEADER_ONLY = re.compile(rf"^({_CODE}){_SEP}({_CODE})\s*$")
# Pattern B — header with inline pax count:  "GTZ - ARK  8"  or  "GTZ - ARK (8 pax)"
_HEADER_WITH_PAX = re.compile(rf"^({_CODE}){_SEP}({_CODE})[\s:;(]+(\d+)")
# Pattern C — table row "GTZ  ARK  8"  (whitespace-separated, no dash)
_TABLE_ROW = re.compile(rf"^({_CODE})\s+({_CODE})\s+(\d+)")
# Numbered passenger entry — matches both formats:
#   "1. SURNAME"  /  "1) SURNAME"   (period or parenthesis separator)
#   "1 F Mrs. ..."  /  "19 M Mr. ..." (Auric Air format: number + gender code F/M/C/I)
_PAX_ENTRY = re.compile(r"^\s*\d+(?:[.)]\s|\s+[FMCI]\b)")
# Extracts surname + firstname from Auric Air format "1 F Mrs. SURNAME FIRSTNAME PNR"
_PAX_NAME_AURIC  = re.compile(r"^\s*\d+\s+[FMCI]\s+\S+\s+((?:[A-Z]{2,}(?:\s+[A-Z]{2,})+))")
# Extracts name from generic format "1. SURNAME FIRSTNAME"
_PAX_NAME_GENERIC = re.compile(r"^\s*\d+[.)]\s+(.+)")
# Segment-load table header
_SEG_HEAD = re.compile(r"\bFrom\b.{0,10}\bTo\b.{0,10}\bTotal\b", re.IGNORECASE)


def parse_passenger_pdf(f) -> tuple[str | None, str | None, list[dict]]:
    """
    Parse a passenger list PDF.

    Tries four strategies in order:
      A. Section headers on their own line ("GTZ - ARK"), count numbered entries below
      B. Section header with inline pax count ("GTZ - ARK  8")
      C. Three-column table rows ("GTZ  ARK  8") — no header needed
      D. Segment-load table ("From  To  Total" then data rows)

    Returns
    -------
    (flight_num, date_str, od_list)
    od_list: [{'origin': 'GTZ', 'dest': 'ARK', 'pax': 8}, ...]
    raw_lines is accessible via extract_pdf_text() for debugging.
    """
    lines = extract_pdf_text(f)

    # --- flight number & date (scan first 30 lines) ---
    flight_num: str | None = None
    date_str: str | None = None
    for line in lines[:30]:
        if flight_num is None:
            m = re.search(r"\bUI\d{3,4}\b", line)
            if m:
                flight_num = m.group()
        if date_str is None:
            m2 = re.search(
                r"(\d{1,2}[-/\s](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{2})[-/\s]\d{2,4})",
                line, re.IGNORECASE,
            )
            if m2:
                date_str = _parse_date(m2.group())

    # ------------------------------------------------------------------ #
    # Strategy A — section headers + numbered passenger entries
    # ------------------------------------------------------------------ #
    od_a: list[dict] = []
    current_od: tuple[str, str] | None = None
    pax_count = 0
    pax_names: list[str] = []
    in_seg = False
    for line in lines:
        s = line.strip()
        if _SEG_HEAD.search(s):
            in_seg = True
            if current_od and pax_count:
                od_a.append({"origin": current_od[0], "dest": current_od[1],
                             "pax": pax_count, "passengers": pax_names})
            current_od, pax_count, pax_names = None, 0, []
            continue
        if in_seg:
            continue
        m = _HEADER_ONLY.match(s)
        if m:
            if current_od and pax_count:
                od_a.append({"origin": current_od[0], "dest": current_od[1],
                             "pax": pax_count, "passengers": pax_names})
            current_od = (m.group(1), m.group(2))
            pax_count = 0
            pax_names = []
        elif current_od and _PAX_ENTRY.match(s):
            pax_count += 1
            name = _extract_name(s)
            if name:
                pax_names.append(name)
    if current_od and pax_count:
        od_a.append({"origin": current_od[0], "dest": current_od[1],
                     "pax": pax_count, "passengers": pax_names})

    if od_a:
        return flight_num, date_str, _resolve_aliases(od_a)

    # ------------------------------------------------------------------ #
    # Strategy B — section headers with inline pax count
    # ------------------------------------------------------------------ #
    od_b: list[dict] = []
    for line in lines:
        s = line.strip()
        m = _HEADER_WITH_PAX.match(s)
        if m:
            pax = int(m.group(3))
            if pax > 0:
                od_b.append({"origin": m.group(1), "dest": m.group(2), "pax": pax})

    if od_b:
        return flight_num, date_str, _resolve_aliases(od_b)

    # ------------------------------------------------------------------ #
    # Strategy C — whitespace-separated table rows (no dash separator)
    # ------------------------------------------------------------------ #
    od_c: list[dict] = []
    for line in lines:
        s = line.strip()
        m = _TABLE_ROW.match(s)
        if m:
            pax = int(m.group(3))
            if pax > 0:
                od_c.append({"origin": m.group(1), "dest": m.group(2), "pax": pax})

    if od_c:
        return flight_num, date_str, _resolve_aliases(od_c)

    # ------------------------------------------------------------------ #
    # Strategy D — segment-load table only (derive incremental O-D from
    # cumulative load; every stop pair becomes one manifest row)
    # ------------------------------------------------------------------ #
    seg_rows: list[tuple[str, str, int]] = []
    in_seg = False
    for line in lines:
        s = line.strip()
        if _SEG_HEAD.search(s):
            in_seg = True
            continue
        if in_seg:
            m = re.match(rf"^({_CODE})\s+({_CODE})\s+(\d+)", s)
            if m:
                seg_rows.append((m.group(1), m.group(2), int(m.group(3))))
            elif s and not re.match(r"^[A-Z]", s):
                in_seg = False  # table ended

    od_d: list[dict] = []
    if seg_rows:
        prev = 0
        for frm, to, total in seg_rows:
            boarded = total - prev
            if boarded > 0:
                od_d.append({"origin": frm, "dest": to, "pax": boarded})
            prev = total

    return flight_num, date_str, _resolve_aliases(od_d)


# --------------------------------------------------------------------------- #
# 3) Timings CSV
# --------------------------------------------------------------------------- #

def parse_timings_csv(f) -> dict[tuple[str, str], dict]:
    """
    Parse a timings CSV.

    Expected columns (case-insensitive): flight (optional), from, to, dep, arr
    'dep' and 'arr' are HH:MM strings.

    Returns
    -------
    {(from_code, to_code): {"dep": "09:50", "arr": "09:55",
                            "dep_min": 590, "arr_min": 595}}
    """
    df = pd.read_csv(f)
    df.columns = [c.strip().lower() for c in df.columns]

    required = {"from", "to"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Timings CSV is missing columns: {missing}.  "
                         f"Required: from, to, dep, arr")

    result: dict[tuple[str, str], dict] = {}
    dep_col = next((c for c in ("dep", "departure") if c in df.columns), None)
    arr_col = next((c for c in ("arr", "arrival")   if c in df.columns), None)

    for _, row in df.iterrows():
        frm = str(row["from"]).strip().upper()
        to  = str(row["to"]).strip().upper()
        dep = str(row[dep_col]).strip() if dep_col else ""
        arr = str(row[arr_col]).strip() if arr_col else ""
        result[(frm, to)] = {
            "dep":     dep,
            "arr":     arr,
            "dep_min": _hhmm_to_min(dep),
            "arr_min": _hhmm_to_min(arr),
        }

    return result


# --------------------------------------------------------------------------- #
# 4) Build manifest DataFrame
# --------------------------------------------------------------------------- #

def build_manifest(
    od_list:    list[dict],
    timings:    dict[tuple[str, str], dict],
    date:       str,
    flight_num: str | None = None,
) -> pd.DataFrame:
    """
    Convert an O-D demand list + timings into a manifest DataFrame.

    • earliest_dep  — departure time at the origin stop (minutes since midnight)
    • connect_by    — left blank; ops team fills in if a connection is required
    """
    # origin → earliest outbound departure on this flight
    origin_dep: dict[str, int] = {}
    for (frm, _), t in timings.items():
        if t["dep_min"] is not None:
            if frm not in origin_dep or t["dep_min"] < origin_dep[frm]:
                origin_dep[frm] = t["dep_min"]

    prefix = flight_num or "D"
    rows = []
    for i, od in enumerate(od_list, start=1):
        rows.append({
            "id":           f"{prefix}-{i:02d}",
            "date":         date,
            "origin":       od["origin"],
            "dest":         od["dest"],
            "pax":          od["pax"],
            "connect_by":   "",
            "earliest_dep": origin_dep.get(od["origin"], ""),
            "passengers":   ";".join(od.get("passengers", [])),
        })

    return pd.DataFrame(
        rows,
        columns=["id","date","origin","dest","pax","connect_by","earliest_dep","passengers"],
    )


# --------------------------------------------------------------------------- #
# Flight Route Master  —  permanent stop-sequence + published leg times
# --------------------------------------------------------------------------- #

def load_flight_routes(path: str = ROUTES_CSV) -> dict[str, list[dict]]:
    """
    Load data/flight_routes.csv.

    Returns
    -------
    {flight_num: [{from, to, dep, arr, dep_min, arr_min}, ...]}
    Legs are in file order (stop sequence order).
    """
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    routes: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        flight = str(row.get("flight", "")).strip().upper()
        if not flight:
            continue
        leg = {
            "from":          str(row.get("from",          "")).strip().upper(),
            "to":            str(row.get("to",            "")).strip().upper(),
            "dep":           str(row.get("dep",           "")).strip(),
            "arr":           str(row.get("arr",           "")).strip(),
            "aircraft_type": str(row.get("aircraft_type", "")).strip(),
        }
        leg["dep_min"] = _hhmm_to_min(leg["dep"])
        leg["arr_min"] = _hhmm_to_min(leg["arr"])
        routes.setdefault(flight, []).append(leg)
    return routes


def save_flight_routes(routes: dict[str, list[dict]], path: str = ROUTES_CSV) -> None:
    """Persist routes dict back to flight_routes.csv."""
    rows = []
    for flight in sorted(routes):
        for leg in routes[flight]:
            rows.append({
                "flight":        flight,
                "from":          leg.get("from",          ""),
                "to":            leg.get("to",            ""),
                "dep":           leg.get("dep",           ""),
                "arr":           leg.get("arr",           ""),
                "aircraft_type": leg.get("aircraft_type", ""),
            })
    pd.DataFrame(
        rows, columns=["flight", "from", "to", "dep", "arr", "aircraft_type"]
    ).to_csv(path, index=False)


def routes_to_timings(
    routes: dict[str, list[dict]],
    flight_nums: list[str],
) -> dict[tuple[str, str], dict]:
    """
    Merge legs for one or more flights into the timings dict
    format that build_manifest() expects.
    """
    result: dict[tuple[str, str], dict] = {}
    for fn in flight_nums:
        for leg in routes.get(fn, []):
            key = (leg["from"], leg["to"])
            result[key] = {
                "dep":     leg["dep"],
                "arr":     leg["arr"],
                "dep_min": leg.get("dep_min"),
                "arr_min": leg.get("arr_min"),
            }
    return result


def route_stop_sequence(routes: dict[str, list[dict]], flight_num: str) -> list[str]:
    """Return the ordered list of stop codes for a flight (first stop through last stop)."""
    legs = routes.get(flight_num, [])
    if not legs:
        return []
    stops = [legs[0]["from"]] + [l["to"] for l in legs]
    return stops


def route_aircraft_type(routes: dict[str, list[dict]], flight_num: str) -> str:
    """Return the aircraft_type code for a flight, or '' if not set."""
    legs = routes.get(flight_num, [])
    return legs[0].get("aircraft_type", "") if legs else ""


# --------------------------------------------------------------------------- #
# Timings CSV download template (kept for reference / manual import)
# --------------------------------------------------------------------------- #

TIMINGS_TEMPLATE = (
    "flight,from,to,dep,arr\n"
    "UI613,LAM,KOG,09:50,09:55\n"
    "UI613,KOG,LOB,10:00,10:05\n"
    "UI613,LOB,GTZ,10:05,10:10\n"
    "UI613,GTZ,FTI,10:15,10:20\n"
    "UI613,FTI,SAS,10:25,10:30\n"
    "UI613,SAS,SEU,10:35,10:40\n"
    "UI613,SEU,SGS,11:00,11:05\n"
    "UI613,SGS,NTU,11:10,11:15\n"
    "UI613,NTU,MWT,11:20,11:25\n"
    "UI613,MWT,LKY,11:30,11:50\n"
    "UI613,LKY,CHM,11:55,12:00\n"
    "UI613,CHM,KUR,12:05,12:10\n"
    "UI613,KUR,JRO,12:15,12:20\n"
    "UI613,JRO,ARK,12:25,12:35\n"
)
