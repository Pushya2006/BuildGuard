"""Local-only HTTP API and web UI for BuildGuard.

The API runs the existing pipeline scripts (build, provenance,
verification, tamper test, assurance) as subprocesses, in order, and
exposes their real outputs. It never simulates results.

Safety model
------------
* Serves loopback clients only and checks the Host and Origin headers.
* Projects are chosen by name from one allow-listed projects root; the
  API never accepts a filesystem path from the browser.
* No shell is used; every subprocess has a timeout and a minimal
  environment.
* Running a project's tests executes that project's code. Only place
  projects you trust in the projects root.

Start it with:  python -m app.api
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scripts.project_adapters import ProjectError, get_adapter, make_project_id, validate_project_path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_PROJECTS_ROOT = REPO_ROOT / "projects"
DEFAULT_RUNS_ROOT = REPO_ROOT / "runs"

DEFAULT_TEST_TIMEOUT = 600
DEFAULT_STAGE_TIMEOUT = 60
LOG_LIMIT_BYTES = 500_000

RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
PROJECT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"

LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# (stage name, label, script)
STAGES = [
    ("build", "Build & test", "build.py"),
    ("provenance", "Provenance", "generate_provenance.py"),
    ("verification", "Verification", "verify_evidence.py"),
    ("tamper", "Tamper test", "test_tamper_detection.py"),
    ("assurance", "Assurance", "evaluate_assurance.py"),
]

# Stages whose non-zero exit is a result (not a crash) when this report
# exists, is valid JSON and contains the key.
RESULT_REPORTS = {
    "verification": ("verification-report.json", "verification_status"),
    "tamper": ("tamper-test-report.json", "tamper_detected"),
    "assurance": ("assurance-report.json", "final_decision"),
}

# Files a successful stage must have produced.
EXPECTED_OUTPUTS = {
    "build": ("buildguard-artifact.zip", "build-evidence.json", "build-status.json"),
    "provenance": ("provenance.json",),
    "verification": ("verification-report.json",),
    "tamper": ("tamper-test-report.json",),
    "assurance": ("assurance-report.json",),
}

REPORT_FILES = {
    "evidence": "build-evidence.json",
    "provenance": "provenance.json",
    "verification": "verification-report.json",
    "tamper": "tamper-test-report.json",
    "assurance": "assurance-report.json",
    "build_status": "build-status.json",
}

DOWNLOADS = {
    "artifact": ("buildguard-artifact.zip", "application/zip"),
    "evidence": ("build-evidence.json", "application/json"),
    "provenance": ("provenance.json", "application/json"),
    "verification": ("verification-report.json", "application/json"),
    "tamper": ("tamper-test-report.json", "application/json"),
    "assurance": ("assurance-report.json", "application/json"),
    "build-status": ("build-status.json", "application/json"),
    "test-output": ("test-output.log", "text/plain"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def read_json(path: Path):
    """Return parsed JSON, or None when missing or invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data) -> None:
    import tempfile
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2) + "\n"
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)

        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                temp_path = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def read_text_capped(path: Path, limit: int = LOG_LIMIT_BYTES) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += "\n[truncated]\n"
    return text


def clean_environment() -> dict:
    """Minimal environment for subprocesses (no inherited secrets)."""
    keep = (
        "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "LANG", "LC_ALL", "VIRTUAL_ENV",
    )
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # Keep the target project's tree free of cache files.
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


class RunRequest(BaseModel):
    project: str = Field(min_length=1, max_length=100, pattern=PROJECT_NAME_PATTERN)


class LocalProjectPathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class BusyError(Exception):
    pass


