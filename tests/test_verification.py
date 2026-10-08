"""Tests for scripts/verify_evidence.py.

Each test builds a complete, valid set of artifact + build-evidence.json
+ provenance.json in a temporary directory (using the real build and
provenance generation code, so nothing here is hand-faked), then
tampers with exactly one thing before calling verify_evidence.verify().
The real dist/ directory is never touched.
"""

import json
import zipfile

import pytest

from scripts import build, generate_provenance, verify_evidence


GITHUB_ENV = {
    "GITHUB_REPOSITORY": "example-org/buildguard",
    "GITHUB_SHA": "b" * 40,
    "GITHUB_RUN_ID": "555000111",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_WORKFLOW_REF": "example-org/buildguard/.github/workflows/ci.yml@refs/heads/main",
    "RUNNER_OS": "Linux",
    "RUNNER_ARCH": "X64",
    "GITHUB_JOB": "test",
}


@pytest.fixture()
def valid_dist(tmp_path, monkeypatch):
    """Build a fully valid artifact + evidence + provenance set.

    Simulates a GitHub Actions run (so source_commit_match is
    exercised rather than skipped) and writes everything into
    tmp_path, using the real build.py / generate_provenance.py logic.
    """
    for key, value in GITHUB_ENV.items():
        monkeypatch.setenv(key, value)

    repo_root = build.get_repository_root()
    artifact_path = build.create_build_artifact(repo_root, tmp_path)
    artifact_digest = build.calculate_sha256(artifact_path)
    evidence = build.build_evidence(artifact_path, artifact_digest)
    build.write_evidence_file(evidence, tmp_path)

    statement = generate_provenance.generate_provenance(tmp_path)
    generate_provenance.write_provenance_file(statement, tmp_path)

    return tmp_path


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _evidence_path(dist_dir):
    return dist_dir / verify_evidence.EVIDENCE_FILENAME


def _provenance_path(dist_dir):
    return dist_dir / verify_evidence.PROVENANCE_FILENAME


def _artifact_path(dist_dir):
    return dist_dir / verify_evidence.ARTIFACT_FILENAME


# 1. Valid artifact/evidence/provenance -> PASS
def test_valid_evidence_passes(valid_dist):
    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "PASS"
    for name in verify_evidence.CHECK_NAMES:
        assert report["checks"][name] in ("PASS", "SKIP")
    # With GitHub env vars simulated, source_commit_match should
    # actually run (not be skipped).
    assert report["checks"]["source_commit_match"] == "PASS"


# 2. Modified artifact -> FAIL
def test_modified_artifact_fails(valid_dist):
    artifact_path = _artifact_path(valid_dist)
    with zipfile.ZipFile(artifact_path, "a") as archive:
        archive.writestr("tampered.txt", "unexpected extra content")

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["artifact_digest_match"] == "FAIL"
    assert report["checks"]["provenance_artifact_match"] == "FAIL"


# 3. Modified evidence digest -> FAIL
def test_modified_evidence_digest_fails(valid_dist):
    evidence_path = _evidence_path(valid_dist)
    evidence = _load(evidence_path)
    evidence["artifact_sha256"] = "0" * 64
    _save(evidence_path, evidence)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["artifact_digest_match"] == "FAIL"


# 4. Wrong artifact filename in evidence -> FAIL
def test_wrong_artifact_filename_fails(valid_dist):
    evidence_path = _evidence_path(valid_dist)
    evidence = _load(evidence_path)
    evidence["artifact_filename"] = "not-the-real-artifact.zip"
    _save(evidence_path, evidence)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["artifact_filename_match"] == "FAIL"


# 5. Failed build status -> FAIL
def test_failed_build_status_fails(valid_dist):
    evidence_path = _evidence_path(valid_dist)
    evidence = _load(evidence_path)
    evidence["build_status"] = "failure"
    _save(evidence_path, evidence)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["build_status"] == "FAIL"


# 6. Failed test status -> FAIL
def test_failed_tests_status_fails(valid_dist):
    evidence_path = _evidence_path(valid_dist)
    evidence = _load(evidence_path)
    evidence["tests_status"] = "failed"
    _save(evidence_path, evidence)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["tests_status"] == "FAIL"


# 7. Invalid provenance predicate type -> FAIL
def test_invalid_predicate_type_fails(valid_dist):
    provenance_path = _provenance_path(valid_dist)
    provenance = _load(provenance_path)
    provenance["predicateType"] = "https://example.com/not-slsa"
    _save(provenance_path, provenance)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["provenance_structure"] == "FAIL"


# 8. Wrong provenance artifact digest -> FAIL
def test_wrong_provenance_digest_fails(valid_dist):
    provenance_path = _provenance_path(valid_dist)
    provenance = _load(provenance_path)
    provenance["subject"][0]["digest"]["sha256"] = "1" * 64
    _save(provenance_path, provenance)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["provenance_artifact_match"] == "FAIL"


# 9. Wrong provenance artifact name -> FAIL
def test_wrong_provenance_name_fails(valid_dist):
    provenance_path = _provenance_path(valid_dist)
    provenance = _load(provenance_path)
    provenance["subject"][0]["name"] = "wrong-name.zip"
    _save(provenance_path, provenance)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["provenance_artifact_match"] == "FAIL"


# 10. Wrong source commit -> FAIL
def test_wrong_source_commit_fails(valid_dist):
    provenance_path = _provenance_path(valid_dist)
    provenance = _load(provenance_path)
    provenance["predicate"]["buildDefinition"]["resolvedDependencies"][0][
        "digest"
    ]["gitCommit"] = "c" * 40
    _save(provenance_path, provenance)

    report = verify_evidence.verify(valid_dist)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["source_commit_match"] == "FAIL"


# Extra: fail-safe behavior on missing files (not one of the numbered
# scenarios above, but directly required: "must fail safely rather
# than silently accepting inconsistent evidence").
def test_missing_artifact_fails_safely(tmp_path):
    report = verify_evidence.verify(tmp_path)

    assert report["verification_status"] == "FAIL"
    assert report["checks"]["artifact_exists"] == "FAIL"


def test_source_commit_check_skipped_when_no_source_info(tmp_path):
    """A local build with no repository/commit_sha should SKIP, not FAIL,
    the source_commit_match check (nothing to verify it against)."""
    repo_root = build.get_repository_root()
    artifact_path = build.create_build_artifact(repo_root, tmp_path)
    artifact_digest = build.calculate_sha256(artifact_path)
    evidence = build.build_evidence(artifact_path, artifact_digest)
    build.write_evidence_file(evidence, tmp_path)

    statement = generate_provenance.generate_provenance(tmp_path)
    generate_provenance.write_provenance_file(statement, tmp_path)

    report = verify_evidence.verify(tmp_path)

    assert report["checks"]["source_commit_match"] == "SKIP"
    # A skipped check should not, by itself, fail overall verification.
    assert report["verification_status"] == "PASS"


def test_report_written_to_disk_is_valid_json(valid_dist):
    report = verify_evidence.verify(valid_dist)
    report_path = verify_evidence.write_report(report, valid_dist)

    assert report_path.exists()
    with open(report_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    assert loaded == report
