"""Build script for BuildGuard.

Builds BuildGuard itself by default, or a separate project when
--project-dir is supplied.

The script:
1. Removes known outputs from a previous build attempt.
2. Runs the target project's pytest tests.
3. Packages project files into a ZIP artifact.
4. Calculates the artifact's SHA-256 digest.
5. Writes build evidence.

This script generates raw build evidence only. It does not sign
provenance, independently attest to the build, or claim any SLSA level.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


# Preserve the original BuildGuard packaging configuration.
ARTIFACT_SOURCE_PATHS = [
    "app",
    "requirements.txt",
]

ARTIFACT_FILENAME = "buildguard-artifact.zip"
EVIDENCE_FILENAME = "build-evidence.json"
DIST_DIR_NAME = "dist"

# Known generated outputs from this pipeline.
GENERATED_OUTPUT_FILENAMES = (
    ARTIFACT_FILENAME,
    EVIDENCE_FILENAME,
    "provenance.json",
    "verification-report.json",
    "tamper-test-report.json",
    "assurance-report.json",
)

EXCLUDED_NAME_FRAGMENTS = (
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
)

# Common sensitive files that should not be packaged.
EXCLUDED_EXACT_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "secrets.json",
}

EXCLUDED_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


def get_repository_root() -> Path:
    """Return the BuildGuard repository root."""
    return Path(__file__).resolve().parent.parent


def remove_previous_build_outputs(dist_dir: Path) -> None:
    """Remove only known generated outputs from a previous build."""
    for filename in GENERATED_OUTPUT_FILENAMES:
        output_path = dist_dir / filename
        if output_path.is_file():
            output_path.unlink()


def should_exclude(path: Path) -> bool:
    """Return True for generated files, common secrets, and caches."""
    if path.suffix.lower() == ".pyc":
        return True

    if any(part.lower() in EXCLUDED_NAME_FRAGMENTS for part in path.parts):
        return True

    filename = path.name.lower()

    if filename in EXCLUDED_EXACT_FILENAMES:
        return True

    # Exclude .env and variants, but allow the documented example template.
    if filename == ".env" or (
        filename.startswith(".env.")
        and filename != ".env.example"
    ):
        return True

    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True

    return False


def collect_files_to_package(
    repo_root: Path,
    external_project: bool = False,
) -> list[Path]:
    """Collect source files for BuildGuard or an external project."""
    repo_root = repo_root.resolve()
    collected_files: list[Path] = []

    if external_project:
        dist_dir = (repo_root / DIST_DIR_NAME).resolve()

        for file_path in sorted(repo_root.rglob("*")):
            # Never package symbolic links or non-files.
            if file_path.is_symlink() or not file_path.is_file():
                continue

            relative_path = file_path.relative_to(repo_root)

            if should_exclude(relative_path):
                continue

            # Do not include generated outputs or evidence.
            if file_path.is_relative_to(dist_dir):
                continue

            collected_files.append(file_path)

        if not collected_files:
            raise FileNotFoundError(
                f"No project files found in {repo_root}"
            )

        return collected_files

    # Preserve BuildGuard's original packaging behavior.
    for relative_source in ARTIFACT_SOURCE_PATHS:
        source_path = repo_root / relative_source

        if source_path.is_symlink():
            continue

        if source_path.is_file():
            if not should_exclude(source_path.relative_to(repo_root)):
                collected_files.append(source_path)
            continue

        if source_path.is_dir():
            for file_path in sorted(source_path.rglob("*")):
                # Apply the same symbolic-link protection recursively.
                if (
                    file_path.is_symlink()
                    or not file_path.is_file()
                ):
                    continue

                if not should_exclude(
                    file_path.relative_to(repo_root)
                ):
                    collected_files.append(file_path)
            continue

        raise FileNotFoundError(
            f"Expected build source path does not exist: {source_path}"
        )

    if not collected_files:
        raise FileNotFoundError(
            f"No eligible source files found in {repo_root}"
        )

    return collected_files


def create_build_artifact(
    repo_root: Path,
    dist_dir: Path,
) -> Path:
    """Create a ZIP artifact and return its path."""
    repo_root = repo_root.resolve()
    dist_dir = dist_dir.resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = dist_dir / ARTIFACT_FILENAME

    if artifact_path.exists():
        artifact_path.unlink()

    external_project = repo_root != get_repository_root()

    files_to_package = collect_files_to_package(
        repo_root,
        external_project=external_project,
    )

    with zipfile.ZipFile(
        artifact_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for file_path in files_to_package:
            archive_name = file_path.relative_to(repo_root)
            archive.write(file_path, arcname=str(archive_name))

    return artifact_path


def run_project_tests(project_dir: Path) -> bool:
    """Run pytest in the target project and return its actual result."""
    print(f"Running pytest in: {project_dir}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v",
            ],
            cwd=project_dir,
            check=False,
        )
    except OSError as exc:
        print(f"Could not run project tests: {exc}")
        return False

    passed = result.returncode == 0
    print(f"Project tests: {'PASSED' if passed else 'FAILED'}")
    return passed


def calculate_sha256(file_path: Path) -> str:
    """Calculate a file's SHA-256 digest."""
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()


def build_evidence(
    artifact_path: Path,
    artifact_digest: str,
    tests_status: str = "passed",
) -> dict:
    """Assemble build evidence using available environment metadata."""
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
        "tests_status": tests_status,
        "build_status": "success",
    }


def write_evidence_file(evidence: dict, dist_dir: Path) -> Path:
    """Write build evidence to JSON."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = dist_dir / EVIDENCE_FILENAME

    with open(evidence_path, "w", encoding="utf-8") as evidence_file:
        json.dump(evidence, evidence_file, indent=2)
        evidence_file.write("\n")

    return evidence_path


def main() -> int:
    """Run tests, build the artifact, and write evidence."""
    parser = argparse.ArgumentParser(
        description="Build a Python project and collect build evidence."
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=get_repository_root(),
        help="Project directory to build (defaults to BuildGuard itself).",
    )
    args = parser.parse_args()

    repo_root = args.project_dir.resolve()

    if not repo_root.is_dir():
        parser.error(f"Project directory does not exist: {repo_root}")

    dist_dir = repo_root / DIST_DIR_NAME

    # Invalidate previous results before starting a new build attempt.
    try:
        remove_previous_build_outputs(dist_dir)
    except OSError as exc:
        print(f"Could not remove previous build outputs: {exc}")
        return 1

    # Stop before packaging if the target project's tests fail.
    if not run_project_tests(repo_root):
        print("Build stopped because project tests failed.")
        return 1

    try:
        artifact_path = create_build_artifact(repo_root, dist_dir)
        artifact_digest = calculate_sha256(artifact_path)

        evidence = build_evidence(
            artifact_path,
            artifact_digest,
            tests_status="passed",
        )
        evidence_path = write_evidence_file(evidence, dist_dir)

    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Build failed: {exc}")
        return 1

    print(f"Build artifact created: {artifact_path}")
    print(f"Artifact SHA-256: {artifact_digest}")
    print(f"Build evidence written: {evidence_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
