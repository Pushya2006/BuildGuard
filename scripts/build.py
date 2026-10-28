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
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
if __package__:
    from . import project_adapters
else:
    import project_adapters

def _git_output(project_dir: Path, *args: str) -> str:
    """Run a read-only git command in project_dir and return stdout."""
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def sanitize_remote_url(remote_url: str | None) -> str | None:
    """Remove any embedded credentials from a Git remote URL."""
    if not remote_url:
        return None

    if "://" in remote_url:
        parts = urlsplit(remote_url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit(
                (parts.scheme, host, parts.path, parts.query, parts.fragment)
            )

    return remote_url


def is_working_tree_dirty(
    project_dir: Path,
    ignore_dir: Path | None = None,
) -> bool | None:
    """Return True if the project has uncommitted changes.

    Untracked files count as changes because they could be packaged.
    Files that packaging always excludes (caches, bytecode, secrets,
    root-level build output) and BuildGuard's own output directory are
    ignored. Returns None when the state cannot be determined.
    """
    project_dir = project_dir.resolve()

    try:
        toplevel = Path(_git_output(project_dir, "rev-parse", "--show-toplevel"))
        output = subprocess.run(
            [
                "git", "-C", str(project_dir),
                "status", "--porcelain", "--untracked-files=all", "--", ".",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None

    ignore_prefix = None
    if ignore_dir is not None:
        try:
            ignore_prefix = (
                ignore_dir.resolve().relative_to(toplevel.resolve()).as_posix()
            )
        except ValueError:
            ignore_prefix = None

    for line in output.splitlines():
        entry = line[3:].split(" -> ")[-1].strip().strip('"')
        if ignore_prefix and (
            entry == ignore_prefix or entry.startswith(ignore_prefix + "/")
        ):
            continue

        # Changes to files that are never packaged (caches, bytecode,
        # secrets, build output) cannot alter the artifact.
        try:
            relative = (toplevel / entry).resolve().relative_to(project_dir)
        except ValueError:
            return True
        if should_exclude(relative):
            continue

        return True

    return False


def get_source_identity(
    project_dir: Path,
    ignore_dir: Path | None = None,
) -> dict:
    """Identify the target project's Git source when available.

    Every key is always present so callers never fail on projects that
    have no Git metadata. ``repository`` and ``commit_sha`` come only
    from GitHub Actions; the ``local_*`` values are observed from a
    local Git checkout and are not an attestation.
    """
    project_dir = project_dir.resolve()

    identity = {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "repository_url": None,
        "commit_sha": os.environ.get("GITHUB_SHA"),
        "local_commit_sha": None,
        "source_type": "local",
        "source_root": str(project_dir),
        "source_dirty": None,
        "source_uri": None,
    }

    try:
        repository_root = _git_output(project_dir, "rev-parse", "--show-toplevel")
        commit_sha = _git_output(project_dir, "rev-parse", "HEAD")
        # A project that merely sits inside some other repository (for
        # example an ignored or untracked folder) must not inherit that
        # repository's commit: require tracked files in the project.
        tracked = _git_output(project_dir, "ls-files", "--cached", "--", ".")
    except (subprocess.CalledProcessError, OSError):
        return identity

    if not tracked:
        return identity

    remote_result = subprocess.run(
        ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    remote_url = (
        sanitize_remote_url(remote_result.stdout.strip())
        if remote_result.returncode == 0
        else None
    )

    identity.update(
        {
            "repository_url": remote_url,
            "local_commit_sha": commit_sha,
            "source_type": "git",
            "source_root": repository_root,
            "source_dirty": is_working_tree_dirty(project_dir, ignore_dir),
            "source_uri": remote_url or f"local-git:{project_dir.name}",
        }
    )
    return identity


# Preserve the original BuildGuard packaging configuration.
ARTIFACT_SOURCE_PATHS = [
    "app",
    "requirements.txt",
]

ARTIFACT_FILENAME = "buildguard-artifact.zip"
EVIDENCE_FILENAME = "build-evidence.json"
BUILD_STATUS_FILENAME = "build-status.json"
TEST_LOG_FILENAME = "test-output.log"
DIST_DIR_NAME = "dist"
DEFAULT_TEST_TIMEOUT_SECONDS = 600

# Known generated outputs from this pipeline.
GENERATED_OUTPUT_FILENAMES = (
    ARTIFACT_FILENAME,
    EVIDENCE_FILENAME,
    "provenance.json",
    "verification-report.json",
    "tamper-test-report.json",
    "assurance-report.json",
    BUILD_STATUS_FILENAME,
    TEST_LOG_FILENAME,
)

# Directories that are never project source, at any depth.
EXCLUDED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".eggs",
        "node_modules",
    }
)

# Conventional output directories. They are generated output only at the
# project root; nested names such as src/build/ are legitimate source.
ROOT_ONLY_EXCLUDED_NAMES = frozenset({"dist", "build"})

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
    """Return True for generated files, common secrets, and caches.

    ``path`` is relative to the project root.
    """
    if path.suffix.lower() == ".pyc":
        return True

    parts = [part.lower() for part in path.parts]

    if any(
        part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info")
        for part in parts
    ):
        return True

    if parts and parts[0] in ROOT_ONLY_EXCLUDED_NAMES:
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
    dist_dir: Path | None = None,
) -> list[Path]:
    """Collect source files for BuildGuard or an external project."""
    repo_root = repo_root.resolve()
    collected_files: list[Path] = []

    if external_project:
        dist_dir = (
            dist_dir.resolve()
            if dist_dir is not None
            else (repo_root / DIST_DIR_NAME).resolve()
        )

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
        dist_dir=dist_dir,
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



def execute_project_tests(
    project_dir: Path,
    log_path: Path | None = None,
    timeout: int | None = DEFAULT_TEST_TIMEOUT_SECONDS,
) -> str:
    """Discover, execute and classify project tests through the Python adapter.

    The build pipeline accepts only "passed", "failed", "timeout" and "error".
    Outcomes that are missing or insufficiently verified are never successes.
    """
    print(f"Discovering Python project tests in: {project_dir}")

    output = ""
    status = "error"
    junit_path = None

    try:
        adapter = project_adapters.get_adapter("python")
        info = project_adapters.inspect_project(project_dir.resolve())
        runner_info = info.tests

        if runner_info.needs_configuration or runner_info.runner is None:
            output = (
                "[BuildGuard] Test runner needs configuration. "
                f"{runner_info.reason}\n"
            )
            status = "error"
        else:
            effective_timeout = (
                timeout
                if timeout is not None
                else DEFAULT_TEST_TIMEOUT_SECONDS
            )
            config = project_adapters.ProjectConfig(
                runner=runner_info.runner,
                timeout_seconds=effective_timeout,
            )

            if runner_info.runner == "pytest":
                with tempfile.TemporaryDirectory(prefix="buildguard-junit-") as temp_dir:
                    junit_path = Path(temp_dir) / "results.xml"
                    command = adapter.test_command(config, junit_path)
                    print(f"Running {runner_info.runner} in: {project_dir}")

                    try:
                        result = subprocess.run(
                            list(command.argv),
                            cwd=project_dir,
                            check=False,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            errors="replace",
                            timeout=effective_timeout,
                        )
                        output = result.stdout or ""
                        outcome = adapter.classify_outcome(
                            command,
                            result.returncode,
                            output,
                        )
                    except subprocess.TimeoutExpired as exc:
                        partial = exc.stdout or ""
                        if isinstance(partial, bytes):
                            partial = partial.decode("utf-8", errors="replace")
                        output = partial
                        outcome = adapter.classify_outcome(
                            command,
                            None,
                            output,
                            timed_out=True,
                        )
                    except OSError as exc:
                        output = f"[BuildGuard] Could not run project tests: {exc}\n"
                        outcome = adapter.classify_outcome(
                            command,
                            None,
                            output,
                            launch_error=str(exc),
                        )

            else:
                command = adapter.test_command(config)
                print(f"Running {runner_info.runner} in: {project_dir}")

                try:
                    result = subprocess.run(
                        list(command.argv),
                        cwd=project_dir,
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        timeout=effective_timeout,
                    )
                    output = result.stdout or ""
                    outcome = adapter.classify_outcome(
                        command,
                        result.returncode,
                        output,
                    )
                except subprocess.TimeoutExpired as exc:
                    partial = exc.stdout or ""
                    if isinstance(partial, bytes):
                        partial = partial.decode("utf-8", errors="replace")
                    output = partial
                    outcome = adapter.classify_outcome(
                        command,
                        None,
                        output,
                        timed_out=True,
                    )
                except OSError as exc:
                    output = f"[BuildGuard] Could not run project tests: {exc}\n"
                    outcome = adapter.classify_outcome(
                        command,
                        None,
                        output,
                        launch_error=str(exc),
                    )

            output += (
                f"\n[BuildGuard] Test outcome: {outcome.status}. "
                f"{outcome.reason}\n"
            )
            status = (
                outcome.status
                if outcome.status in {"passed", "failed", "timeout", "error"}
                else "error"
            )

    except project_adapters.ProjectError as exc:
        output = f"[BuildGuard] {exc.message}\n"
        status = "error"
    except (OSError, ValueError, TypeError) as exc:
        output = f"[BuildGuard] Project test discovery failed: {exc}\n"
        status = "error"

    print(output, end="" if output.endswith("\n") else "\n")

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
        except OSError as exc:
            print(f"Could not write test log: {exc}")

    print(f"Project tests: {status.upper()}")
    return status


def run_project_tests(project_dir: Path) -> bool:
    """Run pytest in the target project and return whether it passed."""
    return execute_project_tests(project_dir) == "passed"


def calculate_sha256(file_path: Path) -> str:
    """Calculate a file's SHA-256 digest."""
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(8192), b""):
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()



