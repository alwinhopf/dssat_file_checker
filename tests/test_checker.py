import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dssat_checker as dc


VALID_WTH = """*WEATHER DATA : TEST
@ INSI      LAT     LONG  ELEV   TAV   AMP REFHT WNDHT
  TEST   29.650  -82.350    30  20.0  10.0   2.0   3.0
@DATE  SRAD  TMAX  TMIN  RAIN
24001  15.0  25.0  10.0   0.0
24002  16.0  26.0  11.0   2.0
"""

VALID_SOL = """*SOILS: DSSAT
*TEST000001  TEST PROFILE
@SITE        COUNTRY          LAT     LONG SCS FAMILY
 Test        USA           29.650  -82.350 Loam
@ SLB  SLLL  SDUL  SSAT  SRGF  SSKS  SBDM  SLOC  SLCL  SLSI
    15 0.100 0.250 0.450 1.000 10.00  1.30  1.50  20.0  40.0
    30 0.120 0.270 0.430 0.800  5.00  1.40  1.00  25.0  35.0
"""

VALID_CUL = """*CULTIVARS: TEST
@VAR# VRNAME.......... EXPNO ECO#    P1    P2
 TEST1 Test cultivar       .  ECO1 100.0 200.0
"""

VALID_ECO = """*ECOTYPES: TEST
@ECO# ECONAME.........  TBASE  TOPT
 ECO1 Test ecotype        5.0  28.0
"""

VALID_EXP = """*EXP.DETAILS: TEST0001MZ TEST
*TREATMENTS                        -------------FACTOR LEVELS------------
@N R O C TNAME.................... CU FL SA IC MP MI MF MR MC MT ME MH SM
 1 1 0 0 Base treatment             1  1  0  0  1  0  0  0  0  0  0  1  1
*CULTIVARS
@C CR INGEN CNAME
 1 MZ TEST1 Test cultivar
*FIELDS
@L ID_FIELD WSTA....  ID_SOIL    FLNAME
 1 FIELD1   TEST      TEST000001 Test field
*PLANTING DETAILS
@P PDATE EDATE  PPOP  PLDP PLNAME
 1 24001   -99   8.0   5.0 Planting
*HARVEST DETAILS
@H HDATE HSTG HNAME
 1 24300 GS000 Harvest
*SIMULATION CONTROLS
@N GENERAL NYERS NREPS START SDATE RSEED SNAME
 1 GE      1     1     S  24001  2150 Base
"""


class CheckerTests(unittest.TestCase):
    def write(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def codes(self, findings):
        return {finding.code for finding in findings}

    def test_valid_project_has_no_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "TEST2401.WTH", VALID_WTH)
            self.write(root, "SOIL.SOL", VALID_SOL)
            self.write(root, "MZCER048.CUL", VALID_CUL)
            self.write(root, "MZCER048.ECO", VALID_ECO)
            self.write(root, "TEST0001.MZX", VALID_EXP)
            files, findings = dc.run_check([str(root)])
            errors = [f for f in findings if f.severity == dc.Severity.ERROR]
            self.assertEqual(5, len(files))
            self.assertEqual([], errors, [f"{f.code}: {f.message}" for f in errors])

    def test_weather_logic_errors(self):
        broken = VALID_WTH.replace(
            "24002  16.0  26.0  11.0   2.0",
            "24001  -1.0   5.0  11.0  -2.0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), "BROKEN.WTH", broken)
            parsed = dc.parse_file(path)
            dc.validate_file(parsed)
            codes = self.codes(parsed.findings)
            self.assertIn("WTH011", codes)
            self.assertIn("WTH016", codes)
            self.assertIn("WTH017", codes)
            self.assertIn("WTH018", codes)

    def test_weather_gap_and_bad_date(self):
        content = VALID_WTH.replace("24002", "24367")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), "BROKEN.WTH", content)
            parsed = dc.parse_file(path)
            dc.validate_file(parsed)
            self.assertIn("DAT001", self.codes(parsed.findings))

    def test_soil_water_and_depth_errors(self):
        broken = VALID_SOL.replace(
            "    30 0.120 0.270 0.430 0.800  5.00  1.40  1.00  25.0  35.0",
            "    10 0.300 0.250 0.200 1.200 -5.00  3.40  1.00  80.0  40.0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), "BROKEN.SOL", broken)
            parsed = dc.parse_file(path)
            dc.validate_file(parsed)
            codes = self.codes(parsed.findings)
            self.assertIn("SOL010", codes)
            self.assertIn("SOL011", codes)
            self.assertIn("SOL012", codes)
            self.assertIn("SOL018", codes)

    def test_cross_file_reference_errors(self):
        broken_exp = VALID_EXP.replace("TEST000001", "NOTFOUND01").replace("TEST1 Test cultivar", "BAD01 Bad cultivar")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "TEST2401.WTH", VALID_WTH)
            self.write(root, "SOIL.SOL", VALID_SOL)
            self.write(root, "MZCER048.CUL", VALID_CUL)
            self.write(root, "TEST0001.MZX", broken_exp)
            _, findings = dc.run_check([str(root)])
            codes = self.codes(findings)
            self.assertIn("REF002", codes)
            self.assertIn("REF003", codes)

    def test_cultivar_ecotype_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "MZCER048.CUL", VALID_CUL.replace("ECO1", "ECO9"))
            self.write(root, "MZCER048.ECO", VALID_ECO)
            _, findings = dc.run_check([str(root)])
            self.assertIn("REF004", self.codes(findings))

    def test_json_report_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "TEST2401.WTH", VALID_WTH)
            files, findings = dc.run_check([str(root)])
            report = json.loads(dc.json_report(files, findings))
            self.assertEqual("dssat-check", report["summary"]["tool"])
            self.assertEqual(1, report["summary"]["files_checked"])

    def test_sarif_report_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "TEST2401.WTH", VALID_WTH.replace(" 10.0   0.0", " 30.0   0.0"))
            files, findings = dc.run_check([str(root)])
            report = json.loads(dc.sarif_report(files, findings))
            self.assertEqual("2.1.0", report["version"])
            self.assertEqual("dssat-check", report["runs"][0]["tool"]["driver"]["name"])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "GOOD.WTH", VALID_WTH)
            self.assertEqual(0, dc.main([str(root), "--no-color", "--quiet"]))
            self.write(root, "BAD.WTH", VALID_WTH.replace(" 10.0   0.0", " 30.0   0.0"))
            self.assertEqual(1, dc.main([str(root), "--no-color", "--quiet"]))


if __name__ == "__main__":
    unittest.main()
