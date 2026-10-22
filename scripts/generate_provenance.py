
"""SLSA provenance generator for BuildGuard.

Reads build evidence and a build artifact, verifies the artifact's
SHA-256 digest, and generates an in-toto Statement using the SLSA
Provenance v1 predicate type.

This script generates an unsigned provenance document. It does not
provide cryptographic signing, independent attestation, or certify
any SLSA assurance level.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

DIST_DIR_NAME = "dist"
EVIDENCE_FILENAME = "build-evidence.json"
PROVENANCE_FILENAME = "provenance.json"

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

# Describes the build definition; it does not assert an assurance level.
BUILD_TYPE = "https://github.com/actions/workflows@v1"


def get_repository_root() -> Path:
    """Return the BuildGuard repository root."""
    return Path(__file__).resolve().parent.parent


def calculate_sha256(file_path: Path) -> str:
    """Calculate the SHA-256 digest of a file."""
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()


def load_build_evidence(dist_dir: Path) -> dict:
    """Load the build evidence JSON file."""
    evidence_path = dist_dir / EVIDENCE_FILENAME

    if not evidence_path.exists():
        raise FileNotFoundError(
            f"Build evidence not found at {evidence_path}. "
            "Run scripts/build.py before generating provenance."
        )

    with open(evidence_path, "r", encoding="utf-8") as evidence_file:
        evidence = json.load(evidence_file)

    if not isinstance(evidence, dict):
        raise ValueError("Build evidence must contain a JSON object.")

    return evidence


def verify_artifact_matches_evidence(
    evidence: dict,
    dist_dir: Path,
) -> Path:
    """Verify that the artifact matches the recorded SHA-256 digest."""
    artifact_filename = evidence.get("artifact_filename")
    recorded_digest = evidence.get("artifact_sha256")

    if (
        not isinstance(artifact_filename, str)
        or not artifact_filename
        or Path(artifact_filename).name != artifact_filename
        or artifact_filename in {".", ".."}
    ):
        raise ValueError(
            "build-evidence.json must contain a valid artifact filename."
        )

    if (
        not isinstance(recorded_digest, str)
        or len(recorded_digest) != 64
        or any(character not in "0123456789abcdef" for character in recorded_digest)
    ):
        raise ValueError(
            "build-evidence.json must contain a valid lowercase SHA-256 digest."
        )

    artifact_path = dist_dir / artifact_filename

    if not artifact_path.is_file():
        raise FileNotFoundError(
            f"Build artifact not found at {artifact_path}. "
            "Run scripts/build.py again before generating provenance."
        )

    actual_digest = calculate_sha256(artifact_path)

    if actual_digest != recorded_digest:
        raise ValueError(
            "Artifact digest mismatch: build-evidence.json records "
            f"'{recorded_digest}', but the artifact hashes to "
            f"'{actual_digest}'. Refusing to generate provenance."
        )

    return artifact_path


def build_external_parameters() -> dict:
    """Describe workflow parameters from available GitHub environment data."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    server_url = os.environ.get("GITHUB_SERVER_URL")

    repository_url = None
    if server_url and repository:
        repository_url = f"{server_url}/{repository}"

    return {
        "workflow": {
            "repository": repository_url,
            "ref": os.environ.get("GITHUB_REF"),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        }
    }


def build_internal_parameters() -> dict:
    """Describe available GitHub runner metadata."""
    return {
        "github": {
            "runner_os": os.environ.get("RUNNER_OS"),
            "runner_arch": os.environ.get("RUNNER_ARCH"),
            "job": os.environ.get("GITHUB_JOB"),
        }
    }


def build_resolved_dependencies(evidence: dict) -> list:
    """Describe the source repository and commit when available."""
    repository = evidence.get("repository")
    commit_sha = evidence.get("commit_sha")

    if not repository or not commit_sha:
        return []

    return [
        {
            "uri": f"git+https://github.com/{repository}",
            "digest": {"gitCommit": commit_sha},
        }
    ]


def build_run_details(evidence: dict) -> dict:
    """Describe available build-run metadata."""
    repository = evidence.get("repository")
    run_id = evidence.get("workflow_run_id")

    invocation_id = None
    if repository and run_id:
        invocation_id = (
            f"https://github.com/{repository}/actions/runs/{run_id}"
        )

    return {
        "builder": {
            # Descriptive identity only; this document is not signed.
            "id": "https://github.com/actions/runner",
        },
        "metadata": {
            "invocationId": invocation_id,
            "startedOn": evidence.get("build_timestamp_utc"),
        },
    }


def build_provenance_predicate(evidence: dict) -> dict:
    """Build the provenance predicate."""
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": build_external_parameters(),
            "internalParameters": build_internal_parameters(),
            "resolvedDependencies": build_resolved_dependencies(evidence),
        },
        "runDetails": build_run_details(evidence),
    }


def build_subject(evidence: dict, verified_digest: str) -> list:
    """Describe the artifact using its independently calculated digest."""
    return [
        {
            "name": evidence["artifact_filename"],
            "digest": {"sha256": verified_digest},
        }
    ]


def build_statement(evidence: dict, verified_digest: str) -> dict:
    """Create the in-toto Statement."""
    return {
        "_type": STATEMENT_TYPE,
        "subject": build_subject(evidence, verified_digest),
        "predicateType": PREDICATE_TYPE,
        "predicate": build_provenance_predicate(evidence),
    }


def write_provenance_file(statement: dict, dist_dir: Path) -> Path:
    """Write provenance JSON to the selected distribution directory."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    provenance_path = dist_dir / PROVENANCE_FILENAME

    with open(provenance_path, "w", encoding="utf-8") as provenance_file:
        json.dump(statement, provenance_file, indent=2)
        provenance_file.write("\n")

    return provenance_path


def generate_provenance(dist_dir: Path) -> dict:
    """Verify the artifact and generate its provenance statement."""
    evidence = load_build_evidence(dist_dir)
    artifact_path = verify_artifact_matches_evidence(evidence, dist_dir)
    verified_digest = calculate_sha256(artifact_path)

    return build_statement(evidence, verified_digest)


def main() -> int:
    """Generate provenance for the selected build directory."""
    parser = argparse.ArgumentParser(
        description="Generate provenance for a built artifact."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=get_repository_root() / DIST_DIR_NAME,
        help=(
            "Directory containing the artifact and build evidence. "
            "Defaults to BuildGuard's dist directory."
        ),
    )
    args = parser.parse_args()
    dist_dir = args.dist_dir.resolve()

    try:
        statement = generate_provenance(dist_dir)
        provenance_path = write_provenance_file(statement, dist_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Provenance generation failed: {exc}")
        return 1

    subject = statement["subject"][0]

    print(f"Provenance written: {provenance_path}")
    print(f"Subject artifact: {subject['name']}")
    print(f"Subject digest (sha256): {subject['digest']['sha256']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
