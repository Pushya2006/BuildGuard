"""Tests for run identity, captured test output and failed-run records."""

import json

from scripts import build


def make_project(tmp_path, passing=True):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    body = "assert f() == 1" if passing else "assert f() == 2, 'deliberate failure'"
    (project / "test_app.py").write_text(
        f"from app import f\n\ndef test_f():\n    {body}\n", encoding="utf-8"
    )
    return project


def run_build(project, monkeypatch, *extra):
    monkeypatch.setattr("sys.argv", ["build.py", "--project-dir", str(project), *extra])
    return build.main()


def test_successful_build_records_identity_and_status(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert run_build(project, monkeypatch, "--run-id", "abc-1") == 0

    dist = project / "dist"
    evidence = json.loads((dist / build.EVIDENCE_FILENAME).read_text())
    status = json.loads((dist / build.BUILD_STATUS_FILENAME).read_text())

    assert evidence["project_name"] == "proj"
    assert evidence["run_id"] == "abc-1"
    assert status["build_status"] == "success"
    assert status["tests_status"] == "passed"
    assert status["run_id"] == "abc-1"
    assert "passed" in (dist / build.TEST_LOG_FILENAME).read_text()


def test_run_id_is_generated_and_unique_when_omitted():
    first, second = build.generate_run_id(), build.generate_run_id()
    assert first != second
    assert first.endswith(first.split("-")[-1]) and "T" in first


def test_failed_tests_are_recorded_but_no_artifact_or_evidence(tmp_path, monkeypatch):
    project = make_project(tmp_path, passing=False)
    assert run_build(project, monkeypatch, "--run-id", "bad-1") == 1

    dist = project / "dist"
    status = json.loads((dist / build.BUILD_STATUS_FILENAME).read_text())

    assert status["build_status"] == "failed"
    assert status["tests_status"] == "failed"
    assert status["failure_reason"]
    assert "deliberate failure" in (dist / build.TEST_LOG_FILENAME).read_text()
    assert not (dist / build.ARTIFACT_FILENAME).exists()
    assert not (dist / build.EVIDENCE_FILENAME).exists()


def test_test_timeout_is_recorded_as_failure(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / "test_app.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )

    assert run_build(project, monkeypatch, "--test-timeout", "1") == 1

    status = json.loads((project / "dist" / build.BUILD_STATUS_FILENAME).read_text())
    assert status["tests_status"] == "timeout"
    assert status["build_status"] == "failed"
    assert not (project / "dist" / build.ARTIFACT_FILENAME).exists()


def test_custom_dist_dir_keeps_project_untouched(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    out = tmp_path / "elsewhere"

    assert run_build(project, monkeypatch, "--dist-dir", str(out)) == 0

    assert (out / build.ARTIFACT_FILENAME).is_file()
    assert not (project / "dist").exists()


def test_stale_outputs_are_cleared_before_a_new_attempt(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert run_build(project, monkeypatch) == 0
    (project / "test_app.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")

    assert run_build(project, monkeypatch) == 1
    assert not (project / "dist" / build.ARTIFACT_FILENAME).exists()
    assert not (project / "dist" / build.EVIDENCE_FILENAME).exists()
