"""End-to-end tests: local source traceability and local-vs-GitHub honesty.

These run the real build, provenance, verification and assurance code
against small throwaway projects.
"""

import json
import subprocess

import pytest

from scripts import build, evaluate_assurance, generate_provenance, verify_evidence

GITHUB_VARS = (
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT", "GITHUB_REF", "GITHUB_SERVER_URL",
    "GITHUB_WORKFLOW_REF", "RUNNER_OS", "RUNNER_ARCH", "GITHUB_JOB",
)


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    for var in GITHUB_VARS:
        monkeypatch.delenv(var, raising=False)


def make_project(tmp_path, git=True):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (project / "test_app.py").write_text(
        "from app import f\n\ndef test_f():\n    assert f() == 1\n",
        encoding="utf-8",
    )
    if git:
        run_git(project, "init", "-q")
        run_git(project, "add", "-A")
        run_git(project, "commit", "-q", "-m", "init")
    return project


def run_git(project, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=project, check=True, capture_output=True,
    )


def run_pipeline(project, monkeypatch, tamper_check=False):
    """Run build -> provenance -> verify -> assurance; return the dist dir."""
    dist = project / "dist"
    monkeypatch.setattr(
        "sys.argv",
        ["build.py", "--project-dir", str(project), "--run-id", "run-test-1"],
    )
    assert build.main() == 0
    statement = generate_provenance.generate_provenance(dist)
    generate_provenance.write_provenance_file(statement, dist)
    return dist


def assurance_for(dist):
    evaluate_assurance.run(dist)
    return json.loads((dist / "assurance-report.json").read_text())


def requirement(report, req_id):
    return next(r for r in report["requirements"] if r["id"] == req_id)


def test_clean_git_project_supports_source_traceability(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    dist = run_pipeline(project, monkeypatch)

    evidence = json.loads((dist / "build-evidence.json").read_text())
    assert evidence["build_environment"] == "local"
    assert evidence["source_dirty"] is False
    assert evidence["project_name"] == "proj"
    assert evidence["run_id"] == "run-test-1"

    report = verify_evidence.verify(dist)
    assert report["checks"]["source_commit_match"] == "PASS"
    assert "unattested" in report["details"]["source_commit_match"]

    assurance = assurance_for(dist)
    assert requirement(assurance, "R5")["status"] == "SUPPORTED"
    assert assurance["final_decision"] == "ACCEPT"


def test_dist_directory_does_not_make_the_tree_dirty(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    run_pipeline(project, monkeypatch)
    # Second build: dist/ from the first build now exists untracked.
    dist = run_pipeline(project, monkeypatch)

    evidence = json.loads((dist / "build-evidence.json").read_text())
    assert evidence["source_dirty"] is False


def test_dirty_project_is_not_traceable_and_is_revised(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / "app.py").write_text("def f():\n    return 1  # edit\n", encoding="utf-8")
    dist = run_pipeline(project, monkeypatch)

    report = verify_evidence.verify(dist)
    assert report["checks"]["source_commit_match"] == "SKIP"
    assert "uncommitted" in report["details"]["source_commit_match"]

    assurance = assurance_for(dist)
    assert requirement(assurance, "R5")["status"] == "UNSUPPORTED"
    assert assurance["final_decision"] == "REVISE"


def test_project_without_git_is_revised_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = make_project(tmp_path, git=False)
    dist = run_pipeline(project, monkeypatch)

    assert verify_evidence.verify(dist)["checks"]["source_commit_match"] == "SKIP"
    assurance = assurance_for(dist)
    assert assurance["final_decision"] == "REVISE"


def test_tampered_source_claim_is_rejected(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    dist = run_pipeline(project, monkeypatch)

    provenance_path = dist / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["predicate"]["buildDefinition"]["externalParameters"]["source"][
        "digest"]["gitCommit"] = "f" * 40
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    assert verify_evidence.verify(dist)["checks"]["source_commit_match"] == "FAIL"


def test_missing_source_entry_in_provenance_fails(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    dist = run_pipeline(project, monkeypatch)

    provenance_path = dist / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    del provenance["predicate"]["buildDefinition"]["externalParameters"]["source"]
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    assert verify_evidence.verify(dist)["checks"]["source_commit_match"] == "FAIL"


def test_local_provenance_is_not_described_as_github_actions(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    dist = run_pipeline(project, monkeypatch)

    statement = json.loads((dist / "provenance.json").read_text())
    predicate = statement["predicate"]

    assert predicate["runDetails"]["builder"]["id"] == generate_provenance.LOCAL_BUILDER_ID
    assert predicate["buildDefinition"]["buildType"] == generate_provenance.LOCAL_BUILD_TYPE
    assert "github" not in predicate["runDetails"]["builder"]["id"].lower()
    assert predicate["buildDefinition"]["resolvedDependencies"] == []
    source = predicate["buildDefinition"]["externalParameters"]["source"]
    assert "unattested" in source["observation"]
    assert predicate["runDetails"]["metadata"]["invocationId"] == "urn:buildguard:run:run-test-1"


def test_github_evidence_still_uses_github_builder_description():
    evidence = {
        "build_environment": "github-actions",
        "repository": "org/repo",
        "commit_sha": "a" * 40,
        "workflow_run_id": "42",
    }
    predicate = generate_provenance.build_provenance_predicate(evidence)

    assert predicate["runDetails"]["builder"]["id"] == generate_provenance.GITHUB_BUILDER_ID
    assert predicate["buildDefinition"]["buildType"] == generate_provenance.BUILD_TYPE
    assert predicate["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] == "a" * 40


def test_environment_detection(monkeypatch):
    assert build.detect_build_environment() == "local"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert build.detect_build_environment() == "github-actions"


def test_untracked_caches_do_not_dirty_but_new_source_does(tmp_path):
    project = make_project(tmp_path)

    # Files packaging always excludes cannot change the artifact.
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"x")
    (project / ".pytest_cache").mkdir()
    (project / ".pytest_cache" / "README.md").write_text("x", encoding="utf-8")
    assert build.is_working_tree_dirty(project) is False

    # A new untracked source file would be packaged, so it counts.
    (project / "new_module.py").write_text("X = 1\n", encoding="utf-8")
    assert build.is_working_tree_dirty(project) is True
