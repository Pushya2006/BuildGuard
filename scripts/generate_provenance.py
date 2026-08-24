"""SLSA provenance generator for BuildGuard.

This script reads the raw build evidence and build artifact already
produced by scripts/build.py, and generates a SLSA-compatible
provenance attestation (an in-toto Statement whose predicateType is
https://slsa.dev/provenance/v1) describing the actual build that
produced the artifact.

SCOPE — READ BEFORE MODIFYING:
This script ONLY generates a provenance *document*. It does NOT:
  - sign or cryptographically attest the provenance
  - independently verify the provenance against anything
  - calculate, assign, or claim any SLSA assurance/build level
  - represent a trusted-builder attestation (the document is produced
    by this repository's own build tooling, not by an independent
    attestation service)

Producing this document is a building block towards SLSA alignment.
By itself, generating it does NOT mean any SLSA level has been
achieved. Provenance verification and assurance-level logic are
handled in a later stage of this project.

This script is designed to run:
  - Inside GitHub Actions, after scripts/build.py has already produced
    dist/buildguard-artifact.zip and dist/build-evidence.json, or
  - Locally, for testing purposes. GitHub-specific fields will be
    null when run locally, because that information genuinely does
    not exist outside of a GitHub Actions run. Nothing is invented to
    fill the gap.

VERY IMPORTANT — NO FABRICATED VALUES:
Every field in the generated provenance is either:
  (a) read directly from dist/build-evidence.json (itself populated
      from real GitHub Actions environment variables at build time),
      or
  (b) recomputed directly from the real artifact on disk (the SHA-256
      digest is independently recalculated here and cross-checked
      against build-evidence.json rather than merely copied).
Nothing here is hardcoded to a fixed repository, commit, run ID,
digest, or timestamp.
"""

import hashlib
import json
import os
from pathlib import Path

DIST_DIR_NAME = "dist"
EVIDENCE_FILENAME = "build-evidence.json"
PROVENANCE_FILENAME = "provenance.json"

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

# Identifies the *kind* of build definition this provenance describes:
# a build driven by a GitHub Actions workflow. This is a description
# of the build process, not a claim about assurance level.
BUILD_TYPE = "https://github.com/actions/workflows@v1"


def get_repository_root() -> Path:
    """Return the repository root directory.

    This script lives at <repo_root>/scripts/generate_provenance.py,
    so the repository root is two levels up from this file.
    """
    return Path(__file__).resolve().parent.parent


def calculate_sha256(file_path: Path) -> str:
    """Calculate and return the SHA-256 digest of a file, as a hex string."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def load_build_evidence(dist_dir: Path) -> dict:
    """Load the raw build evidence produced by scripts/build.py.

    Raises FileNotFoundError if build.py has not been run yet, rather
    than fabricating placeholder evidence.
    """
    evidence_path = dist_dir / EVIDENCE_FILENAME
    if not evidence_path.exists():
        raise FileNotFoundError(
            f"Build evidence not found at {evidence_path}. "
            "Run scripts/build.py before generating provenance."
        )
    with open(evidence_path, "r", encoding="utf-8") as evidence_file:
        return json.load(evidence_file)


def verify_artifact_matches_evidence(evidence: dict, dist_dir: Path) -> Path:
    """Confirm the artifact on disk is the one build-evidence.json describes.

    The digest recorded in build-evidence.json is independently
    recalculated from the actual artifact file on disk. If they do not
    match, provenance generation is refused rather than describing an
    artifact that may be stale or have been tampered with. Returns the
    verified artifact path.
    """
    artifact_filename = evidence.get("artifact_filename")
    recorded_digest = evidence.get("artifact_sha256")

    if not artifact_filename or not recorded_digest:
        raise ValueError(
            "build-evidence.json is missing artifact_filename or "
            "artifact_sha256; cannot generate provenance."
        )

    artifact_path = dist_dir / artifact_filename
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Build artifact not found at {artifact_path}, but "
            "build-evidence.json references it. Run scripts/build.py "
            "again before generating provenance."
        )

    actual_digest = calculate_sha256(artifact_path)
    if actual_digest != recorded_digest:
        raise ValueError(
            "Artifact digest mismatch: build-evidence.json records "
            f"'{recorded_digest}' but the artifact currently on disk "
            f"hashes to '{actual_digest}'. Refusing to generate "
            "provenance for a stale or modified artifact."
        )

    return artifact_path


def build_external_parameters() -> dict:
    """Describe what was requested of the build (trigger + workflow ref).

    These are read from GitHub Actions environment variables and are
    null when this script runs outside of GitHub Actions.
    """
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
    """Describe the runtime environment the build actually executed in."""
    return {
        "github": {
            "runner_os": os.environ.get("RUNNER_OS"),
            "runner_arch": os.environ.get("RUNNER_ARCH"),
            "job": os.environ.get("GITHUB_JOB"),
        }
    }


def build_resolved_dependencies(evidence: dict) -> list:
    """Describe the source commit that was actually checked out and built."""
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
    """Describe who/what ran the build and when."""
    repository = evidence.get("repository")
    run_id = evidence.get("workflow_run_id")

    invocation_id = None
    if repository and run_id:
        invocation_id = f"https://github.com/{repository}/actions/runs/{run_id}"

    return {
        "builder": {
            # Identifies the execution environment, not a trusted,
            # independently-attested builder identity. This provenance
            # document is produced by this repository's own build
            # tooling and is not cryptographically signed.
            "id": "https://github.com/actions/runner",
        },
        "metadata": {
            "invocationId": invocation_id,
            "startedOn": evidence.get("build_timestamp_utc"),
        },
    }


def build_provenance_predicate(evidence: dict) -> dict:
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
    """Build the in-toto subject describing the actual artifact.

    Uses the independently-recalculated digest (verified_digest)
    rather than simply trusting the value recorded in
    build-evidence.json, so the provenance subject is guaranteed to
    describe the artifact as it currently exists on disk.
    """
    return [
        {
            "name": evidence["artifact_filename"],
            "digest": {"sha256": verified_digest},
        }
    ]


def build_statement(evidence: dict, verified_digest: str) -> dict:
    return {
        "_type": STATEMENT_TYPE,
        "subject": build_subject(evidence, verified_digest),
        "predicateType": PREDICATE_TYPE,
        "predicate": build_provenance_predicate(evidence),
    }


def write_provenance_file(statement: dict, dist_dir: Path) -> Path:
    provenance_path = dist_dir / PROVENANCE_FILENAME
    with open(provenance_path, "w", encoding="utf-8") as provenance_file:
        json.dump(statement, provenance_file, indent=2)
        provenance_file.write("\n")
    return provenance_path


def generate_provenance(dist_dir: Path) -> dict:
    """Run the full provenance-generation pipeline and return the statement.

    Kept separate from main() so tests can call it directly without
    depending on stdout/print behavior.
    """
    evidence = load_build_evidence(dist_dir)
    artifact_path = verify_artifact_matches_evidence(evidence, dist_dir)
    verified_digest = calculate_sha256(artifact_path)
    return build_statement(evidence, verified_digest)


def main() -> None:
    repo_root = get_repository_root()
    dist_dir = repo_root / DIST_DIR_NAME

    statement = generate_provenance(dist_dir)
    provenance_path = write_provenance_file(statement, dist_dir)

    subject = statement["subject"][0]
    print(f"SLSA provenance written: {provenance_path}")
    print(f"Subject artifact: {subject['name']}")
    print(f"Subject digest (sha256): {subject['digest']['sha256']}")


if __name__ == "__main__":
    main()
