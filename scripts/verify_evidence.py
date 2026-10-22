"""Evidence and provenance verifier for BuildGuard.

Independently verifies the build artifact, build evidence, and SLSA
provenance for consistency.

This script does not claim SLSA certification or any SLSA assurance level.
Malformed or missing inputs are reported as verification failures rather
than being allowed to crash the verifier.
"""

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

DIST_DIR_NAME = "dist"
ARTIFACT_FILENAME = "buildguard-artifact.zip"
EVIDENCE_FILENAME = "build-evidence.json"
PROVENANCE_FILENAME = "provenance.json"
REPORT_FILENAME = "verification-report.json"

EXPECTED_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
EXPECTED_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

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
    """Return the repository root directory."""
    return Path(__file__).resolve().parent.parent


def calculate_sha256(file_path: Path) -> str:
    """Calculate a file's SHA-256 digest."""
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()


def load_json_file(path: Path):
    """Return (data, error_message) when loading a JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as json_file:
            return json.load(json_file), None
    except FileNotFoundError:
        return None, f"File not found: {path}"
    except json.JSONDecodeError as exc:
        return None, f"File is not valid JSON ({path}): {exc}"
    except (OSError, UnicodeError) as exc:
        return None, f"Could not read file ({path}): {exc}"


def normalize_repository_identity(repository: str):
    """Normalize a repository URL or owner/repository identifier.

    Returns a canonical owner/repository string, or None if the
    input cannot be interpreted as a supported repository identity.
    """
    if not isinstance(repository, str):
        return None

    value = repository.strip()
    if not value:
        return None

    # Accept HTTPS, SSH-style Git URLs, and owner/repository identifiers.
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()

        if not host:
            return None

        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]

        parts = [part for part in path.split("/") if part]

        if host not in {"github.com", "www.github.com"}:
            return None

        if len(parts) != 2:
            return None

        owner, repo = parts

    elif value.startswith("git@github.com:"):
        path = value.split(":", 1)[1].strip("/")
        if path.endswith(".git"):
            path = path[:-4]

        parts = [part for part in path.split("/") if part]
        if len(parts) != 2:
            return None

        owner, repo = parts

    else:
        path = value.strip("/")
        if path.endswith(".git"):
            path = path[:-4]

        parts = [part for part in path.split("/") if part]
        if len(parts) != 2:
            return None

        owner, repo = parts

    if not owner or not repo:
        return None

    return f"{owner.lower()}/{repo.lower()}"


class VerificationResult:
    """Accumulate verification outcomes in a predictable order."""

    def __init__(self):
        self.checks = {name: "FAIL" for name in CHECK_NAMES}
        self.details = {
            name: "Not evaluated" for name in CHECK_NAMES
        }

    def record(self, name: str, passed: bool, detail: str) -> None:
        if name not in self.checks:
            raise ValueError(f"Unknown check name: {name}")

        self.checks[name] = "PASS" if passed else "FAIL"
        self.details[name] = detail

    def skip(self, name: str, detail: str) -> None:
        if name not in self.checks:
            raise ValueError(f"Unknown check name: {name}")

        self.checks[name] = "SKIP"
        self.details[name] = detail

    def overall_status(self) -> str:
        return (
            "FAIL"
            if "FAIL" in self.checks.values()
            else "PASS"
        )

    def to_report(self) -> dict:
        return {
            "verification_status": self.overall_status(),
            "checks": dict(self.checks),
            "details": dict(self.details),
        }


def fail_checks(
    result: VerificationResult,
    check_names: tuple,
    reason: str,
) -> None:
    """Mark dependent checks as failed with a clear reason."""
    for name in check_names:
        result.record(name, False, reason)


def verify(dist_dir: Path) -> dict:
    """Verify the artifact, evidence, and provenance in dist_dir."""
    result = VerificationResult()

    # Check 1: artifact exists and can be read.
    artifact_path = dist_dir / ARTIFACT_FILENAME
    artifact_exists = artifact_path.is_file()

    result.record(
        "artifact_exists",
        artifact_exists,
        f"Artifact found at {artifact_path}"
        if artifact_exists
        else f"Artifact not found or is not a regular file: {artifact_path}",
    )

    actual_digest = None
    artifact_read_error = None

    if artifact_exists:
        try:
            actual_digest = calculate_sha256(artifact_path)
        except OSError as exc:
            artifact_read_error = str(exc)
            result.record(
                "artifact_exists",
                False,
                f"Artifact exists but could not be read: {exc}",
            )

    artifact_usable = actual_digest is not None

    # Load build evidence.
    evidence_path = dist_dir / EVIDENCE_FILENAME
    evidence, evidence_error = load_json_file(evidence_path)

    if evidence_error:
        fail_checks(
            result,
            (
                "artifact_filename_match",
                "artifact_digest_match",
                "build_status",
                "tests_status",
            ),
            evidence_error,
        )
        evidence = None

    elif not isinstance(evidence, dict):
        reason = (
            "build-evidence.json must contain a JSON object; "
            f"received {type(evidence).__name__}"
        )
        fail_checks(
            result,
            (
                "artifact_filename_match",
                "artifact_digest_match",
                "build_status",
                "tests_status",
            ),
            reason,
        )
        evidence = None

    else:
        # Check 2: recorded artifact filename.
        recorded_filename = evidence.get("artifact_filename")

        if not artifact_usable:
            result.record(
                "artifact_filename_match",
                False,
                "Cannot verify filename because the artifact is "
                "missing or unreadable",
            )
        elif recorded_filename != artifact_path.name:
            result.record(
                "artifact_filename_match",
                False,
                f"Evidence records artifact_filename='{recorded_filename}', "
                f"but the actual filename is '{artifact_path.name}'",
            )
        else:
            result.record(
                "artifact_filename_match",
                True,
                f"Matches actual artifact filename '{artifact_path.name}'",
            )

        # Check 3: independently calculated artifact digest.
        recorded_digest = evidence.get("artifact_sha256")

        if not artifact_usable:
            detail = (
                f"Cannot verify digest because the artifact could not "
                f"be read: {artifact_read_error}"
                if artifact_read_error
                else "Cannot verify digest because the artifact "
                "is missing or unreadable"
            )
            result.record("artifact_digest_match", False, detail)

        elif (
            not isinstance(recorded_digest, str)
            or recorded_digest != actual_digest
        ):
            result.record(
                "artifact_digest_match",
                False,
                f"Evidence records artifact_sha256='{recorded_digest}', "
                f"but the independently calculated digest is "
                f"'{actual_digest}'",
            )
        else:
            result.record(
                "artifact_digest_match",
                True,
                "Independently calculated artifact digest matches "
                "build-evidence.json",
            )

        # Check 4: build status.
        build_status = evidence.get("build_status")
        result.record(
            "build_status",
            build_status == "success",
            f"build-evidence.json build_status='{build_status}'",
        )

        # Check 5: test status.
        tests_status = evidence.get("tests_status")
        result.record(
            "tests_status",
            tests_status == "passed",
            f"build-evidence.json tests_status='{tests_status}'",
        )

    # Load provenance.
    provenance_path = dist_dir / PROVENANCE_FILENAME
    provenance, provenance_error = load_json_file(provenance_path)

    if provenance_error:
        fail_checks(
            result,
            (
                "provenance_structure",
                "provenance_artifact_match",
                "source_commit_match",
            ),
            provenance_error,
        )
        return result.to_report()

    if not isinstance(provenance, dict):
        reason = (
            "provenance.json must contain a JSON object; "
            f"received {type(provenance).__name__}"
        )
        fail_checks(
            result,
            (
                "provenance_structure",
                "provenance_artifact_match",
                "source_commit_match",
            ),
            reason,
        )
        return result.to_report()

    # Check 6: provenance structure.
    statement_type = provenance.get("_type")
    predicate_type = provenance.get("predicateType")
    subject_list = provenance.get("subject")

    structure_problems = []

    if statement_type != EXPECTED_STATEMENT_TYPE:
        structure_problems.append(
            f"_type is '{statement_type}', expected "
            f"'{EXPECTED_STATEMENT_TYPE}'"
        )

    if predicate_type != EXPECTED_PREDICATE_TYPE:
        structure_problems.append(
            f"predicateType is '{predicate_type}', expected "
            f"'{EXPECTED_PREDICATE_TYPE}'"
        )

    if not isinstance(subject_list, list) or not subject_list:
        structure_problems.append("subject is missing or empty")
    elif not isinstance(subject_list[0], dict):
        structure_problems.append(
            "the first provenance subject must be a JSON object"
        )

    if structure_problems:
        result.record(
            "provenance_structure",
            False,
            "; ".join(structure_problems),
        )
    else:
        result.record(
            "provenance_structure",
            True,
            "provenance._type, predicateType, and subject are present "
            "and have the expected structure",
        )

    # Check 7: provenance subject matches the artifact.
    if not isinstance(subject_list, list) or not subject_list:
        result.record(
            "provenance_artifact_match",
            False,
            "Cannot verify subject: provenance.subject is missing "
            "or empty",
        )

    elif not isinstance(subject_list[0], dict):
        result.record(
            "provenance_artifact_match",
            False,
            "Cannot verify subject: the first subject must be "
            "a JSON object",
        )

    else:
        subject = subject_list[0]
        subject_name = subject.get("name")
        subject_digest_data = subject.get("digest")

        if isinstance(subject_digest_data, dict):
            subject_digest = subject_digest_data.get("sha256")
        else:
            subject_digest = None

        recorded_filename = (
            evidence.get("artifact_filename")
            if isinstance(evidence, dict)
            else None
        )

        problems = []

        if not isinstance(subject_name, str):
            problems.append("provenance subject name is missing or invalid")
        elif subject_name != recorded_filename:
            problems.append(
                f"provenance subject name '{subject_name}' does not match "
                f"build-evidence.json artifact_filename "
                f"'{recorded_filename}'"
            )

        if not artifact_usable:
            problems.append(
                "cannot verify digest because the artifact is "
                "missing or unreadable"
            )
        elif (
            not isinstance(subject_digest, str)
            or subject_digest != actual_digest
        ):
            problems.append(
                f"provenance subject digest '{subject_digest}' does not "
                f"match the independently calculated artifact digest "
                f"'{actual_digest}'"
            )

        if problems:
            result.record(
                "provenance_artifact_match",
                False,
                "; ".join(problems),
            )
        else:
            result.record(
                "provenance_artifact_match",
                True,
                "Provenance subject name and digest match the artifact "
                "and build evidence",
            )

    # Check 8: source repository and commit traceability.
    evidence_repository = (
        evidence.get("repository")
        if isinstance(evidence, dict)
        else None
    )
    evidence_commit_sha = (
        evidence.get("commit_sha")
        if isinstance(evidence, dict)
        else None
    )

    if not evidence_repository or not evidence_commit_sha:
        result.skip(
            "source_commit_match",
            "build-evidence.json has no repository/commit_sha to verify "
            "(expected for local, non-GitHub-Actions builds)",
        )

    elif (
        not isinstance(evidence_repository, str)
        or not isinstance(evidence_commit_sha, str)
    ):
        result.record(
            "source_commit_match",
            False,
            "Repository and commit_sha must both be strings",
        )

    else:
        expected_repository = normalize_repository_identity(
            evidence_repository
        )

        predicate = provenance.get("predicate")
        if not isinstance(predicate, dict):
            predicate = {}

        build_definition = predicate.get("buildDefinition")
        if not isinstance(build_definition, dict):
            build_definition = {}

        dependencies = build_definition.get("resolvedDependencies")
        if not isinstance(dependencies, list):
            dependencies = []

        match_found = False

        for dependency in dependencies:
            if not isinstance(dependency, dict):
                continue

            dependency_uri = dependency.get("uri")
            dependency_digest_data = dependency.get("digest")

            if not isinstance(dependency_uri, str):
                continue

            dependency_repository = normalize_repository_identity(
                dependency_uri
            )

            if isinstance(dependency_digest_data, dict):
                dependency_commit = dependency_digest_data.get("gitCommit")
            else:
                dependency_commit = None

            if (
                expected_repository is not None
                and dependency_repository == expected_repository
                and isinstance(dependency_commit, str)
                and dependency_commit.lower() == evidence_commit_sha.lower()
            ):
                match_found = True
                break

        if match_found:
            result.record(
                "source_commit_match",
                True,
                "Provenance resolvedDependencies contains repository "
                f"'{evidence_repository}' at commit "
                f"'{evidence_commit_sha}'",
            )
        else:
            result.record(
                "source_commit_match",
                False,
                "Provenance resolvedDependencies does not contain "
                f"repository '{evidence_repository}' at commit "
                f"'{evidence_commit_sha}'",
            )

    return result.to_report()


def write_report(report: dict, dist_dir: Path) -> Path:
    """Write the verification report to disk."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    report_path = dist_dir / REPORT_FILENAME

    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")

    return report_path


def main() -> int:
    """Run verification and return a process exit code."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify build artifact, evidence, and provenance."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=get_repository_root() / DIST_DIR_NAME,
        help=(
            "Directory containing the artifact, build evidence, "
            "and provenance. Defaults to BuildGuard's dist directory."
        ),
    )
    args = parser.parse_args()
    dist_dir = args.dist_dir.resolve()

    try:
        report = verify(dist_dir)
        report_path = write_report(report, dist_dir)
    except (OSError, ValueError) as exc:
        print(f"Verification could not complete: {exc}")
        return 1

    print(f"Verification report written: {report_path}")
    print(f"Overall status: {report['verification_status']}")

    for name in CHECK_NAMES:
        print(
            f"  - {name}: {report['checks'][name]} — "
            f"{report['details'][name]}"
        )

    return 0 if report["verification_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
