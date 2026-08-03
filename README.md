# DSSAT File Checker

[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A read-only validator for DSSAT crop-model input files. The dependency-free CLI
and Streamlit UI use the same rule engine, date convention, ranges, and syntax.

---

## Features

- **Dual Modes**:
  - **CLI / Library**: Zero third-party dependencies. Fast static validation, exit codes, JSON and SARIF (GitHub Code Scanning) output formats.
  - **Interactive Web App**: Drag-and-drop server-side validation with the same findings as the CLI.
- **Supported File Formats**:
  - Weather files (`.WTH`)
  - Soil profile files (`.SOL`)
  - Cultivar, Ecotype, and Species coefficient files (`.CUL`, `.ECO`, `.SPE`)
  - Experiment / FileX files (`.<crop-code>X`, e.g., `.MZX`, `.SBX`, `.HMX`, `.WHX`, `.SQX`)
- **Validation Engine**:
  - Encoded text, control characters, tabs, record lengths, header structures (`*` titles, `@` headers).
  - Physical plausibility checks ($T_{min} \le T_{max}$, soil water bounds $SLLL < SDUL < SSAT$, population ranges, fertilizer/irrigation bounds).
  - Date math and leap year validation (`YYDDD` / `YYYYDDD`).
  - Cross-file reference validation (experiment references to weather stations, soil profiles, and cultivar/ecotype keys).
- **No implicit repair**: the checker never invents rainfall, chooses among duplicate
  days, fills weather gaps, changes soil hydraulics, or rewrites comments/provenance.
  Those operations require an explicit scientific method and an auditable manifest.

---

## Requirements & Installation

### Option 1: Standalone CLI (No External Dependencies)
Requires **Python 3.8+** with standard library only.
```bash
git clone https://github.com/alwinhopf/dssat_file_checker.git
cd dssat_file_checker
```

### Option 2: Web GUI (With Streamlit & Pandas)
Install optional GUI dependencies:
```bash
pip install -e .[gui]
# OR directly
pip install streamlit pandas numpy
```

---

## Quick Start

### 1. Command Line Interface (CLI)

Check a single file or an entire DSSAT project folder:
```bash
python dssat_checker.py /path/to/dssat/project
```

Generate machine-readable JSON report:
```bash
python dssat_checker.py /path/to/project --format json --output report.json
```

Generate SARIF report for GitHub Code Scanning / CI pipelines:
```bash
python dssat_checker.py . --format sarif --output dssat-check.sarif
```

Treat warnings as errors (strict mode):
```bash
python dssat_checker.py . --strict
```

If installed via `pip`:
```bash
dssat-check /path/to/project
```

### 2. Streamlit Web Dashboard

Launch the interactive web UI in your browser:
```bash
streamlit run app.py
```
Uploads are sent to the Streamlit server, written under a sanitized name in a
temporary directory, validated, and deleted when the request finishes. Do not
deploy the UI where this server-side processing violates your data policy.

---

## Rule Error Codes (CLI Engine)

Findings produced by the CLI engine use stable codes for easy filtering:
- `DAT001`: Invalid DSSAT dates (YYDDD uses the shared `<80` century pivot).
- `WTH001` - `WTH020`: Weather station header, range, and physical plausibility checks.
- `SOL001` - `SOL018`: Soil profile depth monotonicity, water retention ($SLLL < SDUL < SSAT$), and texture bounds.
- `REF001` - `REF004`: Cross-file key references (Experiment $\rightarrow$ Weather / Soil / Cultivar / Ecotype).
- `EXP001` - `EXP013`: FileX section headers, treatment numbers, and management levels.

---

## Running Unit Tests

Run the test suite using Python's built-in test runner:
```bash
python -m unittest discover -s tests -v
```

---

## License

This project is licensed under the [MIT License](LICENSE).
