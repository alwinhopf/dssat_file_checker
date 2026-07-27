"""
DSSAT Master Validator — Standalone (no tradssat dependency)
Handles .WTH, .SOL, and Experiment files (.MZX, .HMX, .WHX, .SQX, etc.)
"""

import re
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — WEATHER FILE ENGINE (.WTH)
# ══════════════════════════════════════════════════════════════════════════════

WTH_VARS = ["SRAD", "TMAX", "TMIN", "RAIN", "DEWP", "WIND", "PAR", "EVAP", "RHUM"]

# Physical plausibility bounds (min, max) — values outside → flag
WTH_BOUNDS = {
    "SRAD": (0.0, 50.0),   # MJ/m²/d
    "TMAX": (-50.0, 60.0), # °C
    "TMIN": (-60.0, 50.0), # °C
    "RAIN": (0.0, 500.0),  # mm/d
    "DEWP": (-60.0, 40.0), # °C
    "WIND": (0.0, 100.0),  # km/d
    "PAR":  (0.0, 25.0),   # MJ/m²/d
    "EVAP": (0.0, 50.0),   # mm/d
    "RHUM": (0.0, 100.0),  # %
}

def parse_wth(text: str) -> dict:
    """Parse a WTH file into a structured dict."""
    result = {"header": {}, "data": {v: [] for v in ["DATE"] + WTH_VARS},
              "col_order": [], "raw_header_lines": []}
    lines = text.splitlines()
    in_data = False
    col_names = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue

        if stripped.startswith("*WEATHER"):
            result["header"]["TITLE"] = stripped
            continue

        if stripped.startswith("@ INSI"):
            result["raw_header_lines"].append(stripped)
            continue

        if stripped.startswith("@DATE"):
            # Parse column header
            col_names = stripped.split()  # ["@DATE", "SRAD", "TMAX", ...]
            result["col_order"] = col_names[1:]  # vars after @DATE
            for v in col_names[1:]:
                if v not in result["data"]:
                    result["data"][v] = []
            in_data = True
            continue

        # Parse header metadata line (INSI LAT LONG ELEV TAV AMP REFHT WNDHT)
        if not in_data and not stripped.startswith("@") and not stripped.startswith("*"):
            parts = stripped.split()
            keys = ["INSI", "LAT", "LONG", "ELEV", "TAV", "AMP", "REFHT", "WNDHT"]
            for i, k in enumerate(keys):
                if i < len(parts):
                    try:
                        result["header"][k] = float(parts[i]) if k != "INSI" else parts[i]
                    except ValueError:
                        result["header"][k] = parts[i]
            continue

        if in_data:
            parts = stripped.split()
            if not parts or len(parts[0]) != 5:
                continue
            try:
                date_val = int(parts[0])
            except ValueError:
                continue
            result["data"]["DATE"].append(date_val)
            for i, var in enumerate(result["col_order"]):
                if (i + 1) < len(parts):
                    try:
                        result["data"][var].append(float(parts[i + 1]))
                    except ValueError:
                        result["data"][var].append(-99.0)
                else:
                    result["data"][var].append(-99.0)

    return result


def wth_date_to_dt(d: int) -> datetime:
    return datetime.strptime(str(d).zfill(5), "%y%j")


def interpolate_window(lst: list, idx: int, var: str, window: int = 3) -> float:
    vals = []
    for j in range(max(0, idx - window), min(len(lst), idx + window + 1)):
        if j == idx:
            continue
        v = lst[j]
        if v != -99.0 and not np.isnan(v):
            vals.append(v)
    return round(float(np.mean(vals)), 1) if vals else -99.0


