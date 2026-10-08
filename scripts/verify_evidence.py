"""Evidence and provenance verifier for BuildGuard.

This script independently verifies that the real build artifact, the
raw build evidence (dist/build-evidence.json), and the SLSA
provenance (dist/provenance.json) are mutually consistent — based on
values this script recomputes itself, not values it simply reads and
trusts.

SCOPE — READ BEFORE MODIFYING:
This script does NOT:
  - assign, compute, or claim any SLSA assurance/build level
  - claim any form of "certification"
  - trust build-evidence.json or provenance.json just because they
    exist and parse as JSON

It answers a narrower question: "are the artifact on disk, the raw
build evidence, and the SLSA provenance consistent with each other,
based on facts this script can independently verify?" Assigning an
assurance level, detecting unsupported claims, fault injection, and
independent acceptance are later stages of this project.

WHAT "INDEPENDENT" MEANS HERE:
The artifact's SHA-256 digest is recalculated directly from the bytes
of dist/buildguard-artifact.zip in this script. It is never copied
from build-evidence.json or provenance.json — both of those are
instead checked AGAINST this independently-computed value.

FAIL-SAFE BEHAVIOR:
If a required file is missing, unreadable, or malformed, the
corresponding check (and any check that depends on it) is recorded as
FAIL with a clear reason. Nothing is skipped silently, and a missing
or broken file never causes an exception that hides the failure from
the report; it is captured and reported instead.
"""

import hashlib
import json
import sys
from pathlib import Path

DIST_DIR_NAME = "dist"
ARTIFACT_FILENAME = "buildguard-artifact.zip"
EVIDENCE_FILENAME = "build-evidence.json"
PROVENANCE_FILENAME = "provenance.json"
REPORT_FILENAME = "verification-report.json"

EXPECTED_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
EXPECTED_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

# Fixed, ordered list of checks. Every run's report contains exactly
# these keys, so the report shape never depends on which checks
# happened to run.
CHECK_NAMES = [
    "artifact_exists",
    "artifact_filename_match",
    "artifact_digest_match",
    "build_status",
    "tests_status",
    "provenance_structure",
    "provenance_artifact_match",
    "source_commit_match",
]


def get_repository_root() -> Path:
    """Return the repository root directory.

    This script lives at <repo_root>/scripts/verify_evidence.py, so
    the repository root is two levels up from this file.
    """
    return Path(__file__).resolve().parent.parent


