"""Tests for the local API. These run the real pipeline scripts."""

import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import api

GITHUB_VARS = (
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT", "GITHUB_REF", "GITHUB_SERVER_URL",
    "GITHUB_WORKFLOW_REF", "RUNNER_OS", "RUNNER_ARCH", "GITHUB_JOB",
)


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    for var in GITHUB_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def roots(tmp_path):
    projects, runs = tmp_path / "projects", tmp_path / "runs"
    projects.mkdir()
    return projects, runs


@pytest.fixture()
def client(roots):
    app = api.create_app(roots[0], roots[1], allow_test_client=True)
    return TestClient(app, base_url="http://localhost")


def git(project, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=project, check=True, capture_output=True,
    )


def make_project(projects, name, passing=True, use_git=True):
    project = projects / name
    project.mkdir()
    (project / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    check = "assert f() == 1" if passing else "assert f() == 2, 'deliberate failure'"
    (project / "test_app.py").write_text(
        f"from app import f\n\ndef test_f():\n    {check}\n", encoding="utf-8"
    )
    if use_git:
        git(project, "init", "-q")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "init")
    return project


def wait_for_run(client, run_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["status"] not in ("queued", "running"):
            return detail
        time.sleep(0.2)
    pytest.fail("run did not finish in time")


def run_to_completion(client, project, timeout=120):
    response = client.post("/api/runs", json={"project": project})
    assert response.status_code == 202, response.text
    return wait_for_run(client, response.json()["run_id"], timeout)


def stage_statuses(detail):
    return {s["name"]: s["status"] for s in detail["stages"]}


# ---- real pipeline results ------------------------------------------

def test_clean_git_project_run_is_accepted_with_real_reports(client, roots):
    make_project(roots[0], "clean")

    detail = run_to_completion(client, "clean")

    assert detail["status"] == "completed"
    assert detail["decision"] == "ACCEPT"
    assert set(stage_statuses(detail).values()) == {"ok"}
    assert detail["reports"]["assurance"]["final_decision"] == "ACCEPT"
    assert detail["reports"]["verification"]["verification_status"] == "PASS"
    assert detail["reports"]["tamper"]["tamper_detected"] is True
    assert detail["reports"]["evidence"]["project_name"] == "clean"
    assert detail["reports"]["evidence"]["run_id"] == detail["run_id"]
    assert "passed" in detail["test_output"]
    assert {d["key"] for d in detail["downloads"]} >= {"artifact", "evidence", "assurance"}


def test_downloaded_artifact_matches_recorded_digest(client, roots):
    import hashlib

    make_project(roots[0], "dl")
    detail = run_to_completion(client, "dl")

    response = client.get(f"/api/runs/{detail['run_id']}/files/artifact")
    assert response.status_code == 200
    digest = hashlib.sha256(response.content).hexdigest()
    assert digest == detail["reports"]["evidence"]["artifact_sha256"]
    assert "attachment" in response.headers["content-disposition"]


def test_failing_tests_are_a_build_failure_not_a_success(client, roots):
    make_project(roots[0], "broken", passing=False)

    detail = run_to_completion(client, "broken")

    assert detail["status"] == "build_failed"
    assert detail["decision"] is None
    statuses = stage_statuses(detail)
    assert statuses["build"] == "failed"
    assert all(statuses[s] == "skipped" for s in ("provenance", "verification", "tamper", "assurance"))
    assert "deliberate failure" in detail["test_output"]
    assert detail["reports"]["build_status"]["tests_status"] == "failed"
    assert detail["reports"]["evidence"] is None
    assert client.get(f"/api/runs/{detail['run_id']}/files/artifact").status_code == 404


def test_project_without_git_is_revise_and_not_an_error(client, roots, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(roots[0].parent))
    make_project(roots[0], "plain", use_git=False)

    detail = run_to_completion(client, "plain")

    # A non-zero exit that a valid report explains is a result, not a crash.
    assert detail["status"] == "completed"
    assert detail["decision"] == "REVISE"
    statuses = stage_statuses(detail)
    assert statuses["assurance"] == "finding"
    assert "error" not in statuses.values()


def test_dirty_git_project_is_revise(client, roots):
    project = make_project(roots[0], "dirty")
    (project / "app.py").write_text("def f():\n    return 1  # edit\n", encoding="utf-8")

    detail = run_to_completion(client, "dirty")

    assert detail["decision"] == "REVISE"
    assert detail["reports"]["verification"]["checks"]["source_commit_match"] == "SKIP"


def test_stage_crash_is_an_error_and_never_accepted(client, roots, tmp_path, monkeypatch):
    make_project(roots[0], "crashy")
    scripts_copy = tmp_path / "scripts_copy"
    shutil.copytree(api.SCRIPTS_DIR, scripts_copy, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts_copy / "generate_provenance.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setattr(api, "SCRIPTS_DIR", scripts_copy)

    detail = run_to_completion(client, "crashy")

    statuses = stage_statuses(detail)
    assert statuses["provenance"] == "error"
    assert detail["status"] == "error"
    assert "Provenance" in detail["error"]
    assert detail["decision"] != "ACCEPT"


def test_stage_timeout_is_an_error(roots, tmp_path, monkeypatch):
    make_project(roots[0], "slow")
    scripts_copy = tmp_path / "scripts_copy"
    shutil.copytree(api.SCRIPTS_DIR, scripts_copy, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts_copy / "generate_provenance.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(api, "SCRIPTS_DIR", scripts_copy)
    app = api.create_app(roots[0], roots[1], stage_timeout=2, allow_test_client=True)
    local = TestClient(app, base_url="http://localhost")

    detail = run_to_completion(local, "slow")

    provenance = next(s for s in detail["stages"] if s["name"] == "provenance")
    assert provenance["status"] == "error"
    assert "Timed out" in provenance["message"]
    assert detail["status"] == "error"


def test_second_run_is_rejected_while_one_is_active(client, roots):
    make_project(roots[0], "busy")
    first = client.post("/api/runs", json={"project": "busy"})
    second = client.post("/api/runs", json={"project": "busy"})
    assert first.status_code == 202
    assert second.status_code == 409
    wait_for_run(client, first.json()["run_id"])


def test_runs_are_isolated_and_listed(client, roots):
    make_project(roots[0], "one")
    first = run_to_completion(client, "one")
    second = run_to_completion(client, "one")

    assert first["run_id"] != second["run_id"]
    listing = client.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in listing[:2]] == [second["run_id"], first["run_id"]]
    assert not (roots[0] / "one" / "dist").exists()
    assert (roots[1] / first["run_id"] / "dist" / "buildguard-artifact.zip").is_file()


def test_logs_endpoint_returns_real_stage_output(client, roots):
    make_project(roots[0], "logs")
    detail = run_to_completion(client, "logs")

    text = client.get(f"/api/runs/{detail['run_id']}/logs/assurance").text
    assert "Final decision: ACCEPT" in text


def test_interrupted_runs_are_marked_error_on_startup(roots):
    run_dir = roots[1] / "20250101T000000Z-aaaaaaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_dir.name, "status": "running",
        "stages": [{"name": "build", "label": "Build", "status": "running"}],
    }), encoding="utf-8")

    api.create_app(roots[0], roots[1], allow_test_client=True)

    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "error"
    assert record["stages"][0]["status"] == "skipped"