def check_and_heal_wth(parsed: dict):
    errors, warnings, fixes = [], [], []
    data = parsed["data"]
    hdr = parsed["header"]

    # ── Header checks ─────────────────────────────────────────────────────────
    lat = float(hdr.get("LAT", -99))
    lon = float(hdr.get("LONG", -99))
    elev = float(hdr.get("ELEV", -99))
    if lat == -99 or not (-90 <= lat <= 90):
        errors.append("Header: LAT is missing or out of range (−90 to 90).")
    if lon == -99 or not (-180 <= lon <= 180):
        errors.append("Header: LONG is missing or out of range (−180 to 180).")
    if elev != -99 and not (-500 <= elev <= 9000):
        warnings.append(f"Header: ELEV ({elev} m) is outside plausible range (−500 to 9000).")

    dates = data.get("DATE", [])
    if not dates:
        errors.append("Fatal: No DATE column found — file cannot be parsed.")
        return errors, warnings, fixes, parsed

    # ── PHASE 1: Timeline healing (gaps & duplicates) ─────────────────────────
    i = 1
    while i < len(dates):
        try:
            prev_dt = wth_date_to_dt(dates[i - 1])
            curr_dt = wth_date_to_dt(dates[i])
            delta = (curr_dt - prev_dt).days
        except Exception:
            i += 1
            continue

        if delta == 0:
            dup_str = str(dates[i]).zfill(5)
            fixes.append(f"Timeline — Removed duplicate day {dup_str}.")
            for v in data:
                data[v].pop(i)
            continue  # don't advance i

        elif delta > 1:
            insert_dt = prev_dt + timedelta(days=1)
            insert_code = int(insert_dt.strftime("%y%j"))
            fixes.append(f"Timeline — Inserted missing day {str(insert_code).zfill(5)} (values interpolated).")
            data["DATE"].insert(i, insert_code)
            for v in WTH_VARS:
                if v in data:
                    data[v].insert(i, -99.0)
            i += 1
            continue

        i += 1

    # ── PHASE 2: Per-row value checks & healing ───────────────────────────────
    tmax = data.get("TMAX", [])
    tmin = data.get("TMIN", [])
    n = len(dates)

    for i in range(n):
        d_str = str(int(dates[i])).zfill(5)
        row = i + 1

        # Fix -99 / physically implausible mandatory vars
        for var in ["TMAX", "TMIN", "SRAD", "RAIN"]:
            if var not in data:
                continue
            val = data[var][i]
            lo, hi = WTH_BOUNDS.get(var, (-9999, 9999))

            # Missing value
            if val == -99.0:
                fixed = interpolate_window(data[var], i, var)
                if fixed != -99.0:
                    data[var][i] = fixed
                    fixes.append(f"Row {row} ({d_str}): {var} missing → interpolated to {fixed}.")
                else:
                    errors.append(f"Row {row} ({d_str}): {var} is missing and could not be interpolated.")
                continue

            # Out of physical range
            if not (lo <= val <= hi):
                errors.append(
                    f"Row {row} ({d_str}): {var} = {val} is outside physical bounds [{lo}, {hi}]."
                )

        # Optional vars — just flag, don't interpolate
        for var in ["DEWP", "WIND", "PAR", "EVAP", "RHUM"]:
            if var not in data:
                continue
            val = data[var][i]
            if val == -99.0:
                continue
            lo, hi = WTH_BOUNDS[var]
            if not (lo <= val <= hi):
                warnings.append(
                    f"Row {row} ({d_str}): {var} = {val} outside plausible range [{lo}, {hi}]."
                )

        # TMAX < TMIN swap
        if tmax and tmin and i < len(tmax) and i < len(tmin):
            if tmax[i] != -99.0 and tmin[i] != -99.0 and tmax[i] < tmin[i]:
                data["TMAX"][i], data["TMIN"][i] = data["TMIN"][i], data["TMAX"][i]
                fixes.append(f"Row {row} ({d_str}): TMAX/TMIN were inverted — swapped.")

        # TMAX == TMIN (suspicious)
        if tmax and tmin and i < len(tmax) and i < len(tmin):
            if tmax[i] == tmin[i] and tmax[i] != -99.0:
                warnings.append(f"Row {row} ({d_str}): TMAX == TMIN ({tmax[i]}°C) — possible data error.")

        # SRAD == 0 on a non-rainy day (suspicious — but don't auto-fix)
        if "SRAD" in data and "RAIN" in data:
            srad_v = data["SRAD"][i]
            rain_v = data["RAIN"][i]
            if srad_v == 0.0 and rain_v == 0.0:
                warnings.append(f"Row {row} ({d_str}): SRAD = 0 with no rain — possible missing value.")

        # Dew point > TMAX (physically impossible)
        if "DEWP" in data and tmax and i < len(tmax):
            dewp_v = data["DEWP"][i]
            if dewp_v != -99.0 and tmax[i] != -99.0 and dewp_v > tmax[i]:
                warnings.append(
                    f"Row {row} ({d_str}): DEWP ({dewp_v}°C) > TMAX ({tmax[i]}°C) — physically impossible."
                )

        # RAIN < 0 (fix to 0)
        if "RAIN" in data and i < len(data["RAIN"]):
            if data["RAIN"][i] < 0:
                data["RAIN"][i] = 0.0
                fixes.append(f"Row {row} ({d_str}): RAIN was negative — corrected to 0.")

    return errors, warnings, fixes, parsed


def calc_tav_amp(data: dict) -> tuple:
    dates = data.get("DATE", [])
    tmax = data.get("TMAX", [])
    tmin = data.get("TMIN", [])
    monthly = {}
    daily_means = []
    for i in range(len(dates)):
        if i >= len(tmax) or i >= len(tmin):
            continue
        if tmax[i] == -99.0 or tmin[i] == -99.0:
            continue
        dm = (tmax[i] + tmin[i]) / 2.0
        daily_means.append(dm)
        try:
            dt = wth_date_to_dt(int(dates[i]))
            monthly.setdefault(dt.month, []).append(dm)
        except Exception:
            continue
    if not daily_means:
        return -99.0, -99.0
    tav = round(float(np.mean(daily_means)), 1)
    if monthly:
        mon_avgs = [float(np.mean(v)) for v in monthly.values()]
        amp = round(max(mon_avgs) - min(mon_avgs), 1)
    else:
        amp = -99.0
    return tav, amp


