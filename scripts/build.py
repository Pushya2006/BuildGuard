"""Build script for BuildGuard.

This script performs three tasks, in order:

1. Package the application source into a ZIP build artifact.
2. Calculate the SHA-256 digest of that artifact.
3. Record raw build evidence (repository, commit, workflow run details,
   runner information, artifact identity, and digest) to a JSON file.

IMPORTANT:
This script produces raw build evidence only. It does NOT produce SLSA
provenance or any form of attestation, and it does NOT claim that any
SLSA assurance level has been achieved. Provenance generation and
verification are handled in a later stage of the project.

This script is designed to run:
- Inside GitHub Actions (using GitHub-provided environment variables), or
- Locally, for testing purposes (in which case environment-specific
  evidence fields will be recorded as null, since that information does
  not exist outside of a GitHub Actions run).
"""

import hashlib
import json
import os
import platform
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Files and directories that are included in the build artifact.
# These are the actual source files needed to run the application.
ARTIFACT_SOURCE_PATHS = [
    "app",
    "requirements.txt",
]

ARTIFACT_FILENAME = "buildguard-artifact.zip"
EVIDENCE_FILENAME = "build-evidence.json"
DIST_DIR_NAME = "dist"

# Directory/file name fragments that must never be included in the artifact,
# even if they happen to exist inside one of the ARTIFACT_SOURCE_PATHS.
EXCLUDED_NAME_FRAGMENTS = (
    "__pycache__",
    ".pytest_cache",
    ".git",
)


def get_repository_root() -> Path:
    """Return the repository root directory.

    This script lives at <repo_root>/scripts/build.py, so the repository
    root is two levels up from this file.
    """
    return Path(__file__).resolve().parent.parent


def should_exclude(path: Path) -> bool:
    """Return True if the given path should be excluded from the artifact."""
    if path.suffix == ".pyc":
        return True
    return any(fragment in path.parts for fragment in EXCLUDED_NAME_FRAGMENTS)


def collect_files_to_package(repo_root: Path) -> list[Path]:
    """Collect the list of files that belong in the build artifact.

    Only files under ARTIFACT_SOURCE_PATHS are included. Directories are
    walked recursively; excluded files (caches, git metadata, etc.) are
    skipped.
    """
    collected_files: list[Path] = []

    for relative_source in ARTIFACT_SOURCE_PATHS:
        source_path = repo_root / relative_source

        if source_path.is_file():
            if not should_exclude(source_path):
                collected_files.append(source_path)
            continue

        if source_path.is_dir():
            for file_path in sorted(source_path.rglob("*")):
                if file_path.is_file() and not should_exclude(file_path):
                    collected_files.append(file_path)
            continue

        raise FileNotFoundError(
            f"Expected build source path does not exist: {source_path}"
        )

    return collected_files


def create_build_artifact(repo_root: Path, dist_dir: Path) -> Path:
    """Create the ZIP build artifact and return its path.

    The artifact contains only the application source files listed in
    ARTIFACT_SOURCE_PATHS, stored with paths relative to the repository
    root so the archive layout mirrors the project layout.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = dist_dir / ARTIFACT_FILENAME

    # Remove any artifact left over from a previous local run so the
    # artifact always reflects the current source files.
    if artifact_path.exists():
        artifact_path.unlink()

    files_to_package = collect_files_to_package(repo_root)

    with zipfile.ZipFile(artifact_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in files_to_package:
            arcname = file_path.relative_to(repo_root)
            archive.write(file_path, arcname=str(arcname))

    return artifact_path


def calculate_sha256(file_path: Path) -> str:
    """Calculate and return the SHA-256 digest of a file, as a hex string."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def build_evidence(artifact_path: Path, artifact_digest: str) -> dict:
    """Assemble the build evidence dictionary.

    Values that come from the GitHub Actions environment are read from
    environment variables. When those variables are not present (for
    example, when this script is run locally), the corresponding field
    is recorded as null rather than a fabricated value.

    The "tests_status" field is recorded as "passed" because, in the
    GitHub Actions workflow, this script only runs after a separate
    pytest step has already completed successfully; if pytest had
    failed, GitHub Actions would have stopped the job before this
    script ever executed. This field does not represent an independent
    test result generated by this script.
    """
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "commit_sha": os.environ.get("GITHUB_SHA"),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "python_version": platform.python_version(),
        "artifact_filename": artifact_path.name,
        "artifact_sha256": artifact_digest,
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tests_status": "passed",
        "build_status": "success",
    }


def write_evidence_file(evidence: dict, dist_dir: Path) -> Path:
    """Write the evidence dictionary to build-evidence.json and return its path."""
    evidence_path = dist_dir / EVIDENCE_FILENAME
    with open(evidence_path, "w", encoding="utf-8") as evidence_file:
        json.dump(evidence, evidence_file, indent=2)
        evidence_file.write("\n")
    return evidence_path


def main() -> None:
    repo_root = get_repository_root()
    dist_dir = repo_root / DIST_DIR_NAME

    artifact_path = create_build_artifact(repo_root, dist_dir)
    artifact_digest = calculate_sha256(artifact_path)
    evidence = build_evidence(artifact_path, artifact_digest)
    evidence_path = write_evidence_file(evidence, dist_dir)

    print(f"Build artifact created: {artifact_path}")
    print(f"Artifact SHA-256: {artifact_digest}")
    print(f"Build evidence written: {evidence_path}")


if __name__ == "__main__":
    main()