# ---- projects and path safety ----------------------------------------

def test_projects_listing_uses_only_the_allowed_root(client, roots):
    make_project(roots[0], "alpha")
    (roots[0] / ".hidden").mkdir()
    (roots[0] / "notes.txt").write_text("x", encoding="utf-8")

    names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
    assert names == ["alpha"]


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", "/etc", "C:\\x", "", ".hidden", "x" * 200])
def test_unsafe_project_names_are_rejected(client, name):
    response = client.post("/api/runs", json={"project": name})
    assert response.status_code in (404, 422)


def test_unknown_project_is_not_found(client):
    assert client.post("/api/runs", json={"project": "missing"}).status_code == 404


def test_symlinked_project_is_rejected(client, roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (roots[0] / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this system")

    assert client.post("/api/runs", json={"project": "linked"}).status_code == 404
    assert "linked" not in [p["name"] for p in client.get("/api/projects").json()["projects"]]


@pytest.mark.parametrize("run_id", ["../x", "nope", "20250101T000000Z-zzzzzzzz"])
def test_invalid_run_ids_are_not_found(client, run_id):
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_unknown_download_key_and_stage_are_rejected(client, roots):
    make_project(roots[0], "keys")
    detail = run_to_completion(client, "keys")
    assert client.get(f"/api/runs/{detail['run_id']}/files/../../x").status_code == 404
    assert client.get(f"/api/runs/{detail['run_id']}/files/secrets").status_code == 404
    assert client.get(f"/api/runs/{detail['run_id']}/logs/evil").status_code == 404


# ---- loopback-only protections ---------------------------------------

def test_non_loopback_client_is_rejected(roots):
    app = api.create_app(roots[0], roots[1], allow_test_client=False)
    remote = TestClient(app, base_url="http://localhost")  # client host is "testclient"
    assert remote.get("/api/health").status_code == 403


def test_foreign_host_header_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "evil.example.com"}).status_code == 403


def test_cross_origin_post_is_rejected(client, roots):
    make_project(roots[0], "csrf")
    response = client.post(
        "/api/runs", json={"project": "csrf"}, headers={"Origin": "http://evil.example.com"}
    )
    assert response.status_code == 403


def test_non_json_post_is_rejected(client):
    response = client.post("/api/runs", content="project=x", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_security_headers_and_static_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_uvicorn_entry_point_binds_loopback_only():
    import inspect
    source = inspect.getsource(api.main)
    assert 'host="127.0.0.1"' in source
    assert "--host" not in source
