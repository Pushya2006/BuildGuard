"""Assurance evaluator for BuildGuard.

Reads dist/verification-report.json (produced by scripts/verify_evidence.py)
and evaluates six requirements (R1-R6) against the checks recorded there,
then produces a final ACCEPT / REVISE / STOP decision.

SCOPE — READ BEFORE MODIFYING:
This script does NOT:
  - assign, compute, or claim any SLSA level
  - claim any form of certification
  - re-verify the artifact, evidence, or provenance itself; it only
    interprets the results the verifier already recorded
  - modify the verifier or its report

REQUIREMENTS:
  R1  Artifact integrity        (mandatory, gating)
  R2  Build evidence integrity  (mandatory)
  R3  Provenance integrity      (mandatory, gating)
  R4  Test evidence             (mandatory)
  R5  Source traceability       (mandatory)
  R6  Tamper detection          (experimental, never affects the decision)

DECISION RULES:
  STOP    if R1 or R3 is UNSUPPORTED.
  ACCEPT  if R1 and R3 are SUPPORTED and R2, R4, R5 are all SUPPORTED.
  REVISE  if R1 and R3 are SUPPORTED but any of R2, R4, R5 is UNSUPPORTED.
  R6 is reported for information only and never changes the decision.

STRICTNESS:
A requirement is SUPPORTED only when every check it depends on is
exactly the string "PASS". FAIL, SKIP, a missing check, or any other
value is treated as not-PASS. In particular, SKIP is never silently
treated as PASS.

FAIL-SAFE BEHAVIOR:
A missing or malformed verification report means nothing can be
established, so every requirement is UNSUPPORTED and the decision is
STOP. An assurance report is still written so the failure is visible.

R6 AND TAMPER-TEST RESULTS:
R6 is UNSUPPORTED unless an explicit tamper-test result is supplied.
This script never invents or assumes one. A result is accepted only
from the optional file dist/tamper-test-report.json, and only if it is
valid JSON containing "tamper_detected": true (a real JSON boolean).
Anything else (no file, malformed file, false, wrong type) leaves R6
UNSUPPORTED.
"""

import json
import sys
from pathlib import Path

DIST_DIR_NAME = "dist"
VERIFICATION_REPORT_FILENAME = "verification-report.json"
TAMPER_TEST_REPORT_FILENAME = "tamper-test-report.json"
ASSURANCE_REPORT_FILENAME = "assurance-report.json"

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"

ACCEPT = "ACCEPT"
REVISE = "REVISE"
STOP = "STOP"

# requirement id -> (name, mandatory?, verifier checks it depends on)
REQUIREMENTS = {
    "R1": ("Artifact integrity", True, ["artifact_digest_match"]),
    "R2": (
        "Build evidence integrity",
        True,
        [
            "artifact_filename_match",
            "artifact_digest_match",
            "build_status",
            "tests_status",
        ],
    ),
    "R3": (
        "Provenance integrity",
        True,
        ["provenance_structure", "provenance_artifact_match"],
    ),
    "R4": ("Test evidence", True, ["tests_status"]),
    "R5": ("Source traceability", True, ["source_commit_match"]),
}

R6_NAME = "Tamper detection"

DISCLAIMER = (
    "This report interprets verification results only. It does not assign "
    "a SLSA level and is not a certification."
)


def get_repository_root() -> Path:
    """Return the repository root (two levels above this file)."""
    return Path(__file__).resolve().parent.parent


def load_json_file(path: Path):
    """Load a JSON file, returning (data, error_message)."""
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as json_file:
            return json.load(json_file), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"File is not valid JSON ({path}): {exc}"


def extract_checks(verification_report):
    """Return (checks_dict, error_message) from a loaded verification report."""
    if not isinstance(verification_report, dict):
        return None, "verification report is not a JSON object"
    checks = verification_report.get("checks")
    if not isinstance(checks, dict):
        return None, "verification report has no 'checks' object"
    return checks, None


def evaluate_requirement(req_id: str, checks: dict) -> dict:
    """Evaluate one of R1-R5 strictly against the verifier's checks."""
    name, mandatory, required_checks = REQUIREMENTS[req_id]

    observed = {check: checks.get(check, "MISSING") for check in required_checks}
    not_passing = {c: v for c, v in observed.items() if v != "PASS"}

    if not not_passing:
        status = SUPPORTED
        explanation = "All required checks are PASS: " + ", ".join(required_checks) + "."
    else:
        status = UNSUPPORTED
        problems = ", ".join(f"{c}={v}" for c, v in not_passing.items())
        explanation = f"Required checks not PASS: {problems}."
        if observed.get("source_commit_match") == "SKIP":
            explanation += (
                " A SKIP is not treated as PASS: no source repository/commit "
                "was available to verify."
            )

    return {
        "id": req_id,
        "name": name,
        "mandatory": mandatory,
        "status": status,
        "checks_used": observed,
        "explanation": explanation,
    }


