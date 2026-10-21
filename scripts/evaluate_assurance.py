"""Assurance evaluator for BuildGuard.

Re-verifies build inputs using scripts/verify_evidence.py, then evaluates
six assurance requirements (R1-R6) and writes an assurance report.

SCOPE:
- Does not assign or claim any SLSA level or certification.
- Uses the verifier's fresh results rather than trusting a saved report.
- Does not modify the verifier or its report.
- Fails safely if fresh verification is unavailable or malformed.

REQUIREMENTS:
    R1  Artifact integrity        (mandatory, gating)
    R2  Build evidence integrity  (mandatory)
    R3  Provenance integrity      (mandatory, gating)
    R4  Test evidence             (mandatory)
    R5  Source traceability       (mandatory)
    R6  Tamper detection          (experimental, informational)

DECISION RULES:
    STOP    if R1 or R3 is UNSUPPORTED.
    ACCEPT  if R1, R2, R3, R4 and R5 are SUPPORTED and fresh verification
            reports PASS.
    REVISE  if R1 and R3 are SUPPORTED but R2, R4 or R5 is UNSUPPORTED.
    R6 never changes the decision.

STRICTNESS:
A requirement is SUPPORTED only when every required check is exactly
the string "PASS". FAIL, SKIP, a missing check, or any other value is
treated as not-PASS.

FAIL-SAFE BEHAVIOR:
If fresh verification fails unexpectedly or returns an unusable result,
the evaluator does not accept the build. An assurance report is still
written whenever possible.

R6:
R6 is SUPPORTED only when dist/tamper-test-report.json contains valid
JSON with "tamper_detected": true as a real JSON boolean.
"""

import json
import sys
from pathlib import Path

if __package__:
    from scripts import verify_evidence
else:
    import verify_evidence


DIST_DIR_NAME = "dist"
VERIFICATION_REPORT_FILENAME = "verification-report.json"
TAMPER_TEST_REPORT_FILENAME = "tamper-test-report.json"
ASSURANCE_REPORT_FILENAME = "assurance-report.json"

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"

ACCEPT = "ACCEPT"
REVISE = "REVISE"
STOP = "STOP"

# Requirement ID -> (name, mandatory?, verifier checks it depends on)
REQUIREMENTS = {
    "R1": (
        "Artifact integrity",
        True,
        ["artifact_digest_match"],
    ),
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
        [
            "provenance_structure",
            "provenance_artifact_match",
        ],
    ),
    "R4": (
        "Test evidence",
        True,
        ["tests_status"],
    ),
    "R5": (
        "Source traceability",
        True,
        ["source_commit_match"],
    ),
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
    """Return (checks_dict, error_message) from a verification report."""
    if not isinstance(verification_report, dict):
        return None, "verification report is not a JSON object"

    checks = verification_report.get("checks")
    if not isinstance(checks, dict):
        return None, "verification report has no 'checks' object"

    return checks, None


def evaluate_requirement(req_id: str, checks: dict) -> dict:
    """Evaluate one of R1-R5 strictly against the verifier's checks."""
    name, mandatory, required_checks = REQUIREMENTS[req_id]

    observed = {
        check: checks.get(check, "MISSING")
        for check in required_checks
    }
    not_passing = {
        check: value
        for check, value in observed.items()
        if value != "PASS"
    }

    if not not_passing:
        status = SUPPORTED
        explanation = (
            "All required checks are PASS: "
            + ", ".join(required_checks)
            + "."
        )
    else:
        status = UNSUPPORTED
        problems = ", ".join(
            f"{check}={value}"
            for check, value in not_passing.items()
        )
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
    """Evaluate R6, which is experimental and never gates the decision.

    tamper_result is the parsed content of an explicit tamper-test result,
    or None if no result was supplied.
    """
    base = {
        "id": "R6",
        "name": R6_NAME,
        "mandatory": False,
    }

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
        failed = [
            req_id
            for req_id, ok in (("R1", r1), ("R3", r3))
            if not ok
        ]
        return STOP, (
            f"Gating requirement(s) unsupported: {', '.join(failed)}. "
            "Artifact integrity and provenance integrity are both required "
            "to proceed."
        )

    lacking = [
        req_id
        for req_id in ("R2", "R4", "R5")
        if requirements[req_id]["status"] != SUPPORTED
    ]

    if lacking:
        return REVISE, (
            "R1 and R3 are supported, but mandatory requirement(s) "
            f"unsupported: {', '.join(lacking)}."
        )

    return ACCEPT, "R1, R2, R3, R4 and R5 are all supported."