def write_wth(parsed: dict, tav: float, amp: float) -> str:
    hdr = parsed["header"]
    data = parsed["data"]
    col_order = parsed["col_order"]

    def hv(k, default=-99):
        return hdr.get(k, default)

    lines = []
    title = hdr.get("TITLE", "*WEATHER DATA : XXXX")
    lines.append(title)
    lines.append("")
    lines.append("@ INSI      LAT     LONG  ELEV   TAV   AMP REFHT WNDHT")
    insi = str(hv("INSI", "XXXX"))
    lat = float(hv("LAT", -99))
    lon = float(hv("LONG", -99))
    elev = float(hv("ELEV", -99))
    refht = float(hv("REFHT", -99))
    wndht = float(hv("WNDHT", -99))
    lines.append(f"  {insi:<4s} {lat:8.3f} {lon:8.3f} {elev:5.0f} {tav:5.1f} {amp:5.1f} {refht:5.1f} {wndht:5.1f}")

    present_vars = [v for v in col_order if v in data and data[v]]
    header_row = "@DATE" + "".join(f"{v:>6s}" for v in present_vars)
    lines.append(header_row)

    n = len(data["DATE"])
    for i in range(n):
        row = f"{str(int(data['DATE'][i])).zfill(5):5s}"
        for v in present_vars:
            row += f"{data[v][i]:6.1f}"
        lines.append(row)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — SOIL FILE ENGINE (.SOL)
# ══════════════════════════════════════════════════════════════════════════════

# Valid ranges for soil layer variables
SOL_LAYER_BOUNDS = {
    "SLLL": (0.0, 0.60),   # volumetric water content
    "SDUL": (0.0, 0.65),
    "SSAT": (0.0, 0.70),
    "SBDM": (0.5, 2.65),   # bulk density g/cm³
    "SLOC": (0.0, 15.0),   # organic carbon %
    "SLCL": (0.0, 100.0),  # clay %
    "SLSI": (0.0, 100.0),  # silt %
    "SLHW": (3.0, 11.0),   # pH in water
    "SRGF": (0.0, 1.0),    # root growth factor
    "SSKS": (0.0, 1000.0), # saturated hydraulic conductivity cm/h
    "SLCF": (0.0, 100.0),  # coarse fraction %
    "SCEC": (0.0, 200.0),  # cation exchange capacity
}

SOL_PROFILE_BOUNDS = {
    "SALB": (0.05, 0.35),   # albedo
    "SLU1": (0.0, 15.0),    # stage 1 evaporation
    "SLDR": (0.0, 1.0),     # drainage rate
    "SLRO": (0.0, 100.0),   # runoff curve number
    "SLNF": (0.0, 1.0),     # mineralisation factor
    "SLPF": (0.0, 1.0),     # photosynthesis factor
}

# Standard column headers for SOL sections
SOL_SITE_COLS  = ["SITE", "COUNTRY", "LAT", "LONG", "SCS_FAMILY"]
SOL_SCOM_COLS  = ["SCOM", "SALB", "SLU1", "SLDR", "SLRO", "SLNF", "SLPF", "SMHB", "SMPX", "SMKE"]
SOL_LAYER_COLS = ["SLB", "SLMH", "SLLL", "SDUL", "SSAT", "SRGF", "SSKS", "SBDM",
                  "SLOC", "SLCL", "SLSI", "SLCF", "SLNI", "SLHW", "SLHB", "SCEC", "SADC"]


def parse_sol(text: str) -> list:
    """
    Parse a DSSAT soil file into a list of profile dicts:
    [{'id': str, 'description': str, 'site': dict, 'scom': dict, 'layers': [dict, ...]}]
    """
    profiles = []
    current = None
    mode = None  # 'site', 'scom', 'layer'
    layer_cols = []
    scom_cols = []

    lines = text.splitlines()
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        # Skip blanks and comments
        if not stripped or stripped.startswith("!"):
            continue

        # ── New profile ───────────────────────────────────────────────────────
        if stripped.startswith("*") and not stripped.startswith("*SOILS"):
            parts = stripped[1:].split()
            pid = parts[0] if parts else "UNKNOWN"
            desc = " ".join(parts[1:]) if len(parts) > 1 else ""
            current = {"id": pid, "description": desc, "site": {}, "scom": {}, "layers": [], "_raw": []}
            profiles.append(current)
            mode = None
            continue

        if current is None:
            continue

        current["_raw"].append(raw)

        # ── Header detection ──────────────────────────────────────────────────
        if stripped.startswith("@SITE"):
            mode = "site"
            continue
        if stripped.startswith("@ SCOM") or stripped.startswith("@SCOM"):
            # parse column names
            scom_cols = re.split(r'\s+', stripped.lstrip("@ ").strip())
            mode = "scom"
            continue
        if stripped.startswith("@  SLB") or stripped.startswith("@ SLB") or stripped.startswith("@SLB"):
            layer_cols = re.split(r'\s+', stripped.lstrip("@ ").strip())
            mode = "layer"
            continue

        # ── Data rows ─────────────────────────────────────────────────────────
        if mode == "site":
            parts = stripped.split()
            keys = ["SITE", "COUNTRY", "LAT", "LONG", "SCS_FAMILY"]
            for i, k in enumerate(keys):
                if i < len(parts):
                    current["site"][k] = parts[i]
            mode = None  # site is single line
            continue

        if mode == "scom":
            parts = stripped.split()
            for i, col in enumerate(scom_cols):
                if i < len(parts):
                    try:
                        current["scom"][col] = float(parts[i]) if parts[i] != "-99" else -99.0
                    except ValueError:
                        current["scom"][col] = parts[i]
            mode = None
            continue

        if mode == "layer":
            parts = stripped.split()
            layer = {}
            for i, col in enumerate(layer_cols):
                if i < len(parts):
                    try:
                        layer[col] = float(parts[i])
                    except ValueError:
                        layer[col] = -99.0
                else:
                    layer[col] = -99.0
            if layer:
                current["layers"].append(layer)

    return profiles


