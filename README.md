# DSSAT File Checker & Repair Tool

[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A unified validation, diagnostics, and auto-healing toolkit for DSSAT crop-model input files. It features both a **dependency-free Command Line Interface (CLI)** for automated pipeline checks and an **interactive Streamlit Web Dashboard** for visual diagnostics, data plotting, and automatic file repair.

---

## Features

- **Dual Modes**:
  - **CLI / Library**: Zero third-party dependencies. Fast static validation, exit codes, JSON and SARIF (GitHub Code Scanning) output formats.
  - **Interactive Web App**: Drag-and-drop browser UI with interactive plots, issue logs, and downloadable auto-healed DSSAT files.
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
- **Auto-Healing & Repair (Web UI)**:
  - **Weather**: Interpolates missing weather variables, heals date gaps and duplicates, swaps inverted $T_{max}/T_{min}$, and recalculates $T_{av}$ and $AMP$ header metadata.
  - **Soil**: Auto-swaps inverted $SLLL / SDUL$ layers, validates depth monotonicity, and re-exports clean `.SOL` files.
  - **Experiments**: Parses treatments and section matrices.

---

## Requirements & Installation

### Option 1: Standalone CLI (No External Dependencies)
Requires **Python 3.8+** with standard library only.
```bash
git clone https://github.com/alwin/dssat_file_checker.git
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
Or view the file validator directly in your web browser, upload `.WTH`, `.SOL`, or Experiment files, inspect warnings/errors, view time-series and profile charts, and download auto-healed output files.

---

## Rule Error Codes (CLI Engine)

Findings produced by the CLI engine use stable codes for easy filtering:
- `DAT001` - `DAT003`: Invalid or out-of-order DSSAT dates.
- `WTH001` - `WTH020`: Weather station header, range, and physical plausibility checks.
- `SOL001` - `SOL020`: Soil profile depth monotonicity, water retention ($SLLL < SDUL < SSAT$), and texture bounds.
- `REF001` - `REF005`: Cross-file key references (Experiment $\rightarrow$ Weather / Soil / Cultivar / Ecotype).
- `EXP001` - `EXP015`: FileX section headers, treatment numbers, and management levels.

---

## Running Unit Tests

Run the test suite using Python's built-in test runner:
```bash
python -m unittest discover -s tests -v
```

---

## License

This project is licensed under the [MIT License](LICENSE).