def calculate_sha256(file_path: Path) -> str:
    """Calculate and return the SHA-256 digest of a file, as a hex string."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def load_json_file(path: Path):
    """Load a JSON file, returning (data, error_message).

    On success, error_message is None. On failure (missing file or
    invalid JSON), data is None and error_message explains why —
    verification never silently continues with missing data.
    """
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as json_file:
            return json.load(json_file), None
    except json.JSONDecodeError as exc:
        return None, f"File is not valid JSON ({path}): {exc}"


class VerificationResult:
    """Accumulates check outcomes in a fixed, predictable order."""

    def __init__(self):
        self.checks: dict[str, str] = {name: "FAIL" for name in CHECK_NAMES}
        self.details: dict[str, str] = {
            name: "Not evaluated" for name in CHECK_NAMES
        }

    def record(self, name: str, passed: bool, detail: str) -> None:
        if name not in self.checks:
            raise ValueError(f"Unknown check name: {name}")
        self.checks[name] = "PASS" if passed else "FAIL"
        self.details[name] = detail

    def skip(self, name: str, detail: str) -> None:
        """Mark a check as not applicable (e.g. no source info to check).

        SKIP does not count as a failure on its own, but is distinct
        from PASS: it means the check could not meaningfully run.
        """
        if name not in self.checks:
            raise ValueError(f"Unknown check name: {name}")
        self.checks[name] = "SKIP"
        self.details[name] = detail

    def overall_status(self) -> str:
        return "FAIL" if "FAIL" in self.checks.values() else "PASS"

    def to_report(self) -> dict:
        return {
            "verification_status": self.overall_status(),
            "checks": dict(self.checks),
            "details": dict(self.details),
        }


def verify(dist_dir: Path) -> dict:
    """Run all verification checks against the files in dist_dir.

    Returns the full report dict (same shape written to
    verification-report.json). Never raises for expected failure
    conditions (missing/mismatched files) — those are captured as
    FAIL checks instead.
    """
    result = VerificationResult()

    # --- Check 1: artifact_exists ---------------------------------
    artifact_path = dist_dir / ARTIFACT_FILENAME
    artifact_exists = artifact_path.exists()
    result.record(
        "artifact_exists",
        artifact_exists,
        f"Artifact found at {artifact_path}"
        if artifact_exists
        else f"Artifact not found at {artifact_path}",
    )

    # The independently-calculated digest. This is the value every
    # other digest comparison below is checked against.
    actual_digest = calculate_sha256(artifact_path) if artifact_exists else None

    # --- Load build-evidence.json ----------------------------------
    evidence_path = dist_dir / EVIDENCE_FILENAME
    evidence, evidence_error = load_json_file(evidence_path)

    if evidence is None:
        reason = evidence_error or f"Could not load {evidence_path}"
        for name in (
            "artifact_filename_match",
            "artifact_digest_match",
            "build_status",
            "tests_status",
        ):
            result.record(name, False, reason)
    else:
        # --- Check: artifact_filename_match -------------------------
        recorded_filename = evidence.get("artifact_filename")
        if not artifact_exists:
            result.record(
                "artifact_filename_match",
                False,
                "Cannot verify filename: artifact does not exist on disk",
            )
        elif recorded_filename != artifact_path.name:
            result.record(
                "artifact_filename_match",
                False,
                f"build-evidence.json records artifact_filename="
                f"'{recorded_filename}' but the actual artifact on disk "
                f"is named '{artifact_path.name}'",
            )
        else:
            result.record(
                "artifact_filename_match",
                True,
                f"Matches actual artifact filename '{artifact_path.name}'",
            )

        # --- Check: artifact_digest_match ----------------------------
        recorded_digest = evidence.get("artifact_sha256")
        if not artifact_exists:
            result.record(
                "artifact_digest_match",
                False,
                "Cannot verify digest: artifact does not exist on disk",
            )
        elif recorded_digest != actual_digest:
            result.record(
                "artifact_digest_match",
                False,
                f"build-evidence.json records artifact_sha256="
                f"'{recorded_digest}' but the independently-calculated "
                f"digest of the actual artifact is '{actual_digest}'",
            )
        else:
            result.record(
                "artifact_digest_match",
                True,
                "Independently-calculated artifact digest matches "
                "build-evidence.json",
            )

        # --- Check: build_status --------------------------------------
        build_status = evidence.get("build_status")
        result.record(
            "build_status",
            build_status == "success",
            f"build-evidence.json build_status='{build_status}'",
        )

        # --- Check: tests_status ----------------------------------------
        tests_status = evidence.get("tests_status")
        result.record(
            "tests_status",
            tests_status == "passed",
            f"build-evidence.json tests_status='{tests_status}'",
        )

    # --- Load provenance.json ---------------------------------------
    provenance_path = dist_dir / PROVENANCE_FILENAME
    provenance, provenance_error = load_json_file(provenance_path)

    if provenance is None:
        reason = provenance_error or f"Could not load {provenance_path}"
        for name in (
            "provenance_structure",
            "provenance_artifact_match",
            "source_commit_match",
        ):
            result.record(name, False, reason)
        return result.to_report()

    # --- Check: provenance_structure ---------------------------------
    statement_type = provenance.get("_type")
    predicate_type = provenance.get("predicateType")
    subject_list = provenance.get("subject")
    has_subject = isinstance(subject_list, list) and len(subject_list) > 0

    structure_problems = []
    if statement_type != EXPECTED_STATEMENT_TYPE:
        structure_problems.append(
            f"_type is '{statement_type}', expected '{EXPECTED_STATEMENT_TYPE}'"
        )
    if predicate_type != EXPECTED_PREDICATE_TYPE:
        structure_problems.append(
            f"predicateType is '{predicate_type}', expected "
            f"'{EXPECTED_PREDICATE_TYPE}'"
        )
    if not has_subject:
        structure_problems.append("subject is missing or empty")

    if structure_problems:
        result.record(
            "provenance_structure", False, "; ".join(structure_problems)
        )
    else:
        result.record(
            "provenance_structure",
            True,
            "provenance._type, predicateType, and subject are all present "
            "and correct",
        )

    # --- Check: provenance_artifact_match -----------------------------
    if not has_subject:
        result.record(
            "provenance_artifact_match",
            False,
            "Cannot verify subject: provenance.subject is missing or empty",
        )
    else:
        subject = subject_list[0]
        subject_name = subject.get("name")
        subject_digest = (subject.get("digest") or {}).get("sha256")

        recorded_filename = evidence.get("artifact_filename") if evidence else None
        problems = []

        if subject_name != recorded_filename:
            problems.append(
                f"provenance subject name '{subject_name}' does not match "
                f"build-evidence.json artifact_filename '{recorded_filename}'"
            )
        if not artifact_exists:
            problems.append("cannot verify digest: artifact does not exist on disk")
        elif subject_digest != actual_digest:
            problems.append(
                f"provenance subject digest '{subject_digest}' does not "
                f"match the independently-calculated artifact digest "
                f"'{actual_digest}'"
            )

        if problems:
            result.record("provenance_artifact_match", False, "; ".join(problems))
        else:
            result.record(
                "provenance_artifact_match",
                True,
                "provenance subject name and digest both match the actual "
                "artifact",
            )

    # --- Check: source_commit_match ------------------------------------
    evidence_repository = evidence.get("repository") if evidence else None
    evidence_commit_sha = evidence.get("commit_sha") if evidence else None

    if not evidence_repository or not evidence_commit_sha:
        result.skip(
            "source_commit_match",
            "build-evidence.json has no repository/commit_sha to verify "
            "(expected for local, non-GitHub-Actions builds)",
        )
    else:
        resolved_dependencies = (
            provenance.get("predicate", {})
            .get("buildDefinition", {})
            .get("resolvedDependencies", [])
        )

        match_found = False
        for dependency in resolved_dependencies:
            dependency_uri = dependency.get("uri", "")
            dependency_commit = (dependency.get("digest") or {}).get("gitCommit")
            if (
                evidence_repository in dependency_uri
                and dependency_commit == evidence_commit_sha
            ):
                match_found = True
                break

        if match_found:
            result.record(
                "source_commit_match",
                True,
                f"provenance resolvedDependencies contains repository "
                f"'{evidence_repository}' at commit '{evidence_commit_sha}'",
            )
        else:
            result.record(
                "source_commit_match",
                False,
                f"provenance resolvedDependencies does not contain "
                f"repository '{evidence_repository}' at commit "
                f"'{evidence_commit_sha}' (evidence.json says this is the "
                f"source, but provenance does not confirm it)",
            )

    return result.to_report()


def write_report(report: dict, dist_dir: Path) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    report_path = dist_dir / REPORT_FILENAME
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")
    return report_path


def main() -> int:
    repo_root = get_repository_root()
    dist_dir = repo_root / DIST_DIR_NAME

    report = verify(dist_dir)
    report_path = write_report(report, dist_dir)

    print(f"Verification report written: {report_path}")
    print(f"Overall status: {report['verification_status']}")
    for name in CHECK_NAMES:
        print(f"  - {name}: {report['checks'][name]} — {report['details'][name]}")

    return 0 if report["verification_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