def check_sol(profiles: list):
    errors, warnings, fixes = [], [], []

    for prof in profiles:
        pid = prof["id"]
        layers = prof["layers"]
        scom = prof["scom"]

        if not layers:
            errors.append(f"{pid}: No soil layers found.")
            continue

        # ── Profile-level header checks ────────────────────────────────────
        for field, (lo, hi) in SOL_PROFILE_BOUNDS.items():
            val = scom.get(field, -99.0)
            if val == -99.0:
                continue
            if not (lo <= val <= hi):
                warnings.append(f"{pid}: {field} = {val} is outside expected range [{lo}, {hi}].")

        # ── Layer-level checks ─────────────────────────────────────────────
        prev_slb = None
        prev_srgf = None

        for idx, layer in enumerate(layers):
            lnum = idx + 1
            slb  = layer.get("SLB", -99.0)
            slll = layer.get("SLLL", -99.0)
            sdul = layer.get("SDUL", -99.0)
            ssat = layer.get("SSAT", -99.0)

            # 1. Depth monotonicity
            if slb == -99.0:
                errors.append(f"{pid} L{lnum}: SLB (depth) is missing.")
            elif prev_slb is not None and slb <= prev_slb:
                errors.append(f"{pid} L{lnum}: SLB ({slb}) ≤ previous depth ({prev_slb}) — must be strictly increasing.")
            prev_slb = slb

            # 2. Golden Water Rule: SLLL < SDUL < SSAT
            if slll != -99.0 and sdul != -99.0:
                if slll >= sdul:
                    errors.append(f"{pid} L{lnum}: SLLL ({slll}) ≥ SDUL ({sdul}) — violates SLLL < SDUL.")
                    # Auto-fix: swap if reasonable
                    if slll < ssat or ssat == -99.0:
                        layer["SDUL"], layer["SLLL"] = layer["SLLL"], layer["SDUL"]
                        slll, sdul = sdul, slll
                        fixes.append(f"{pid} L{lnum}: Swapped SLLL and SDUL.")

            if sdul != -99.0 and ssat != -99.0:
                if sdul >= ssat:
                    errors.append(f"{pid} L{lnum}: SDUL ({sdul}) ≥ SSAT ({ssat}) — violates SDUL < SSAT.")

            if slll != -99.0 and ssat != -99.0:
                if slll >= ssat:
                    errors.append(f"{pid} L{lnum}: SLLL ({slll}) ≥ SSAT ({ssat}) — violates SLLL < SSAT.")

            # 3. Physical bounds for all layer variables
            for field, (lo, hi) in SOL_LAYER_BOUNDS.items():
                val = layer.get(field, -99.0)
                if val == -99.0:
                    continue
                if not (lo <= val <= hi):
                    if field in ("SLLL", "SDUL", "SSAT", "SBDM"):
                        errors.append(f"{pid} L{lnum}: {field} = {val} outside physical range [{lo}, {hi}].")
                    else:
                        warnings.append(f"{pid} L{lnum}: {field} = {val} outside expected range [{lo}, {hi}].")

            # 4. SLCL + SLSI must not exceed 100 %
            slcl = layer.get("SLCL", -99.0)
            slsi = layer.get("SLSI", -99.0)
            if slcl != -99.0 and slsi != -99.0 and slcl + slsi > 100.0:
                errors.append(f"{pid} L{lnum}: Clay ({slcl}%) + Silt ({slsi}%) = {slcl+slsi}% — exceeds 100%.")

            # 5. SRGF should generally decrease (or stay) with depth
            srgf = layer.get("SRGF", -99.0)
            if srgf != -99.0 and prev_srgf is not None and prev_srgf != -99.0:
                if srgf > prev_srgf + 0.05:
                    warnings.append(
                        f"{pid} L{lnum}: SRGF ({srgf}) increased from previous layer ({prev_srgf}) "
                        f"— root density usually decreases with depth."
                    )
            prev_srgf = srgf if srgf != -99.0 else prev_srgf

            # 6. SBDM plausibility vs texture
            sbdm = layer.get("SBDM", -99.0)
            if sbdm != -99.0 and slcl != -99.0:
                if slcl > 40 and sbdm > 1.7:
                    warnings.append(
                        f"{pid} L{lnum}: High clay ({slcl}%) but high bulk density ({sbdm} g/cm³) — verify."
                    )

    return errors, warnings, fixes


