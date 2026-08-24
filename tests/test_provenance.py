"""Tests for scripts/generate_provenance.py.

These tests exercise the real build -> evidence -> provenance pipeline
against the actual repository (the same way the GitHub Actions
workflow does: scripts/build.py runs first, then
scripts/generate_provenance.py). dist/ is gitignored, so writing to it
here does not affect version control.
"""

import json

import pytest

from scripts import build, generate_provenance


@pytest.fixture()
def dist_dir(tmp_path, monkeypatch):
    """Run the real build against the actual repo, but redirect dist/
    output to a temporary directory so tests never depend on or pollute
    a real local dist/ folder.
    """
    repo_root = build.get_repository_root()

    artifact_path = build.create_build_artifact(repo_root, tmp_path)
    artifact_digest = build.calculate_sha256(artifact_path)
    evidence = build.build_evidence(artifact_path, artifact_digest)
    build.write_evidence_file(evidence, tmp_path)

    return tmp_path


def test_generate_provenance_creates_valid_json_file(dist_dir):
    """The provenance file should exist and be parseable JSON."""
    statement = generate_provenance.generate_provenance(dist_dir)
    provenance_path = generate_provenance.write_provenance_file(statement, dist_dir)

    assert provenance_path.exists()
    with open(provenance_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)  # raises if not valid JSON

    assert loaded == statement


def test_provenance_uses_slsa_v1_statement_shape(dist_dir):
    """The statement must be a well-formed in-toto/SLSA v1 document."""
    statement = generate_provenance.generate_provenance(dist_dir)

    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    assert "subject" in statement
    assert "predicate" in statement
    assert "buildDefinition" in statement["predicate"]
    assert "runDetails" in statement["predicate"]


def test_provenance_subject_digest_present(dist_dir):
    """The provenance subject must record a SHA-256 artifact digest."""
    statement = generate_provenance.generate_provenance(dist_dir)
    subject = statement["subject"][0]

    assert subject["name"] == "buildguard-artifact.zip"
    assert "sha256" in subject["digest"]
    assert len(subject["digest"]["sha256"]) == 64  # hex-encoded SHA-256


def test_provenance_digest_matches_actual_artifact_on_disk(dist_dir):
    """The subject digest must match the real artifact's real digest.

    This is the core "no fabricated values" check: it independently
    recomputes the SHA-256 of the artifact file and compares it to
    what ended up in the provenance, rather than trusting either the
    evidence file or the provenance blindly.
    """
    statement = generate_provenance.generate_provenance(dist_dir)
    subject_digest = statement["subject"][0]["digest"]["sha256"]

    artifact_path = dist_dir / "buildguard-artifact.zip"
    actual_digest = generate_provenance.calculate_sha256(artifact_path)

    assert subject_digest == actual_digest


def test_provenance_matches_same_build_run_as_evidence(dist_dir):
    """The provenance and build-evidence.json must describe the same build."""
    with open(dist_dir / "build-evidence.json", "r", encoding="utf-8") as f:
        evidence = json.load(f)

    statement = generate_provenance.generate_provenance(dist_dir)
    subject = statement["subject"][0]

    assert subject["name"] == evidence["artifact_filename"]
    assert subject["digest"]["sha256"] == evidence["artifact_sha256"]


def test_provenance_source_fields_populated_from_github_env(dist_dir, monkeypatch):
    """When GitHub Actions env vars are present, source info must be filled in.

    This simulates running inside GitHub Actions by setting the same
    environment variables GitHub Actions provides, then re-running the
    full build -> evidence -> provenance pipeline so the values flow
    through end to end (nothing is hardcoded in the test itself).
    """
    monkeypatch.setenv("GITHUB_REPOSITORY", "example-org/buildguard")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456789")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "example-org/buildguard/.github/workflows/ci.yml@refs/heads/main")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setenv("RUNNER_ARCH", "X64")
    monkeypatch.setenv("GITHUB_JOB", "test")

    repo_root = build.get_repository_root()
    artifact_path = build.create_build_artifact(repo_root, dist_dir)
    artifact_digest = build.calculate_sha256(artifact_path)
    evidence = build.build_evidence(artifact_path, artifact_digest)
    build.write_evidence_file(evidence, dist_dir)

    statement = generate_provenance.generate_provenance(dist_dir)
    predicate = statement["predicate"]

    resolved_deps = predicate["buildDefinition"]["resolvedDependencies"]
    assert resolved_deps[0]["digest"]["gitCommit"] == "a" * 40
    assert "example-org/buildguard" in resolved_deps[0]["uri"]

    external_params = predicate["buildDefinition"]["externalParameters"]
    assert external_params["workflow"]["repository"] == "https://github.com/example-org/buildguard"

    run_details = predicate["runDetails"]
    assert run_details["metadata"]["invocationId"] == (
        "https://github.com/example-org/buildguard/actions/runs/123456789"
    )


def test_provenance_fields_are_null_when_run_locally(dist_dir, monkeypatch):
    """Outside GitHub Actions, GitHub-only fields must be null, not fabricated."""
    for var in (
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_REF",
        "GITHUB_SERVER_URL",
        "GITHUB_WORKFLOW_REF",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "GITHUB_JOB",
    ):
        monkeypatch.delenv(var, raising=False)

    repo_root = build.get_repository_root()
    artifact_path = build.create_build_artifact(repo_root, dist_dir)
    artifact_digest = build.calculate_sha256(artifact_path)
    evidence = build.build_evidence(artifact_path, artifact_digest)
    build.write_evidence_file(evidence, dist_dir)

    statement = generate_provenance.generate_provenance(dist_dir)
    predicate = statement["predicate"]

    # No source commit info available locally -> no fabricated dependency.
    assert predicate["buildDefinition"]["resolvedDependencies"] == []
    assert predicate["buildDefinition"]["externalParameters"]["workflow"]["repository"] is None
    assert predicate["runDetails"]["metadata"]["invocationId"] is None

    # The artifact itself still exists locally and still has a real digest.
    assert statement["subject"][0]["digest"]["sha256"]


def test_generate_provenance_missing_evidence_raises(tmp_path):
    """Provenance generation must refuse to run without real build evidence."""
    with pytest.raises(FileNotFoundError):
        generate_provenance.generate_provenance(tmp_path)


def test_generate_provenance_detects_digest_mismatch(dist_dir):
    """If the artifact on disk doesn't match recorded evidence, refuse."""
    evidence_path = dist_dir / "build-evidence.json"
    with open(evidence_path, "r", encoding="utf-8") as f:
        evidence = json.load(f)

    evidence["artifact_sha256"] = "0" * 64  # tamper with the recorded digest

    with open(evidence_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f)

    with pytest.raises(ValueError, match="digest mismatch"):
        generate_provenance.generate_provenance(dist_dir)