def build_report(
    requirements: dict,
    decision: str,
    reason: str,
    notes=None,
) -> dict:
    """Build the assurance report in a stable requirement order."""
    ordered = [
        requirements[req_id]
        for req_id in ("R1", "R2", "R3", "R4", "R5", "R6")
    ]

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
    """Evaluate a verification report and return an assurance report.

    If the verification report is malformed, every requirement is
    UNSUPPORTED and the decision is STOP.
    """
    checks, error = extract_checks(verification_report)

    if checks is None:
        checks = {}
        notes = [
            f"Verification report unusable: {error}. Failing safe."
        ]
    else:
        notes = None

    requirements = {
        req_id: evaluate_requirement(req_id, checks)
        for req_id in REQUIREMENTS
    }
    requirements["R6"] = evaluate_tamper_requirement(tamper_result)

    decision, reason = decide(requirements)

    return build_report(
        requirements,
        decision,
        reason,
        notes,
    )


def load_optional_tamper_result(dist_dir: Path):
    """Load an explicit tamper-test result if one exists, else None.

    A present-but-malformed file yields a value that leaves R6
    UNSUPPORTED.
    """
    path = dist_dir / TAMPER_TEST_REPORT_FILENAME

    if not path.exists():
        return None

    data, _error = load_json_file(path)
    return data if data is not None else {}


def write_report(report: dict, dist_dir: Path) -> Path:
    """Write the assurance report and return its path."""
    dist_dir.mkdir(parents=True, exist_ok=True)

    path = dist_dir / ASSURANCE_REPORT_FILENAME

    with open(path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")

    return path


def run(dist_dir: Path) -> int:
    """Freshly verify build inputs, evaluate assurance, and return an exit code."""
    verification_error = None

    try:
        verification_report = verify_evidence.verify(dist_dir)
    except Exception as exc:
        verification_report = None
        verification_error = (
            f"Fresh verification failed unexpectedly: {exc}"
        )

    if verification_report is None:
        report = evaluate(None)
        report["notes"] = [
            verification_error
            or "Fresh verification returned no report. Failing safe."
        ]

    elif not isinstance(verification_report, dict):
        report = evaluate(None)
        report["notes"] = [
            "Fresh verifier returned an invalid report. Failing safe."
        ]

    else:
        tamper_result = load_optional_tamper_result(dist_dir)

        report = evaluate(
            verification_report,
            tamper_result,
        )

        verification_status = verification_report.get(
            "verification_status"
        )

        # A failed verification must never produce ACCEPT.
        # Preserve the original STOP/REVISE rules when the individual
        # checks explain the failure. If the checks would otherwise
        # produce ACCEPT, fail safely because the overall status disagrees.
        if (
            verification_status != "PASS"
            and report["final_decision"] == ACCEPT
        ):
            report = evaluate(
                None,
                tamper_result=tamper_result,
            )
            report["notes"] = [
                "Fresh verification status was not PASS, but its checks "
                "would otherwise allow ACCEPT. Failing safe."
            ]

        elif verification_status != "PASS":
            checks = verification_report.get("checks", {})
            details = verification_report.get("details", {})

            if not isinstance(checks, dict):
                checks = {}

            if not isinstance(details, dict):
                details = {}

            failed_checks = [
                f"{name}: {detail}"
                for name, detail in details.items()
                if checks.get(name) != "PASS"
            ]

            report["notes"] = [
                "Fresh verification did not pass. "
                + (
                    "; ".join(failed_checks)
                    if failed_checks
                    else (
                        "Review the verification checks before "
                        "proceeding."
                    )
                )
            ]

    report_path = write_report(report, dist_dir)

    print(f"Assurance report written: {report_path}")

    for requirement in report["requirements"]:
        print(
            f"  - {requirement['id']} "
            f"{requirement['name']}: {requirement['status']}"
        )

    print(f"Final decision: {report['final_decision']}")
    print(f"Reason: {report['decision_reason']}")

    return 0 if report["final_decision"] == ACCEPT else 1


def main() -> int:
    """Run assurance evaluation against the repository's dist directory."""
    return run(get_repository_root() / DIST_DIR_NAME)


if __name__ == "__main__":
    sys.exit(main())