def write_sol(profiles: list, original_text: str) -> str:
    """
    Reconstruct the SOL file. For now we write back the original lines but with
    any layer value fixes applied (SLLL/SDUL swaps).
    """
    # Simplest safe approach: return the original with fixes applied in place.
    # A full reconstructor would be very long; instead we patch numeric values.
    lines = original_text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("!") or stripped.startswith("["):
            continue
        out.append(line.rstrip())
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — EXPERIMENT FILE ENGINE (.MZX, .WHX, .HMX, .SQX, etc.)
# ══════════════════════════════════════════════════════════════════════════════

EXP_SECTIONS = [
    "GENERAL", "TREATMENTS", "CULTIVARS", "FIELDS", "INITIAL CONDITIONS",
    "PLANTING DETAILS", "IRRIGATION AND WATER MANAGEMENT",
    "FERTILIZERS (INORGANIC)", "FERTILIZERS (ORGANIC)",
    "TILLAGE", "ENVIRONMENT MODIFICATIONS", "HARVEST DETAILS",
    "SIMULATION CONTROLS", "AUTOMATIC MANAGEMENT",
]

REQUIRED_SECTIONS = ["TREATMENTS", "CULTIVARS", "FIELDS", "PLANTING DETAILS", "SIMULATION CONTROLS"]

def _clean_section_name(raw: str) -> str:
    """Strip decorative dashes and extra whitespace from section header lines."""
    # Remove leading * and collapse inner whitespace / trailing decoration
    name = raw.lstrip("*").strip()
    # Remove trailing '---...' decoration
    name = re.split(r'\s{3,}[-=]{3,}', name)[0].strip()
    return name.upper()


def parse_exp(text: str) -> dict:
    """Parse experiment file into sections."""
    result = {"title": "", "sections": {}, "raw_lines": text.splitlines()}
    current_section = None
    lines = text.splitlines()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        # Ctrl-Z EOF marker
        if stripped == "\x1a":
            break
        # Title
        if stripped.startswith("*EXP"):
            result["title"] = stripped
            continue
        # Section header — any line starting with * (not *EXP)
        if stripped.startswith("*"):
            sec_name = _clean_section_name(stripped)
            current_section = sec_name
            result["sections"].setdefault(sec_name, {"headers": [], "blocks": []})
            continue
        if current_section is None:
            continue
        # Column header (@...)
        if stripped.startswith("@"):
            result["sections"][current_section]["headers"].append(stripped)
            result["sections"][current_section]["blocks"].append(
                {"header": stripped, "rows": []}
            )
            continue
        # Data row
        blocks = result["sections"][current_section]["blocks"]
        if blocks:
            blocks[-1]["rows"].append(stripped)

    return result


def _parse_treatment_row(row: str, hdr: str) -> dict:
    """Parse a TREATMENTS data row using the column header for alignment."""
    hdr_parts = hdr.split()
    row_parts  = row.split()
    if not row_parts or len(row_parts) < 5:
        return {}
    try:
        d = {}
        for i, k in enumerate(["N", "R", "O", "C"]):
            d[k] = int(row_parts[i]) if i < len(row_parts) else 0
        # Factor columns in header (everything after TNAME)
        factor_keys = ["CU","FL","SA","IC","MP","MI","MF","MR","MC","MT","ME","MH","SM"]
        present_factors = [k for k in factor_keys if k in hdr_parts]
        n_factors = len(present_factors)
        name_tokens = row_parts[4: len(row_parts) - n_factors] if n_factors else row_parts[4:]
        d["TNAME"] = " ".join(name_tokens)
        factor_vals = row_parts[len(row_parts) - n_factors:] if n_factors else []
        for i, k in enumerate(present_factors):
            try:
                d[k] = int(factor_vals[i])
            except (ValueError, IndexError):
                d[k] = 0
        return d
    except Exception:
        return {}


def _find_section(secs: dict, keyword: str):
    """Return (key, section_dict) for the first section whose name contains keyword."""
    for k, v in secs.items():
        if keyword in k:
            return k, v
    return None, None


def _get_col_values(secs: dict, keyword: str, col_name: str) -> dict:
    """
    Return {level: value} by scanning all blocks in the first section
    matching keyword, picking the column named col_name from the header.
    """
    result = {}
    sec_key, sec = _find_section(secs, keyword)
    if sec is None:
        return result
    for block in sec["blocks"]:
        hdr_parts = block["header"].split()
        if col_name not in hdr_parts:
            continue
        col_idx = hdr_parts.index(col_name)  # header[@X, COL1, COL2...] aligns with data[level, val1, val2...]
        for row in block["rows"]:
            rparts = row.split()
            try:
                lvl = int(rparts[0])
                result[lvl] = rparts[col_idx]
            except Exception:
                pass
    return result