def generate_run_id() -> str:
    """Return a unique, sortable run identifier."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def detect_build_environment() -> str:
    """Distinguish GitHub Actions builds from local builds."""
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("GITHUB_RUN_ID"):
        return "github-actions"
    return "local"


def build_evidence(
    artifact_path: Path,
    artifact_digest: str,
    project_dir: Path | None = None,
    tests_status: str = "passed",
    source: dict | None = None,
    run_id: str | None = None,
) -> dict:
    """Assemble build evidence using available environment metadata."""
    if project_dir is None:
        project_dir = get_repository_root()

    # Callers may pass a source identity captured before the tests ran,
    # so files created by the test run cannot make the tree look dirty.
    if source is None:
        source = get_source_identity(project_dir)

    return {
        "repository": source["repository"],
        "commit_sha": source["commit_sha"],
        "source_type": source["source_type"],
        "source_root": source["source_root"],
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "python_version": platform.python_version(),
        "artifact_filename": artifact_path.name,
        "artifact_sha256": artifact_digest,
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tests_status": tests_status,
        "build_status": "success",
        "repository_url": source["repository_url"],
        "local_commit_sha": source["local_commit_sha"],
        "project_name": Path(project_dir).resolve().name,
        "run_id": run_id,
        "build_environment": detect_build_environment(),
        "source_dirty": source.get("source_dirty"),
        "source_uri": source.get("source_uri"),
    }


def write_evidence_file(evidence: dict, dist_dir: Path) -> Path:
    """Write build evidence to JSON."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = dist_dir / EVIDENCE_FILENAME

    with open(evidence_path, "w", encoding="utf-8") as evidence_file:
        json.dump(evidence, evidence_file, indent=2)
        evidence_file.write("\n")

    return evidence_path