class RunManager:
    """Starts pipeline runs and stores one isolated directory per run."""

    def __init__(self, projects_root, runs_root, test_timeout, stage_timeout):
        self.projects_root = Path(projects_root).resolve()
        self.runs_root = Path(runs_root).resolve()
        self.test_timeout = test_timeout
        self.stage_timeout = stage_timeout
        self._lock = threading.Lock()
        self._active: str | None = None
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_runs()

    # ---- projects -------------------------------------------------

    def resolve_project(self, name: str) -> Path:
        candidate = self.projects_root / name
        if candidate.is_symlink() or not candidate.is_dir():
            raise HTTPException(404, "Project not found in the projects root.")
        if candidate.resolve().parent != self.projects_root:
            raise HTTPException(400, "Project must be a direct child of the projects root.")
        return candidate.resolve()

    def list_projects(self) -> list[dict]:
        projects = []
        for entry in sorted(self.projects_root.iterdir(), key=lambda p: p.name.lower()):
            if (
                entry.name.startswith(".")
                or entry.is_symlink()
                or not entry.is_dir()
                or not re.match(PROJECT_NAME_PATTERN, entry.name)
            ):
                continue
            has_tests = (
                any(entry.glob("test_*.py"))
                or any(entry.glob("*_test.py"))
                or (entry / "tests").is_dir()
                or (entry / "test").is_dir()
            )
            projects.append(
                {
                    "name": entry.name,
                    "has_tests": has_tests,
                    "is_git": (entry / ".git").exists(),
                }
            )
        return projects

    def inspect_external_project(self, raw_path: str) -> dict:
        """Inspect a local project without executing or modifying its code."""
        try:
            path = validate_project_path(
                raw_path,
                forbidden_roots=(REPO_ROOT, self.projects_root, self.runs_root),
            )
            # Do not allow choosing a parent directory that contains BuildGuard itself.
            if REPO_ROOT == path or REPO_ROOT in path.parents or path in REPO_ROOT.parents:
                raise ProjectError("unsafe_location", "Choose a specific project folder outside the BuildGuard installation and its parent folders.")
            info = get_adapter("python").discover(
                str(path), forbidden_roots=(REPO_ROOT, self.projects_root, self.runs_root)
            )
            return info.to_dict()
        except ProjectError as exc:
            raise HTTPException(400, exc.message) from exc

    def import_external_project(self, raw_path: str) -> dict:
        """Copy a validated local project into the allow-listed projects root."""
        info = self.inspect_external_project(raw_path)
        source = Path(info["path"])
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip(".-")[:60] or "project"
        project_name = f"local-{safe_name}-{info['project_id'][:8]}"
        destination = self.projects_root / project_name
        if destination.exists():
            return {
                "project": project_name,
                "source_name": source.name,
                "source_path": str(source),
                "working_copy": str(destination),
                "inspection": info,
                "already_imported": True,
                "message": "This project already has a working copy. The existing copy was kept unchanged.",
            }

        excluded_dirs = {
            "__pycache__", ".venv", "venv", "env", "node_modules", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".tox", "site-packages", "dist", "build",
        }

        def ignore_entries(directory, names):
            ignored = []
            for name in names:
                candidate = Path(directory) / name
                if name in excluded_dirs or candidate.is_symlink():
                    ignored.append(name)
            return ignored

        try:
            shutil.copytree(source, destination, ignore=ignore_entries, symlinks=False)
        except Exception as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(400, f"Could not create a working copy: {exc}") from exc

        return {
            "project": project_name,
            "source_name": source.name,
            "source_path": str(source),
            "working_copy": str(destination),
            "inspection": info,
            "message": "A working copy was created. The original project was not modified.",
        }

    # ---- runs -----------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.match(run_id):
            raise HTTPException(404, "Run not found.")
        path = self.runs_root / run_id
        if not path.is_dir():
            raise HTTPException(404, "Run not found.")
        return path

    def _save(self, run_dir: Path, record: dict) -> None:
        write_json_atomic(run_dir / "run.json", record)

    def _recover_interrupted_runs(self) -> None:
        for run_json in self.runs_root.glob("*/run.json"):
            record = read_json(run_json)
            if isinstance(record, dict) and record.get("status") in ("queued", "running"):
                record["status"] = "error"
                record["error"] = "Run was interrupted because the server stopped."
                record["finished_at"] = utc_now()
                for stage in record.get("stages", []):
                    if stage.get("status") in ("pending", "running"):
                        stage["status"] = "skipped"
                write_json_atomic(run_json, record)

    def start(self, project_name: str) -> dict:
        project = self.resolve_project(project_name)

        with self._lock:
            if self._active is not None:
                raise BusyError(self._active)
            run_id = generate_run_id()
            self._active = run_id

        try:
            run_dir = self.runs_root / run_id
            (run_dir / "dist").mkdir(parents=True)
            (run_dir / "logs").mkdir()
            record = {
                "run_id": run_id,
                "project": project_name,
                "status": "queued",
                "decision": None,
                "decision_reason": None,
                "error": None,
                "created_at": utc_now(),
                "started_at": None,
                "finished_at": None,
                "current_stage": None,
                "stages": [
                    {
                        "name": name,
                        "label": label,
                        "status": "pending",
                        "exit_code": None,
                        "duration_seconds": None,
                        "message": None,
                    }
                    for name, label, _ in STAGES
                ],
            }
            self._save(run_dir, record)
            threading.Thread(
                target=self._execute,
                args=(run_id, project, run_dir, record),
                daemon=True,
            ).start()
        except Exception:
            with self._lock:
                self._active = None
            raise

        return record

    def _command(self, name: str, script: str, project: Path, dist: Path, run_id: str):
        script_path = str(SCRIPTS_DIR / script)
        if name == "build":
            return [
                sys.executable, script_path,
                "--project-dir", str(project),
                "--dist-dir", str(dist),
                "--run-id", run_id,
                "--test-timeout", str(self.test_timeout),
            ]
        return [sys.executable, script_path, "--dist-dir", str(dist)]

    def _classify(self, name: str, exit_code: int, dist: Path) -> tuple[str, str]:
        """Return (stage status, message) from the exit code and outputs."""
        if exit_code == 0:
            missing = [f for f in EXPECTED_OUTPUTS[name] if not (dist / f).is_file()]
            if missing:
                return "error", f"Exited successfully but did not produce: {', '.join(missing)}"
            return "ok", ""

        if name == "build":
            status = read_json(dist / "build-status.json")
            if isinstance(status, dict) and status.get("build_status") == "failed":
                return "failed", status.get("failure_reason") or "Build failed."
            return "error", f"Build exited with code {exit_code} without recording a result."

        if name in RESULT_REPORTS and exit_code == 1:
            filename, key = RESULT_REPORTS[name]
            report = read_json(dist / filename)
            if isinstance(report, dict) and key in report:
                return "finding", ""

        return "error", f"Exited with code {exit_code} without a valid report."

    def _stage_message(self, name: str, status: str, message: str, dist: Path) -> str:
        if status in ("failed", "error"):
            return message
        if name == "build":
            return "Tests passed; artifact and evidence created."
        if name == "provenance":
            return "Unsigned provenance statement generated."
        filename, key = RESULT_REPORTS[name]
        report = read_json(dist / filename) or {}
        value = report.get(key)
        if name == "tamper":
            return "Tampering detected." if value else "Tampering was NOT detected."
        label = "Verification" if name == "verification" else "Decision"
        return f"{label}: {value}"

    def _execute(self, run_id: str, project: Path, run_dir: Path, record: dict) -> None:
        dist = run_dir / "dist"
        record["status"] = "running"
        record["started_at"] = utc_now()
        self._save(run_dir, record)
        env = clean_environment()
        build_failed = False

        try:
            for index, (name, _label, script) in enumerate(STAGES):
                stage = record["stages"][index]

                if build_failed:
                    stage["status"] = "skipped"
                    stage["message"] = "Skipped because the build did not complete."
                    continue

                stage["status"] = "running"
                record["current_stage"] = name
                self._save(run_dir, record)

                timeout = (
                    self.test_timeout + 120 if name == "build" else self.stage_timeout
                )
                started = time.monotonic()
                output = ""
                exit_code = None
                try:
                    result = subprocess.run(
                        self._command(name, script, project, dist, run_id),
                        cwd=REPO_ROOT,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        timeout=timeout,
                        shell=False,
                        check=False,
                    )
                    exit_code = result.returncode
                    output = result.stdout or ""
                    status, message = self._classify(name, exit_code, dist)
                except subprocess.TimeoutExpired as exc:
                    partial = exc.stdout or ""
                    output = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
                    status, message = "error", f"Timed out after {timeout} seconds."
                except OSError as exc:
                    status, message = "error", f"Could not start the stage: {exc}"

                (run_dir / "logs" / f"{name}.log").write_text(
                    output[:LOG_LIMIT_BYTES], encoding="utf-8", errors="replace"
                )
                stage.update(
                    {
                        "status": status,
                        "exit_code": exit_code,
                        "duration_seconds": round(time.monotonic() - started, 2),
                        "message": self._stage_message(name, status, message, dist),
                    }
                )
                self._save(run_dir, record)

                if name == "build" and status != "ok":
                    build_failed = True

            record["current_stage"] = None
            statuses = [s["status"] for s in record["stages"]]
            if "error" in statuses:
                record["status"] = "error"
                record["error"] = next(
                    f"{s['label']}: {s['message']}"
                    for s in record["stages"]
                    if s["status"] == "error"
                )
            elif record["stages"][0]["status"] == "failed":
                record["status"] = "build_failed"
            else:
                record["status"] = "completed"

            assurance = read_json(dist / "assurance-report.json")
            if isinstance(assurance, dict) and assurance.get("final_decision"):
                record["decision"] = assurance["final_decision"]
                record["decision_reason"] = assurance.get("decision_reason")

        except Exception as exc:  # never leave a run stuck in "running"
            record["status"] = "error"
            record["error"] = f"Unexpected API error: {exc}"
        finally:
            record["finished_at"] = utc_now()
            self._save(run_dir, record)
            with self._lock:
                self._active = None

    def get_run(self, run_id: str) -> dict:
        run_dir = self.run_dir(run_id)
        record = read_json(run_dir / "run.json")
        if not isinstance(record, dict):
            raise HTTPException(404, "Run record is missing or unreadable.")

        dist = run_dir / "dist"
        reports, report_errors = {}, {}
        for key, filename in REPORT_FILES.items():
            path = dist / filename
            if not path.is_file():
                reports[key] = None
                continue
            data = read_json(path)
            reports[key] = data
            if data is None:
                report_errors[key] = "File exists but is not valid JSON."

        record["reports"] = reports
        record["report_errors"] = report_errors
        record["test_output"] = read_text_capped(dist / "test-output.log")
        record["downloads"] = [
            {"key": key, "filename": filename, "size": (dist / filename).stat().st_size}
            for key, (filename, _type) in DOWNLOADS.items()
            if (dist / filename).is_file()
        ]
        record["logs"] = [name for name, _l, _s in STAGES if (run_dir / "logs" / f"{name}.log").is_file()]
        return record

    def list_runs(self, limit: int = 100) -> list[dict]:
        summaries = []
        for entry in sorted(self.runs_root.iterdir(), reverse=True):
            if not RUN_ID_PATTERN.match(entry.name) or not entry.is_dir():
                continue
            record = read_json(entry / "run.json")
            if not isinstance(record, dict):
                continue
            summaries.append(
                {key: record.get(key) for key in (
                    "run_id", "project", "status", "decision",
                    "created_at", "finished_at",
                )}
            )
            if len(summaries) >= limit:
                break
        return summaries