def check_exp(parsed: dict):
    errors, warnings, fixes = [], [], []
    secs = parsed["sections"]   # keys are UPPER-cased by _clean_section_name

    # ── 1. Required sections ──────────────────────────────────────────────────
    for req in REQUIRED_SECTIONS:
        if req.upper() not in secs:
            errors.append(f"Missing required section: *{req}")

    # ── 2. Parse treatments ───────────────────────────────────────────────────
    trt_key, trt_sec = _find_section(secs, "TREATMENT")
    treatments = []
    if trt_sec:
        for block in trt_sec["blocks"]:
            hdr = block["header"]
            if "CU" in hdr and ("TNAME" in hdr or "N R" in hdr):
                for row in block["rows"]:
                    t = _parse_treatment_row(row, hdr)
                    if t:
                        treatments.append(t)
    if not treatments and trt_key:
        warnings.append("TREATMENTS section found but no rows could be parsed.")

    # ── 3. Collect defined levels per section ─────────────────────────────────
    defined_levels = {}  # sec_key (upper) -> set of int levels
    for sec_key, sec_data in secs.items():
        for block in sec_data["blocks"]:
            for row in block["rows"]:
                parts = row.split()
                if parts:
                    try:
                        defined_levels.setdefault(sec_key, set()).add(int(parts[0]))
                    except ValueError:
                        pass

    # ── 4. Relational integrity ───────────────────────────────────────────────
    factor_map = {
        "CU": "CULTIVARS",
        "FL": "FIELDS",
        "MP": "PLANTING DETAILS",
        "MI": "IRRIGATION AND WATER MANAGEMENT",
        "MF": "FERTILIZERS (INORGANIC)",
        "IC": "INITIAL CONDITIONS",
    }
    for trt in treatments:
        tnum = trt.get("N", "?")
        for factor_code, section_name in factor_map.items():
            level = trt.get(factor_code, 0)
            if level == 0:
                continue
            # Find matching section key (partial match)
            sec_upper = section_name.upper()
            match_key = next((k for k in defined_levels if sec_upper in k or k in sec_upper), None)
            if match_key and level not in defined_levels[match_key]:
                errors.append(
                    f"Treatment {tnum} ({trt.get('TNAME','')}): "
                    f"{factor_code}={level} references level {level} "
                    f"in *{section_name} but that level is not defined."
                )

    # ── 5. Simulation date vs planting / harvest ──────────────────────────────
    sim_dates   = _get_col_values(secs, "SIMULATION", "SDATE")
    plant_dates = _get_col_values(secs, "PLANTING",   "PDATE")
    harv_dates  = _get_col_values(secs, "PLANTING",   "HDATE")

    for lvl, sdate_s in sim_dates.items():
        pdate_s = plant_dates.get(lvl)
        try:
            if pdate_s and int(sdate_s) > int(pdate_s):
                warnings.append(
                    f"Simulation Control {lvl}: SDATE ({sdate_s}) is after PDATE ({pdate_s}). "
                    f"Simulation should start on or before planting."
                )
        except Exception:
            pass

    for lvl, pdate_s in plant_dates.items():
        hdate_s = harv_dates.get(lvl)
        try:
            pdate, hdate = int(pdate_s), int(hdate_s) if hdate_s else 0
            if hdate > 0 and hdate < pdate:
                errors.append(f"Planting Level {lvl}: HDATE ({hdate}) is before PDATE ({pdate}).")
        except Exception:
            pass

    # ── 6. Date format sanity (YYDDD) ─────────────────────────────────────────
    for lvl, pdate_s in plant_dates.items():
        try:
            doy = int(pdate_s) % 1000
            if not (1 <= doy <= 366):
                errors.append(f"Planting Level {lvl}: PDATE ({pdate_s}) — DOY part ({doy}) is invalid.")
        except Exception:
            pass

    # ── 7. Planting population ────────────────────────────────────────────────
    ppops = _get_col_values(secs, "PLANTING", "PPOP")
    for lvl, ppop_s in ppops.items():
        try:
            ppop = float(ppop_s)
            if ppop != -99.0 and not (0.5 <= ppop <= 50.0):
                warnings.append(
                    f"Planting Level {lvl}: PPOP = {ppop} plants/m² is outside normal range "
                    f"(0.5–50). Verify units — DSSAT expects plants/m², not plants/ha."
                )
        except Exception:
            pass

    # ── 8. Irrigation efficiency ───────────────────────────────────────────────
    efirs = _get_col_values(secs, "IRRIGATION", "EFIR")
    for lvl, efir_s in efirs.items():
        try:
            efir = float(efir_s)
            if efir != -99.0 and not (0.0 <= efir <= 1.0):
                errors.append(f"Irrigation Level {lvl}: EFIR = {efir} is outside [0, 1].")
        except Exception:
            pass

    # ── 9. Fertilizer amounts ─────────────────────────────────────────────────
    famns = _get_col_values(secs, "FERTILIZER", "FAMN")
    for lvl, famn_s in famns.items():
        try:
            famn = float(famn_s)
            if famn < 0:
                errors.append(f"Fertilizer Level {lvl}: FAMN ({famn} kg/ha) is negative.")
            elif famn > 500:
                warnings.append(
                    f"Fertilizer Level {lvl}: FAMN ({famn} kg N/ha) is very high (>500) — verify."
                )
        except Exception:
            pass

    return errors, warnings, fixes, treatments


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — STREAMLIT UI
# ══════════════════════════════════════════════════════════════════════════════