def write_build_status(
    dist_dir: Path,
    *,
    run_id: str,
    project_name: str,
    build_status: str,
    tests_status: str,
    started_at: str,
    failure_reason: str | None = None,
) -> Path:
    """Record the outcome of a build attempt, including failed attempts.

    build-evidence.json is written only for successful builds; this file
    exists so that a failed build can still be displayed and audited.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    status_path = dist_dir / BUILD_STATUS_FILENAME
    status = {
        "run_id": run_id,
        "project_name": project_name,
        "build_status": build_status,
        "tests_status": tests_status,
        "failure_reason": failure_reason,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "test_output_file": TEST_LOG_FILENAME,
    }
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return status_path


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
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Output directory (defaults to <project-dir>/dist).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Unique run identifier (generated when omitted).",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=DEFAULT_TEST_TIMEOUT_SECONDS,
        help="Seconds before the project's test run is stopped.",
    )
    args = parser.parse_args()

    repo_root = args.project_dir.resolve()

    if not repo_root.is_dir():
        parser.error(f"Project directory does not exist: {repo_root}")

    dist_dir = (
        args.dist_dir.resolve()
        if args.dist_dir is not None
        else repo_root / DIST_DIR_NAME
    )
    run_id = args.run_id or generate_run_id()
    project_name = repo_root.name
    started_at = datetime.now(timezone.utc).isoformat()

    # Invalidate previous results before starting a new build attempt.
    try:
        dist_dir.mkdir(parents=True, exist_ok=True)
        remove_previous_build_outputs(dist_dir)
    except OSError as exc:
        print(f"Could not remove previous build outputs: {exc}")
        return 1

    def record(build_status, tests_status, reason=None):
        try:
            write_build_status(
                dist_dir,
                run_id=run_id,
                project_name=project_name,
                build_status=build_status,
                tests_status=tests_status,
                started_at=started_at,
                failure_reason=reason,
            )
        except OSError as exc:
            print(f"Could not write build status: {exc}")

    # Capture source identity before tests run, so files created by the
    # test run (caches, bytecode) cannot affect the clean/dirty result.
    source = get_source_identity(repo_root, ignore_dir=dist_dir)

    # Stop before packaging if the target project's tests fail.
    tests_status = execute_project_tests(
        repo_root,
        log_path=dist_dir / TEST_LOG_FILENAME,
        timeout=args.test_timeout,
    )
    if tests_status != "passed":
        reason = {
            "failed": "Project tests failed.",
            "timeout": "Project tests exceeded the time limit.",
        }.get(tests_status, "Project tests could not be run.")
        print("Build stopped because project tests did not pass.")
        record("failed", tests_status, reason)
        return 1

    try:
        artifact_path = create_build_artifact(repo_root, dist_dir)
        artifact_digest = calculate_sha256(artifact_path)

        evidence = build_evidence(
            artifact_path,
            artifact_digest,
            project_dir=repo_root,
            tests_status="passed",
            source=source,
            run_id=run_id,
        )
        evidence_path = write_evidence_file(evidence, dist_dir)

    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"Build failed: {exc}")
        record("failed", "passed", f"Packaging failed: {exc}")
        return 1

    record("success", "passed")

    print(f"Build artifact created: {artifact_path}")
    print(f"Artifact SHA-256: {artifact_digest}")
    print(f"Build evidence written: {evidence_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
