"""Streamlit front end for the canonical, read-only DSSAT static checker."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePath

import pandas as pd
import streamlit as st

import dssat_checker as dc


st.set_page_config(page_title="DSSAT Validator", page_icon="🌾", layout="wide")
st.title("🌾 DSSAT File Validator")
st.caption(
    "The web UI and command line use the same read-only validation rules. "
    "Uploaded bytes are processed on the server in a temporary directory and deleted after validation."
)

uploaded = st.file_uploader(
    "Upload a DSSAT input file (.WTH, .SOL, .CUL, .ECO, .SPE, or .??X)",
    type=None,
)

if uploaded is not None:
    raw_name = PurePath(uploaded.name).name
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in raw_name)
    safe_name = safe_name[:120] or "uploaded.dssat"
    payload = uploaded.getvalue()

    with tempfile.TemporaryDirectory(prefix="dssat-check-") as tmp:
        path = Path(tmp) / safe_name
        path.write_bytes(payload)
        parsed = dc.parse_file(path)
        dc.validate_file(parsed)
        findings = sorted(
            parsed.findings,
            key=lambda item: (item.line, -int(item.severity), item.code),
        )

    counts = {
        severity.label(): sum(item.severity == severity for item in findings)
        for severity in dc.Severity
    }
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Errors", counts["error"])
    c2.metric("Warnings", counts["warning"])
    c3.metric("Information", counts["info"])
    c4.metric("File type", parsed.kind)

    if counts["error"]:
        st.error("The file has validation errors and should not be used for a DSSAT run yet.")
    elif counts["warning"]:
        st.warning("No blocking errors were found; review the warnings before simulation.")
    else:
        st.success("No issues were detected by the implemented rules.")

    if findings:
        st.dataframe(pd.DataFrame([
            {
                "severity": finding.severity.label(),
                "code": finding.code,
                "line": finding.line,
                "column": finding.column,
                "message": finding.message,
                "suggestion": finding.suggestion,
            }
            for finding in findings
        ]), use_container_width=True, hide_index=True)

    st.info(
        "Automatic repair is intentionally disabled. Weather gaps, rainfall, duplicate-day choices, "
        "and soil hydraulic corrections require an explicit scientific decision and provenance record."
    )