def create_app(
    projects_root: Path | str | None = None,
    runs_root: Path | str | None = None,
    test_timeout: int = DEFAULT_TEST_TIMEOUT,
    stage_timeout: int = DEFAULT_STAGE_TIMEOUT,
    allow_test_client: bool = False,
) -> FastAPI:
    manager = RunManager(
        projects_root or os.environ.get("BUILDGUARD_PROJECTS_ROOT") or DEFAULT_PROJECTS_ROOT,
        runs_root or os.environ.get("BUILDGUARD_RUNS_ROOT") or DEFAULT_RUNS_ROOT,
        test_timeout,
        stage_timeout,
    )
    app = FastAPI(title="BuildGuard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    allowed_clients = set(LOOPBACK_CLIENTS)
    if allow_test_client:
        allowed_clients.add("testclient")

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):
        def deny(status: int, detail: str):
            return JSONResponse({"detail": detail}, status_code=status, headers=SECURITY_HEADERS)

        client = request.client.host if request.client else None
        if client not in allowed_clients:
            return deny(403, "This API accepts local connections only.")

        host_header = request.headers.get("host", "")
        if (urlsplit("//" + host_header).hostname or "") not in LOOPBACK_HOSTS:
            return deny(403, "Unexpected Host header.")

        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != host_header:
                return deny(403, "Cross-origin requests are not allowed.")
            if "application/json" not in request.headers.get("content-type", ""):
                return deny(415, "Requests must be application/json.")

        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "projects_root": str(manager.projects_root),
            "scope": "loopback-only",
        }

    @app.get("/api/projects")
    def projects():
        return {"projects_root": str(manager.projects_root), "projects": manager.list_projects()}

    @app.get("/api/projects/browse")
    def browse_local_project():
        """Open a native folder picker on the local machine running BuildGuard."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(
                    parent=root,
                    title="Choose a Python project folder",
                    mustexist=True,
                )
            finally:
                root.destroy()
        except Exception as exc:
            raise HTTPException(
                501,
                f"Native folder picker is unavailable ({exc}). Enter the absolute folder path instead.",
            ) from exc
        if not selected:
            return {"cancelled": True, "path": ""}
        return {"cancelled": False, "path": str(Path(selected).resolve())}

    @app.post("/api/projects/inspect")
    def inspect_local_project(body: LocalProjectPathRequest):
        return manager.inspect_external_project(body.path)

    @app.post("/api/projects/import")
    def import_local_project(body: LocalProjectPathRequest):
        return manager.import_external_project(body.path)

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunRequest):
        try:
            return manager.start(body.project)
        except BusyError as exc:
            raise HTTPException(409, f"Run {exc} is still in progress.")

    @app.get("/api/runs")
    def runs():
        return {"runs": manager.list_runs()}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        return manager.get_run(run_id)

    @app.get("/api/runs/{run_id}/logs/{stage}")
    def run_log(run_id: str, stage: str):
        if stage not in {name for name, _l, _s in STAGES}:
            raise HTTPException(404, "Unknown stage.")
        text = read_text_capped(manager.run_dir(run_id) / "logs" / f"{stage}.log")
        if text is None:
            raise HTTPException(404, "No log for this stage.")
        return PlainTextResponse(text)

    @app.get("/api/runs/{run_id}/files/{key}")
    def run_file(run_id: str, key: str):
        if key not in DOWNLOADS:
            raise HTTPException(404, "Unknown file.")
        filename, media_type = DOWNLOADS[key]
        path = manager.run_dir(run_id) / "dist" / filename
        if not path.is_file():
            raise HTTPException(404, "File was not produced by this run.")
        return FileResponse(path, media_type=media_type, filename=f"{run_id}-{filename}")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local BuildGuard web UI.")
    parser.add_argument("--projects-root", type=Path, default=None,
                        help="Directory containing the projects BuildGuard may build.")
    parser.add_argument("--runs-root", type=Path, default=None,
                        help="Directory where run snapshots are stored.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    app = create_app(args.projects_root, args.runs_root)
    print(f"BuildGuard UI:  http://127.0.0.1:{args.port}")
    print(f"Projects root:  {app.state.manager.projects_root}")
    print("Listening on loopback only. Building a project runs its tests (its code).")
    # The host is intentionally not configurable: loopback only.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
