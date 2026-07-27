#!/usr/bin/env python3
"""Static validator for DSSAT text input files.

The checker deliberately separates syntax/format errors from agronomic or
cross-file plausibility warnings. It does not modify input files.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable, Sequence


VERSION = "0.1.0"
MISSING = {"", "-99", "-99.0", "-99.00", "-99.000", "NA", "N/A", "NULL", "."}
INPUT_EXTENSIONS = {".WTH", ".SOL", ".CUL", ".ECO", ".SPE"}
EXPERIMENT_EXTENSION = re.compile(r"^\.[A-Z0-9]{2}X$", re.IGNORECASE)
DATE_FIELD = re.compile(r"(?:^|_)(?:DATE|.*DAT|PDATE|EDATE|IDATE|FDATE|HDATE|SDATE)$", re.I)


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    ERROR = 3

    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    path: str
    line: int
    message: str
    suggestion: str | None = None
    column: int | None = None

    def serializable(self) -> dict[str, Any]:
        result = asdict(self)
        result["severity"] = self.severity.label()
        return result


@dataclass
class Row:
    line: int
    raw: str
    values: dict[str, str | None]
    tokens: list[str]


@dataclass
class Table:
    section: str
    header_line: int
    headers: list[str]
    starts: list[int]
    rows: list[Row] = field(default_factory=list)


@dataclass
class ParsedFile:
    path: Path
    kind: str
    lines: list[str]
    tables: list[Table]
    sections: list[tuple[int, str]]
    findings: list[Finding] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_header(value: str) -> str:
    return value.strip().lstrip("@").rstrip(".").upper()


def is_missing(value: str | None) -> bool:
    return value is None or value.strip().upper() in MISSING


def as_float(value: str | None) -> float | None:
    if is_missing(value):
        return None
    try:
        result = float(str(value).replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def dssat_date(value: str | None) -> date | None:
    """Parse DSSAT YYDDD or YYYYDDD dates.

    DSSAT's two-digit year convention is interpreted as 00-49 => 2000-2049 and
    50-99 => 1950-1999, matching Python's common %y convention.
    """
    if is_missing(value):
        return None
    text = str(value).strip()
    if not text.isdigit() or len(text) not in (5, 7):
        return None
    year_text, doy_text = (text[:2], text[2:]) if len(text) == 5 else (text[:4], text[4:])
    year = int(year_text)
    if len(text) == 5:
        year += 2000 if year <= 49 else 1900
    doy = int(doy_text)
    max_doy = 366 if _is_leap(year) else 365
    if not 1 <= doy <= max_doy:
        return None
    return date(year, 1, 1) + timedelta(days=doy - 1)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def detect_kind(path: Path, lines: Sequence[str]) -> str:
    suffix = path.suffix.upper()
    if suffix == ".WTH":
        return "weather"
    if suffix == ".SOL":
        return "soil"
    if suffix in {".CUL", ".ECO", ".SPE"}:
        return {".CUL": "cultivar", ".ECO": "ecotype", ".SPE": "species"}[suffix]
    if EXPERIMENT_EXTENSION.match(suffix):
        return "experiment"
    first_content = next((line.upper() for line in lines if line.strip()), "")
    if first_content.startswith("*WEATHER"):
        return "weather"
    if first_content.startswith("*SOILS"):
        return "soil"
    if first_content.startswith("*EXP.DETAILS"):
        return "experiment"
    if first_content.startswith("*CULTIVAR"):
        return "cultivar"
    if first_content.startswith("*ECOTYPE"):
        return "ecotype"
    if first_content.startswith("*SPECIES"):
        return "species"
    return "generic"


def read_text(path: Path) -> tuple[list[str], list[Finding]]:
    findings: list[Finding] = []
    data = path.read_bytes()
    display = str(path)
    if b"\x00" in data:
        findings.append(Finding(Severity.ERROR, "FMT001", display, 1, "NUL byte found; DSSAT inputs must be plain text.", "Remove binary/NUL characters."))
    if data.startswith(b"\xef\xbb\xbf"):
        findings.append(Finding(Severity.WARNING, "FMT002", display, 1, "UTF-8 byte-order mark found.", "Save as UTF-8 without BOM for maximum DSSAT compatibility."))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
        findings.append(Finding(Severity.WARNING, "FMT003", display, 1, "File is not valid UTF-8 and was decoded as Windows-1252.", "Use plain ASCII or UTF-8 without BOM."))
    if data and not data.endswith((b"\n", b"\r")):
        findings.append(Finding(Severity.WARNING, "FMT004", display, max(1, text.count("\n") + 1), "File has no final newline.", "Add a newline after the final record."))
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if "\t" in line:
            findings.append(Finding(Severity.ERROR, "FMT005", display, number, "Tab character found in a fixed-width DSSAT file.", "Replace tabs with spaces; tab width is parser-dependent.", line.index("\t") + 1))
        if len(line) > 240:
            findings.append(Finding(Severity.WARNING, "FMT006", display, number, f"Line is unusually long ({len(line)} characters).", "Verify that records were not accidentally concatenated."))
        controls = [i for i, ch in enumerate(line) if ord(ch) < 32 and ch not in "\r\n\t"]
        if controls:
            findings.append(Finding(Severity.ERROR, "FMT007", display, number, "Control character found in text record.", "Remove non-printing control characters.", controls[0] + 1))
    if not lines or not any(line.strip() for line in lines):
        findings.append(Finding(Severity.ERROR, "FMT008", display, 1, "File is empty.", "Provide a DSSAT title, headers, and records."))
    return lines, findings


def _tokenize_data(raw: str, headers: Sequence[str], starts: Sequence[int]) -> list[str]:
    """Map whitespace tokens while preserving DSSAT free-text name fields.

    Header labels are often centered rather than placed at the literal start of
    their fixed-width field, so their character offsets are not safe slicing
    boundaries. DSSAT uses explicit missing values for numeric fields, while
    NAME/DESCRIPTION fields may contain spaces. Extra tokens can therefore be
    assigned to the first free-text field without shifting later numeric data.
    """
    tokens = raw.strip().split()
    if headers and len(tokens) > len(headers):
        text_indices = [index for index, header in enumerate(headers) if _looks_textual(header)]
        target = text_indices[0] if text_indices else len(headers) - 1
        consume = 1 + len(tokens) - len(headers)
        tokens = tokens[:target] + [" ".join(tokens[target : target + consume])] + tokens[target + consume :]
    return tokens


def parse_file(path: Path) -> ParsedFile:
    lines, initial = read_text(path)
    kind = detect_kind(path, lines)
    sections: list[tuple[int, str]] = []
    tables: list[Table] = []
    current_section = ""
    current_table: Table | None = None
    seen_content = False

    parsed = ParsedFile(path, kind, lines, tables, sections, initial)
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            continue
        if stripped.startswith("*"):
            current_section = stripped[1:].strip()
            sections.append((number, current_section))
            current_table = None
            seen_content = True
            continue
        if stripped.startswith("@"):
            matches = list(re.finditer(r"\S+", raw))
            header_items = [
                (normalize_header(match.group()), match.start())
                for match in matches
                if match.group() != "@"
            ]
            headers = [item[0] for item in header_items]
            starts = [item[1] for item in header_items]
            if not headers:
                parsed.findings.append(Finding(Severity.ERROR, "STR001", str(path), number, "Header marker '@' has no field names."))
                continue
            duplicates = sorted({h for h in headers if headers.count(h) > 1})
            if duplicates:
                parsed.findings.append(Finding(Severity.ERROR, "STR002", str(path), number, f"Duplicate header field(s): {', '.join(duplicates)}.", "Each column code in a table header must be unique."))
            current_table = Table(current_section, number, headers, starts)
            tables.append(current_table)
            seen_content = True
            continue
        if stripped.startswith("$"):
            # DSSAT control/directive lines are accepted but do not belong to a table.
            current_table = None
            seen_content = True
            continue
        if not seen_content:
            parsed.findings.append(Finding(Severity.ERROR, "STR003", str(path), number, "Data appears before a DSSAT title or section.", "Start the file with a '*' title line."))
        if current_table is None:
            # Narrative lines under GENERAL are valid; data-looking orphan lines are not.
            if re.match(r"^[+\-.]?\d", stripped):
                parsed.findings.append(Finding(Severity.WARNING, "STR004", str(path), number, "Numeric record has no active '@' header.", "Check for a missing or malformed header line."))
            continue
        tokens = _tokenize_data(raw, current_table.headers, current_table.starts)
        values = {header: tokens[i] if i < len(tokens) else None for i, header in enumerate(current_table.headers)}
        current_table.rows.append(Row(number, raw, values, tokens))
        missing_count = len(current_table.headers) - len(tokens)
        if missing_count > 0:
            missing_headers = current_table.headers[-missing_count:]
            # A final descriptive field is often optional; only flag missing non-text columns.
            non_text_missing = [h for h in missing_headers if not _looks_textual(h)]
            if non_text_missing:
                parsed.findings.append(
                    Finding(
                        Severity.WARNING,
                        "STR005",
                        str(path),
                        number,
                        f"Record has {len(tokens)} value(s) for {len(current_table.headers)} columns; missing: {', '.join(non_text_missing)}.",
                        "Use -99 for required unavailable numeric data and preserve column order.",
                    )
                )
    _generic_checks(parsed)
    return parsed


def _looks_textual(header: str) -> bool:
    return any(term in header for term in ("NAME", "DES", "FAMILY", "ADDRESS", "PEOPLE", "NOTES", "SOURCE", "TNAME", "VRNAME"))


def _generic_checks(parsed: ParsedFile) -> None:
    display = str(parsed.path)
    first = next(((i, line.strip()) for i, line in enumerate(parsed.lines, 1) if line.strip() and not line.lstrip().startswith("!")), None)
    if first and not first[1].startswith("*"):
        parsed.findings.append(Finding(Severity.ERROR, "STR006", display, first[0], "First content line is not a DSSAT '*' title.", "Add the appropriate DSSAT title record."))
    if not parsed.tables:
        parsed.findings.append(Finding(Severity.ERROR, "STR007", display, 1, "No '@' table headers found.", "Verify that this is a DSSAT input file and that header markers are intact."))
    for table in parsed.tables:
        for row in table.rows:
            for header, value in row.values.items():
                if value is None or is_missing(value):
                    continue
                if DATE_FIELD.search(header) and header not in {"DAY", "DAYS"}:
                    if not dssat_date(value):
                        parsed.findings.append(Finding(Severity.ERROR, "DAT001", display, row.line, f"{header}='{value}' is not a valid YYDDD or YYYYDDD DSSAT date.", "Use a valid year and day-of-year, including leap-year rules."))


def find_table(parsed: ParsedFile, required: Iterable[str], section_contains: str | None = None) -> list[Table]:
    required_set = {x.upper() for x in required}
    result = []
    for table in parsed.tables:
        if section_contains and section_contains.upper() not in table.section.upper():
            continue
        if required_set.issubset(set(table.headers)):
            result.append(table)
    return result


def _number_finding(
    parsed: ParsedFile,
    row: Row,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    severity: Severity = Severity.ERROR,
    code: str = "NUM001",
    message: str | None = None,
) -> float | None:
    value = row.values.get(field_name)
    if is_missing(value):
        return None
    number = as_float(value)
    if number is None:
        parsed.findings.append(Finding(Severity.ERROR, "NUM002", str(parsed.path), row.line, f"{field_name}='{value}' is not numeric.", "Use a decimal number or DSSAT missing value -99."))
        return None
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        bounds = f"{minimum if minimum is not None else '-∞'} to {maximum if maximum is not None else '∞'}"
        parsed.findings.append(Finding(severity, code, str(parsed.path), row.line, message or f"{field_name}={number:g} is outside the expected range ({bounds}).", "Confirm units and decimal placement."))
    return number


def validate_weather(parsed: ParsedFile) -> None:
    display = str(parsed.path)
    metadata_tables = find_table(parsed, {"INSI", "LAT", "LONG"})
    if not metadata_tables or not any(t.rows for t in metadata_tables):
        parsed.findings.append(Finding(Severity.ERROR, "WTH001", display, 1, "Weather station metadata table (INSI, LAT, LONG) is missing or empty."))
    else:
        row = metadata_tables[0].rows[0]
        station = row.values.get("INSI")
        if is_missing(station):
            parsed.findings.append(Finding(Severity.ERROR, "WTH002", display, row.line, "Weather station identifier INSI is missing."))
        else:
            parsed.metadata["weather_station"] = str(station).upper()
        _number_finding(parsed, row, "LAT", minimum=-90, maximum=90, code="WTH003")
        _number_finding(parsed, row, "LONG", minimum=-180, maximum=180, code="WTH004")
        if "ELEV" in row.values:
            _number_finding(parsed, row, "ELEV", minimum=-500, maximum=9000, severity=Severity.WARNING, code="WTH005")
        if "TAV" in row.values:
            _number_finding(parsed, row, "TAV", minimum=-60, maximum=60, severity=Severity.WARNING, code="WTH006")
        if "AMP" in row.values:
            _number_finding(parsed, row, "AMP", minimum=0, maximum=60, severity=Severity.WARNING, code="WTH007")

    daily_tables = find_table(parsed, {"DATE", "TMAX", "TMIN", "RAIN"})
    if not daily_tables:
        parsed.findings.append(Finding(Severity.ERROR, "WTH008", display, 1, "Daily weather table with DATE, TMAX, TMIN, and RAIN is missing."))
        return
    table = daily_tables[0]
    if "SRAD" not in table.headers:
        parsed.findings.append(Finding(Severity.WARNING, "WTH009", display, table.header_line, "Daily weather header has no SRAD column.", "Most DSSAT crop models require solar radiation."))
    if not table.rows:
        parsed.findings.append(Finding(Severity.ERROR, "WTH010", display, table.header_line, "Daily weather table contains no records."))
        return
    previous: date | None = None
    seen: set[date] = set()
    for row in table.rows:
        parsed_date = dssat_date(row.values.get("DATE"))
        if parsed_date:
            if parsed_date in seen:
                parsed.findings.append(Finding(Severity.ERROR, "WTH011", display, row.line, f"Duplicate weather date {row.values.get('DATE')}.", "Keep one daily record per station and date."))
            if previous and parsed_date < previous:
                parsed.findings.append(Finding(Severity.ERROR, "WTH012", display, row.line, f"Weather date {row.values.get('DATE')} is out of chronological order."))
            if previous and parsed_date > previous + timedelta(days=1):
                missing_days = (parsed_date - previous).days - 1
                parsed.findings.append(Finding(Severity.WARNING, "WTH013", display, row.line, f"Gap of {missing_days} day(s) before weather date {row.values.get('DATE')}.", "Fill or deliberately account for missing daily weather records."))
            seen.add(parsed_date)
            previous = parsed_date
        tmax = _number_finding(parsed, row, "TMAX", minimum=-70, maximum=65, severity=Severity.WARNING, code="WTH014")
        tmin = _number_finding(parsed, row, "TMIN", minimum=-90, maximum=55, severity=Severity.WARNING, code="WTH015")
        rain = _number_finding(parsed, row, "RAIN", minimum=0, maximum=1000, code="WTH016")
        if "SRAD" in row.values:
            _number_finding(parsed, row, "SRAD", minimum=0, maximum=50, severity=Severity.WARNING, code="WTH017")
        if tmax is not None and tmin is not None and tmin > tmax:
            parsed.findings.append(Finding(Severity.ERROR, "WTH018", display, row.line, f"TMIN ({tmin:g}) exceeds TMAX ({tmax:g}).", "Swap or correct the temperature values."))
        if rain is not None and rain > 500:
            parsed.findings.append(Finding(Severity.WARNING, "WTH019", display, row.line, f"Daily rainfall is unusually high ({rain:g} mm).", "Confirm the rainfall unit and decimal placement."))


def _soil_profile_ids(parsed: ParsedFile) -> list[tuple[int, str]]:
    ids = []
    for line_no, section in parsed.sections:
        upper = section.upper()
        if upper.startswith("SOILS"):
            continue
        token = section.split()[0] if section.split() else ""
        if re.fullmatch(r"[A-Z0-9_-]{4,20}", token, re.I):
            ids.append((line_no, token.upper()))
    return ids


def validate_soil(parsed: ParsedFile) -> None:
    display = str(parsed.path)
    profile_ids = _soil_profile_ids(parsed)
    if not profile_ids:
        parsed.findings.append(Finding(Severity.ERROR, "SOL001", display, 1, "No soil profile identifier record was found.", "Each profile should start with '*<soil-id>'."))
    duplicates = sorted({value for _, value in profile_ids if [x for _, x in profile_ids].count(value) > 1})
    for duplicate in duplicates:
        line = next(line for line, value in profile_ids if value == duplicate)
        parsed.findings.append(Finding(Severity.ERROR, "SOL002", display, line, f"Duplicate soil profile identifier '{duplicate}'."))
    parsed.metadata["soil_ids"] = {value for _, value in profile_ids}

    layer_tables = find_table(parsed, {"SLB", "SLLL", "SDUL", "SSAT"})
    if not layer_tables:
        parsed.findings.append(Finding(Severity.ERROR, "SOL003", display, 1, "No soil layer table containing SLB, SLLL, SDUL, and SSAT was found."))
        return
    for table in layer_tables:
        previous_depth: float | None = None
        seen_depths: set[float] = set()
        if not table.rows:
            parsed.findings.append(Finding(Severity.ERROR, "SOL004", display, table.header_line, "Soil layer header has no layer records."))
        for row in table.rows:
            depth = _number_finding(parsed, row, "SLB", minimum=0.01, maximum=1000, code="SOL005")
            lll = _number_finding(parsed, row, "SLLL", minimum=0, maximum=1, code="SOL006")
            dul = _number_finding(parsed, row, "SDUL", minimum=0, maximum=1, code="SOL007")
            sat = _number_finding(parsed, row, "SSAT", minimum=0, maximum=1, code="SOL008")
            if depth is not None:
                if depth in seen_depths:
                    parsed.findings.append(Finding(Severity.ERROR, "SOL009", display, row.line, f"Duplicate soil layer bottom depth SLB={depth:g} cm."))
                if previous_depth is not None and depth <= previous_depth:
                    parsed.findings.append(Finding(Severity.ERROR, "SOL010", display, row.line, "Soil layer depths are not strictly increasing."))
                previous_depth = depth
                seen_depths.add(depth)
            if None not in (lll, dul, sat) and not (lll < dul < sat):
                parsed.findings.append(Finding(Severity.ERROR, "SOL011", display, row.line, f"Soil water limits must satisfy SLLL < SDUL < SSAT; found {lll:g}, {dul:g}, {sat:g}.", "Check volumetric fractions and column order."))
            if "SRGF" in row.values:
                _number_finding(parsed, row, "SRGF", minimum=0, maximum=1, code="SOL012")
            if "SSKS" in row.values:
                _number_finding(parsed, row, "SSKS", minimum=0, maximum=1000, severity=Severity.WARNING, code="SOL013")
            if "SBDM" in row.values:
                _number_finding(parsed, row, "SBDM", minimum=0.5, maximum=2.2, severity=Severity.WARNING, code="SOL014")
            if "SLOC" in row.values:
                _number_finding(parsed, row, "SLOC", minimum=0, maximum=100, code="SOL015")
            clay = _number_finding(parsed, row, "SLCL", minimum=0, maximum=100, code="SOL016") if "SLCL" in row.values else None
            silt = _number_finding(parsed, row, "SLSI", minimum=0, maximum=100, code="SOL017") if "SLSI" in row.values else None
            if clay is not None and silt is not None and clay + silt > 100.0001:
                parsed.findings.append(Finding(Severity.ERROR, "SOL018", display, row.line, f"Clay + silt is {clay + silt:g}%, exceeding 100%.", "Correct texture percentages or column alignment."))


def validate_genotype(parsed: ParsedFile) -> None:
    display = str(parsed.path)
    expected = {"cultivar": {"VAR#"}, "ecotype": {"ECO#"}, "species": set()}[parsed.kind]
    candidate_tables = [t for t in parsed.tables if (not expected or expected.issubset(set(t.headers)))]
    if not candidate_tables:
        parsed.findings.append(Finding(Severity.ERROR, "GEN001", display, 1, f"No expected {parsed.kind} coefficient table was found."))
        return
    ids: set[str] = set()
    ecotype_refs: set[tuple[int, str]] = set()
    for table in candidate_tables:
        if not table.rows:
            parsed.findings.append(Finding(Severity.ERROR, "GEN002", display, table.header_line, "Genotype coefficient header has no records."))
        key = "VAR#" if "VAR#" in table.headers else "ECO#" if "ECO#" in table.headers else table.headers[0]
        for row in table.rows:
            identifier = row.values.get(key)
            if is_missing(identifier):
                parsed.findings.append(Finding(Severity.ERROR, "GEN003", display, row.line, f"Required genotype identifier {key} is missing."))
                continue
            normalized = str(identifier).upper()
            if normalized in ids:
                parsed.findings.append(Finding(Severity.ERROR, "GEN004", display, row.line, f"Duplicate genotype identifier '{identifier}'."))
            ids.add(normalized)
            if parsed.kind == "cultivar" and "ECO#" in row.values and not is_missing(row.values.get("ECO#")):
                ecotype_refs.add((row.line, str(row.values["ECO#"]).upper()))
            for header, value in row.values.items():
                if header in {key, "VRNAME", "ECO#", "EXPNO"} or is_missing(value):
                    continue
                # Coefficient tables are overwhelmingly numeric after identity columns.
                if as_float(value) is None and not _looks_textual(header):
                    parsed.findings.append(Finding(Severity.ERROR, "GEN005", display, row.line, f"Coefficient {header}='{value}' is not numeric.", "Check fixed-width alignment and use -99 for missing coefficients."))
    parsed.metadata["genotype_ids"] = ids
    parsed.metadata["ecotype_refs"] = ecotype_refs


SECTION_BY_FACTOR = {
    "CU": "CULTIVAR",
    "FL": "FIELD",
    "SA": "SOIL ANALYSIS",
    "IC": "INITIAL CONDITION",
    "MP": "PLANTING",
    "MI": "IRRIGATION",
    "MF": "FERTILIZER",
    "MR": "RESIDUE",
    "MC": "CHEMICAL",
    "MT": "TILLAGE",
    "ME": "ENVIRONMENT",
    "MH": "HARVEST",
    "SM": "SIMULATION CONTROL",
}


def validate_experiment(parsed: ParsedFile) -> None:
    display = str(parsed.path)
    if not any(section.upper().startswith("EXP.DETAILS") for _, section in parsed.sections):
        parsed.findings.append(Finding(Severity.ERROR, "EXP001", display, 1, "Experiment file lacks '*EXP.DETAILS:' title."))
    treatments = find_table(parsed, {"N", "TNAME"}, "TREATMENT")
    if not treatments:
        parsed.findings.append(Finding(Severity.ERROR, "EXP002", display, 1, "No TREATMENTS table with N and TNAME was found."))
    else:
        seen: set[str] = set()
        section_ids = _experiment_section_ids(parsed)
        for row in treatments[0].rows:
            treatment = row.values.get("N")
            if is_missing(treatment):
                parsed.findings.append(Finding(Severity.ERROR, "EXP003", display, row.line, "Treatment number N is missing."))
            elif str(treatment) in seen:
                parsed.findings.append(Finding(Severity.ERROR, "EXP004", display, row.line, f"Duplicate treatment number {treatment}."))
            else:
                seen.add(str(treatment))
            for factor, section_hint in SECTION_BY_FACTOR.items():
                value = row.values.get(factor)
                number = as_float(value)
                if number is None or number <= 0:
                    continue
                integer = str(int(number))
                available = section_ids.get(factor, set())
                if available and integer not in available:
                    parsed.findings.append(Finding(Severity.ERROR, "EXP005", display, row.line, f"Treatment references {factor}={integer}, but that level is absent from the {section_hint} section.", "Correct the treatment factor or add the referenced section record."))
                elif not available:
                    parsed.findings.append(Finding(Severity.WARNING, "EXP006", display, row.line, f"Treatment references {factor}={integer}, but no {section_hint} records were detected."))

    planting_dates = _collect_dates(parsed, {"PDATE"})
    harvest_dates = _collect_dates(parsed, {"HDATE"})
    simulation_dates = _collect_dates(parsed, {"SDATE"})
    if planting_dates and harvest_dates:
        earliest_plant = min(value for _, value in planting_dates)
        for line, harvest in harvest_dates:
            if harvest < earliest_plant:
                parsed.findings.append(Finding(Severity.ERROR, "EXP007", display, line, "Harvest date occurs before the earliest planting date."))
    if planting_dates and simulation_dates:
        earliest_plant = min(value for _, value in planting_dates)
        for line, start in simulation_dates:
            if start > earliest_plant:
                parsed.findings.append(Finding(Severity.WARNING, "EXP008", display, line, "Simulation start date occurs after the earliest planting date.", "Confirm START/SDATE settings and treatment mapping."))

    cultivars = find_table(parsed, {"CR", "INGEN"}, "CULTIVAR")
    parsed.metadata["cultivar_refs"] = {
        (row.line, str(row.values["INGEN"]).upper())
        for table in cultivars
        for row in table.rows
        if not is_missing(row.values.get("INGEN"))
    }
    fields = find_table(parsed, {"WSTA", "ID_SOIL"}, "FIELD")
    parsed.metadata["weather_refs"] = {
        (row.line, str(row.values["WSTA"]).upper())
        for table in fields
        for row in table.rows
        if not is_missing(row.values.get("WSTA"))
    }
    parsed.metadata["soil_refs"] = {
        (row.line, str(row.values["ID_SOIL"]).upper())
        for table in fields
        for row in table.rows
        if not is_missing(row.values.get("ID_SOIL"))
    }

    _check_management_amounts(parsed)


def _experiment_section_ids(parsed: ParsedFile) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {key: set() for key in SECTION_BY_FACTOR}
    for table in parsed.tables:
        section_upper = table.section.upper()
        for factor, hint in SECTION_BY_FACTOR.items():
            if hint in section_upper and table.headers:
                key = table.headers[0]
                for row in table.rows:
                    value = as_float(row.values.get(key))
                    if value is not None and value >= 0 and float(value).is_integer():
                        result[factor].add(str(int(value)))
    return result


def _collect_dates(parsed: ParsedFile, names: set[str]) -> list[tuple[int, date]]:
    found: list[tuple[int, date]] = []
    for table in parsed.tables:
        for row in table.rows:
            for name in names & set(row.values):
                value = dssat_date(row.values[name])
                if value:
                    found.append((row.line, value))
    return found


def _check_management_amounts(parsed: ParsedFile) -> None:
    nonnegative = {"IRVAL", "FAMN", "FAMP", "FAMK", "FAMC", "RAMT", "TDEP"}
    for table in parsed.tables:
        for row in table.rows:
            for header in nonnegative & set(row.values):
                _number_finding(parsed, row, header, minimum=0, code="EXP009")
            if "PPOP" in row.values:
                _number_finding(parsed, row, "PPOP", minimum=0, maximum=1000, severity=Severity.WARNING, code="EXP010")
            if "PLDP" in row.values:
                _number_finding(parsed, row, "PLDP", minimum=0, maximum=50, severity=Severity.WARNING, code="EXP011")


def validate_generic(parsed: ParsedFile) -> None:
    parsed.findings.append(Finding(Severity.INFO, "GENERIC001", str(parsed.path), 1, "File type was not recognized; only generic DSSAT structure checks were applied.", "Use a standard DSSAT extension or title for specialized checks."))


def validate_file(parsed: ParsedFile) -> None:
    if parsed.kind == "weather":
        validate_weather(parsed)
    elif parsed.kind == "soil":
        validate_soil(parsed)
    elif parsed.kind in {"cultivar", "ecotype", "species"}:
        validate_genotype(parsed)
    elif parsed.kind == "experiment":
        validate_experiment(parsed)
    else:
        validate_generic(parsed)


def cross_file_checks(files: Sequence[ParsedFile]) -> list[Finding]:
    findings: list[Finding] = []
    stations = {
        value
        for parsed in files
        if parsed.kind == "weather"
        for value in [parsed.metadata.get("weather_station")]
        if value
    }
    soils = set().union(*(parsed.metadata.get("soil_ids", set()) for parsed in files if parsed.kind == "soil"))
    cultivars = set().union(*(parsed.metadata.get("genotype_ids", set()) for parsed in files if parsed.kind == "cultivar"))
    ecotypes = set().union(*(parsed.metadata.get("genotype_ids", set()) for parsed in files if parsed.kind == "ecotype"))

    for parsed in files:
        if parsed.kind == "experiment":
            if stations:
                for line, ref in parsed.metadata.get("weather_refs", set()):
                    # WSTA may include a year suffix while INSI is the station base.
                    if not any(ref == station or ref.startswith(station) or station.startswith(ref) for station in stations):
                        findings.append(Finding(Severity.ERROR, "REF001", str(parsed.path), line, f"Weather station '{ref}' was not found among scanned .WTH files."))
            if soils:
                for line, ref in parsed.metadata.get("soil_refs", set()):
                    if ref not in soils:
                        findings.append(Finding(Severity.ERROR, "REF002", str(parsed.path), line, f"Soil profile '{ref}' was not found among scanned .SOL files."))
            if cultivars:
                for line, ref in parsed.metadata.get("cultivar_refs", set()):
                    if ref not in cultivars:
                        findings.append(Finding(Severity.ERROR, "REF003", str(parsed.path), line, f"Cultivar '{ref}' was not found among scanned .CUL files."))
        if parsed.kind == "cultivar" and ecotypes:
            for line, ref in parsed.metadata.get("ecotype_refs", set()):
                if ref not in ecotypes:
                    findings.append(Finding(Severity.ERROR, "REF004", str(parsed.path), line, f"Ecotype '{ref}' was not found among scanned .ECO files."))
    return findings


def candidate_files(paths: Sequence[str], recursive: bool, excludes: Sequence[str]) -> list[Path]:
    result: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(raw)
        candidates = [path] if path.is_file() else (path.rglob("*") if recursive else path.glob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if any(fnmatch.fnmatch(str(candidate), pattern) or fnmatch.fnmatch(candidate.name, pattern) for pattern in excludes):
                continue
            suffix = candidate.suffix.upper()
            if suffix in INPUT_EXTENSIONS or EXPERIMENT_EXTENSION.match(suffix):
                result.add(candidate.resolve())
    return sorted(result)


def run_check(paths: Sequence[str], recursive: bool = True, excludes: Sequence[str] = ()) -> tuple[list[ParsedFile], list[Finding]]:
    selected = candidate_files(paths, recursive, excludes)
    parsed_files: list[ParsedFile] = []
    for path in selected:
        parsed = parse_file(path)
        validate_file(parsed)
        parsed_files.append(parsed)
    findings = [finding for parsed in parsed_files for finding in parsed.findings]
    findings.extend(cross_file_checks(parsed_files))
    findings.sort(key=lambda f: (f.path.lower(), f.line, -int(f.severity), f.code))
    return parsed_files, findings


def summary(files: Sequence[ParsedFile], findings: Sequence[Finding]) -> dict[str, Any]:
    counts = {severity.label(): sum(1 for item in findings if item.severity == severity) for severity in Severity}
    return {
        "tool": "dssat-check",
        "version": VERSION,
        "files_checked": len(files),
        "file_types": {kind: sum(1 for item in files if item.kind == kind) for kind in sorted({item.kind for item in files})},
        "counts": counts,
    }


def text_report(files: Sequence[ParsedFile], findings: Sequence[Finding], color: bool) -> str:
    info = summary(files, findings)
    chunks: list[str] = []
    for finding in findings:
        location = f"{finding.path}:{finding.line}"
        if finding.column:
            location += f":{finding.column}"
        label = finding.severity.name
        if color:
            palette = {Severity.ERROR: "\033[31m", Severity.WARNING: "\033[33m", Severity.INFO: "\033[36m"}
            label = f"{palette[finding.severity]}{label}\033[0m"
        chunks.append(f"{location}: {label} {finding.code}: {finding.message}")
        if finding.suggestion:
            chunks.append(f"  suggestion: {finding.suggestion}")
    counts = info["counts"]
    chunks.append(
        f"Checked {info['files_checked']} file(s): {counts['error']} error(s), "
        f"{counts['warning']} warning(s), {counts['info']} info message(s)."
    )
    return "\n".join(chunks)


def json_report(files: Sequence[ParsedFile], findings: Sequence[Finding]) -> str:
    return json.dumps({"summary": summary(files, findings), "findings": [item.serializable() for item in findings]}, indent=2)


def sarif_report(files: Sequence[ParsedFile], findings: Sequence[Finding]) -> str:
    rules: dict[str, dict[str, Any]] = {}
    results = []
    for item in findings:
        rules.setdefault(item.code, {"id": item.code, "shortDescription": {"text": item.message.split(".")[0]}})
        result: dict[str, Any] = {
            "ruleId": item.code,
            "level": {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}[item.severity],
            "message": {"text": item.message + (f" Suggestion: {item.suggestion}" if item.suggestion else "")},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": Path(item.path).as_uri()},
                        "region": {"startLine": max(1, item.line), **({"startColumn": item.column} if item.column else {})},
                    }
                }
            ],
        }
        results.append(result)
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "dssat-check", "version": VERSION, "rules": list(rules.values())}},
                "invocations": [{"executionSuccessful": not any(x.severity == Severity.ERROR for x in findings)}],
                "results": results,
                "properties": summary(files, findings),
            }
        ],
    }
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dssat-check",
        description="Statically check DSSAT experiment, weather, soil, and genotype files.",
    )
    parser.add_argument("paths", nargs="+", help="DSSAT input file(s) or directorie(s)")
    parser.add_argument("-f", "--format", choices=("text", "json", "sarif"), default="text", help="report format (default: text)")
    parser.add_argument("-o", "--output", help="write report to this path instead of stdout")
    parser.add_argument("--no-recursive", action="store_true", help="do not recurse into subdirectories")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="exclude matching path/name; repeatable")
    parser.add_argument("--strict", action="store_true", help="return exit status 1 for warnings as well as errors")
    parser.add_argument("--quiet", action="store_true", help="suppress clean text reports (errors and warnings are still shown)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        files, findings = run_check(args.paths, recursive=not args.no_recursive, excludes=args.exclude)
    except (OSError, FileNotFoundError) as exc:
        print(f"dssat-check: {exc}", file=sys.stderr)
        return 2
    if not files:
        print("dssat-check: no recognized DSSAT input files found", file=sys.stderr)
        return 2
    if args.format == "json":
        report = json_report(files, findings)
    elif args.format == "sarif":
        report = sarif_report(files, findings)
    else:
        visible = findings
        if args.quiet:
            visible = [item for item in findings if item.severity >= Severity.WARNING]
        color = not args.no_color and not args.output and sys.stdout.isatty()
        report = text_report(files, visible, color)
    if args.output:
        Path(args.output).write_text(report + "\n", encoding="utf-8")
    elif report:
        print(report)
    has_errors = any(item.severity == Severity.ERROR for item in findings)
    has_warnings = any(item.severity == Severity.WARNING for item in findings)
    return 1 if has_errors or (args.strict and has_warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