def evaluate_tamper_requirement(tamper_result) -> dict:
    """Evaluate R6 (experimental). UNSUPPORTED unless explicitly evidenced.

    tamper_result is the parsed content of an explicit tamper-test
    result, or None if none was provided.
    """
    base = {"id": "R6", "name": R6_NAME, "mandatory": False}

    if tamper_result is None:
        return {
            **base,
            "status": UNSUPPORTED,
            "checks_used": {},
            "explanation": (
                "No explicit tamper-test result was provided. A normal "
                "verification run does not demonstrate tamper detection."
            ),
        }

    detected = (
        tamper_result.get("tamper_detected")
        if isinstance(tamper_result, dict)
        else None
    )
    if detected is True:
        return {
            **base,
            "status": SUPPORTED,
            "checks_used": {"tamper_detected": True},
            "explanation": (
                "An explicit tamper-test result reports that the tampering "
                "was detected."
            ),
        }

    return {
        **base,
        "status": UNSUPPORTED,
        "checks_used": {"tamper_detected": detected},
        "explanation": (
            "A tamper-test result was provided but does not show "
            "'tamper_detected': true, so tamper detection is not supported."
        ),
    }


def decide(requirements: dict) -> tuple[str, str]:
    """Apply the decision rules. R6 is deliberately never consulted."""
    r1 = requirements["R1"]["status"] == SUPPORTED
    r3 = requirements["R3"]["status"] == SUPPORTED

    if not (r1 and r3):
        failed = [rid for rid, ok in (("R1", r1), ("R3", r3)) if not ok]
        return STOP, (
            f"Gating requirement(s) unsupported: {', '.join(failed)}. "
            "Artifact integrity and provenance integrity are both required "
            "to proceed."
        )

    lacking = [
        rid for rid in ("R2", "R4", "R5") if requirements[rid]["status"] != SUPPORTED
    ]
    if lacking:
        return REVISE, (
            "R1 and R3 are supported, but mandatory requirement(s) "
            f"unsupported: {', '.join(lacking)}."
        )

    return ACCEPT, "R1, R2, R3, R4 and R5 are all supported."


def build_report(requirements: dict, decision: str, reason: str, notes=None) -> dict:
    ordered = [requirements[rid] for rid in ("R1", "R2", "R3", "R4", "R5", "R6")]
    report = {
        "final_decision": decision,
        "decision_reason": reason,
        "requirements": ordered,
        "disclaimer": DISCLAIMER,
    }
    if notes:
        report["notes"] = notes
    return report


def evaluate(verification_report, tamper_result=None) -> dict:
    """Evaluate an already-loaded verification report. Returns the report dict.

    If verification_report is malformed, every requirement is
    UNSUPPORTED and the decision is STOP.
    """
    checks, error = extract_checks(verification_report)

    if checks is None:
        checks = {}
        notes = [f"Verification report unusable: {error}. Failing safe."]
    else:
        notes = None

    requirements = {rid: evaluate_requirement(rid, checks) for rid in REQUIREMENTS}
    requirements["R6"] = evaluate_tamper_requirement(tamper_result)

    decision, reason = decide(requirements)
    return build_report(requirements, decision, reason, notes)


def load_optional_tamper_result(dist_dir: Path):
    """Load an explicit tamper-test result if one exists, else None.

    A present-but-malformed file yields a dict without
    'tamper_detected': true, which keeps R6 UNSUPPORTED.
    """
    path = dist_dir / TAMPER_TEST_REPORT_FILENAME
    if not path.exists():
        return None
    data, _error = load_json_file(path)
    return data if data is not None else {}


def write_report(report: dict, dist_dir: Path) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    path = dist_dir / ASSURANCE_REPORT_FILENAME
    with open(path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")
    return path


def run(dist_dir: Path) -> int:
    """Evaluate dist_dir's verification report, write the report, return exit code."""
    verification_report, load_error = load_json_file(
        dist_dir / VERIFICATION_REPORT_FILENAME
    )

    if verification_report is None:
        report = evaluate(None)
        report["notes"] = [f"Verification report unusable: {load_error}. Failing safe."]
    else:
        report = evaluate(verification_report, load_optional_tamper_result(dist_dir))

    report_path = write_report(report, dist_dir)

    print(f"Assurance report written: {report_path}")
    for requirement in report["requirements"]:
        print(f"  - {requirement['id']} {requirement['name']}: {requirement['status']}")
    print(f"Final decision: {report['final_decision']}")
    print(f"Reason: {report['decision_reason']}")

    return 0 if report["final_decision"] == ACCEPT else 1


def main() -> int:
    return run(get_repository_root() / DIST_DIR_NAME)


if __name__ == "__main__":
    sys.exit(main())