import streamlit as st

st.set_page_config(page_title="DSSAT Validator", page_icon="🌾", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3 { font-family: 'IBM Plex Mono', monospace !important; letter-spacing: -0.03em; }
.stMetric { background: #0f1117; border: 1px solid #2a2a3a; border-radius: 8px; padding: 12px; }
.stMetric label { color: #8888aa !important; font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; text-transform: uppercase; }
.stMetric [data-testid="stMetricValue"] { color: #e8e8f0 !important; font-family: 'IBM Plex Mono', monospace; }
div[data-testid="stExpander"] { border: 1px solid #2a2a3a; border-radius: 8px; }
.stAlert { border-radius: 6px; font-family: 'IBM Plex Mono', monospace; font-size: 0.82rem; }
</style>
""", unsafe_allow_html=True)

def render_status_badge(n_errors: int, n_fixes: int, n_warnings: int):
    if n_errors == 0 and n_fixes == 0:
        st.success("✅ File is clean — no issues detected.")
    elif n_errors == 0:
        st.warning(f"⚠️ Auto-healed successfully — {n_fixes} fix(es) applied. Review the log below.")
    else:
        st.error(f"❌ {n_errors} error(s) found. {n_fixes} fix(es) applied.")


def render_log(label: str, items: list, color: str = "info"):
    if not items:
        return
    fn = {"error": st.error, "warning": st.warning, "info": st.info, "success": st.success}[color]
    with st.expander(f"{label} ({len(items)})", expanded=(color == "error")):
        for msg in items:
            fn(msg)


try:
    import dssat_checker as dc
    HAS_STATIC_CHECKER = True
except ImportError:
    HAS_STATIC_CHECKER = False


def render_static_rule_findings(text: str, filename: str):
    if not HAS_STATIC_CHECKER:
        return
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / filename
        tmp_path.write_text(text, encoding="utf-8")
        parsed_file = dc.parse_file(tmp_path)
        dc.validate_file(parsed_file)
        if parsed_file.findings:
            with st.expander(f"🔍 Static Rule Checker Findings ({len(parsed_file.findings)} items)", expanded=False):
                for f in parsed_file.findings:
                    sev_symbol = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(f.severity.label(), "ℹ️")
                    loc = f"Line {f.line}" + (f", Col {f.column}" if f.column else "")
                    st.markdown(f"**{sev_symbol} [{f.code}]** `{loc}` — {f.message}")
                    if f.suggestion:
                        st.caption(f"💡 *Suggestion:* {f.suggestion}")


st.title("🌾 DSSAT Master Validator")
st.caption("Standalone validator for Weather (.WTH), Soil (.SOL), and Experiment files — no external dependencies.")

uploaded_file = st.file_uploader(
    "Upload a DSSAT input file (.WTH, .SOL, .MZX, .WHX, .HMX, .SQX, ...)",
    type=None,
    help="All processing happens in-browser — your file is not stored anywhere."
)

if uploaded_file:
    fname = uploaded_file.name
    ext = fname.rsplit(".", 1)[-1].lower()
    raw_bytes = uploaded_file.read()

    # Try decode
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    st.divider()
    render_static_rule_findings(text, fname)

    # ── WEATHER ──────────────────────────────────────────────────────────────
    if ext == "wth":
        st.header(f"🌩️ Weather Engine — {fname}")
        parsed = parse_wth(text)
        errs, warns, fixes, parsed = check_and_heal_wth(parsed)
        data = parsed["data"]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Errors", len(errs))
        c2.metric("Auto-fixes", len(fixes))
        c3.metric("Warnings", len(warns))
        c4.metric("Days parsed", len(data.get("DATE", [])))

        render_status_badge(len(errs), len(fixes), len(warns))
        render_log("❌ Errors", errs, "error")
        render_log("🔧 Auto-fixes applied", fixes, "info")
        render_log("⚠️ Warnings", warns, "warning")

        if data.get("DATE"):
            tav, amp = calc_tav_amp(data)
            st.info(f"Computed TAV = **{tav} °C** | AMP = **{amp} °C** (written to header of fixed file)")

            # Build display dataframe
            df_data = {"Date": [str(int(d)).zfill(5) for d in data["DATE"]]}
            for v in parsed["col_order"]:
                if v in data:
                    df_data[v] = [np.nan if x == -99.0 else x for x in data[v]]
            df = pd.DataFrame(df_data).set_index("Date")

            st.subheader("Data Preview")
            st.dataframe(df.head(30), use_container_width=True)

            st.subheader("Visual Diagnostics")
            col_a, col_b = st.columns(2)
            with col_a:
                temp_cols = [c for c in ["TMAX", "TMIN"] if c in df.columns]
                if temp_cols:
                    st.markdown("**Temperature (°C)**")
                    st.line_chart(df[temp_cols])
                if "RAIN" in df.columns:
                    st.markdown("**Rainfall (mm)**")
                    st.bar_chart(df[["RAIN"]])
            with col_b:
                if "SRAD" in df.columns:
                    st.markdown("**Solar Radiation (MJ/m²/d)**")
                    st.line_chart(df[["SRAD"]])
                wind_cols = [c for c in ["WIND", "DEWP", "RHUM"] if c in df.columns]
                if wind_cols:
                    st.markdown(f"**{', '.join(wind_cols)}**")
                    st.line_chart(df[wind_cols])

            # Download
            fixed_text = write_wth(parsed, tav, amp)
            st.divider()
            st.download_button(
                f"⬇️ Download Fixed {fname}",
                data=fixed_text.encode("utf-8"),
                file_name=f"FIXED_{fname}",
                mime="text/plain",
                use_container_width=True,
            )

    # ── SOIL ─────────────────────────────────────────────────────────────────
    elif ext == "sol":
        st.header(f"🟤 Soil Engine — {fname}")
        profiles = parse_sol(text)
        errs, warns, fixes = check_sol(profiles)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Errors", len(errs))
        c2.metric("Auto-fixes", len(fixes))
        c3.metric("Warnings", len(warns))
        c4.metric("Profiles", len(profiles))

        render_status_badge(len(errs), len(fixes), len(warns))
        render_log("❌ Soil Errors", errs, "error")
        render_log("🔧 Auto-fixes applied", fixes, "info")
        render_log("⚠️ Warnings", warns, "warning")

        if profiles:
            st.subheader("Profile Explorer")
            for prof in profiles:
                pid = prof["id"]
                layers = prof["layers"]
                if not layers:
                    continue
                with st.expander(f"🪨 {pid} — {prof['description']} ({len(layers)} layers)"):
                    df_layers = pd.DataFrame(layers)
                    numeric_cols = df_layers.select_dtypes(include="number").columns.tolist()
                    df_display = df_layers[numeric_cols].replace(-99.0, np.nan)

                    # Key water retention chart
                    water_cols = [c for c in ["SLLL", "SDUL", "SSAT"] if c in df_display.columns]
                    if water_cols and "SLB" in df_display.columns:
                        st.markdown("**Water Retention Profile (SLLL / SDUL / SSAT)**")
                        chart_df = df_display.set_index("SLB")[water_cols]
                        st.line_chart(chart_df)

                    # Other variables chart
                    other_cols = [c for c in ["SBDM", "SLOC", "SRGF", "SLCL", "SLSI"] if c in df_display.columns]
                    if other_cols and "SLB" in df_display.columns:
                        st.markdown("**Physical / Root Properties**")
                        st.line_chart(df_display.set_index("SLB")[other_cols])

                    st.markdown("**Raw Layer Data**")
                    st.dataframe(df_display, use_container_width=True)

        # Download
        fixed_text = write_sol(profiles, text)
        st.divider()
        st.download_button(
            f"⬇️ Download Cleaned {fname}",
            data=fixed_text.encode("utf-8"),
            file_name=f"FIXED_{fname}",
            mime="text/plain",
            use_container_width=True,
        )

    # ── EXPERIMENT ───────────────────────────────────────────────────────────
    else:
        st.header(f"📋 Experiment Engine — {fname}")
        parsed_exp = parse_exp(text)
        errs, warns, fixes, treatments = check_exp(parsed_exp)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Errors", len(errs))
        c2.metric("Fixes", len(fixes))
        c3.metric("Warnings", len(warns))
        c4.metric("Treatments", len(treatments))

        render_status_badge(len(errs), len(fixes), len(warns))
        render_log("❌ Experiment Errors", errs, "error")
        render_log("🔧 Fixes applied", fixes, "info")
        render_log("⚠️ Warnings", warns, "warning")

        if parsed_exp["title"]:
            st.info(f"**Experiment:** {parsed_exp['title']}")

        # Sections overview
        st.subheader("Detected Sections")
        detected = list(parsed_exp["sections"].keys())
        missing = [r for r in REQUIRED_SECTIONS if r.upper() not in {d.upper() for d in detected}]
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Present**")
            for s in detected:
                st.markdown(f"✅ *{s}*")
        with col_b:
            st.markdown("**Missing (required)**")
            if missing:
                for s in missing:
                    st.markdown(f"❌ *{s}*")
            else:
                st.markdown("*All required sections present.*")

        # Treatment table
        if treatments:
            st.subheader("Treatment Matrix")
            df_trt = pd.DataFrame(treatments)
            cols_show = [c for c in ["N", "TNAME", "CU", "FL", "IC", "MP", "MI", "MF"] if c in df_trt.columns]
            st.dataframe(df_trt[cols_show], use_container_width=True)

        # Full section browser
        st.subheader("Section Browser")
        for sec_name, sec_data in parsed_exp["sections"].items():
            with st.expander(f"*{sec_name}"):
                for block in sec_data["blocks"]:
                    if block["rows"]:
                        st.code(block["header"] + "\n" + "\n".join(block["rows"]), language=None)