"""Tests for scripts/evaluate_assurance.py.

Verification reports are built in memory / in temporary directories, so
the real dist/ directory is never touched.
"""

import json

import pytest

from scripts import evaluate_assurance as ea


ALL_PASS = {
    "artifact_exists": "PASS",
    "artifact_filename_match": "PASS",
    "artifact_digest_match": "PASS",
    "build_status": "PASS",
    "tests_status": "PASS",
    "provenance_structure": "PASS",
    "provenance_artifact_match": "PASS",
    "source_commit_match": "PASS",
}


def make_report(**overrides):
    """A verification-report dict with all checks PASS, plus overrides."""
    checks = {**ALL_PASS, **overrides}
    status = "FAIL" if "FAIL" in checks.values() else "PASS"
    return {
        "verification_status": status,
        "checks": checks,
        "details": {},
    }


def statuses(report):
    return {r["id"]: r["status"] for r in report["requirements"]}


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def mock_fresh_verification(monkeypatch, report):
    """Make the evaluator receive a controlled fresh-verification result."""
    monkeypatch.setattr(
        ea.verify_evidence,
        "verify",
        lambda _dist_dir: report,
    )


# --- all supported -----------------------------------------------------


def test_all_requirements_supported_accepts():
    report = ea.evaluate(make_report())
    result = statuses(report)

    assert report["final_decision"] == "ACCEPT"
    for requirement_id in ("R1", "R2", "R3", "R4", "R5"):
        assert result[requirement_id] == "SUPPORTED"
    assert result["R6"] == "UNSUPPORTED"


# --- STOP cases --------------------------------------------------------


def test_r1_failure_stops():
    report = ea.evaluate(make_report(artifact_digest_match="FAIL"))

    assert statuses(report)["R1"] == "UNSUPPORTED"
    assert report["final_decision"] == "STOP"


@pytest.mark.parametrize(
    "check",
    ["provenance_structure", "provenance_artifact_match"],
)
def test_r3_failure_stops(check):
    report = ea.evaluate(make_report(**{check: "FAIL"}))

    assert statuses(report)["R3"] == "UNSUPPORTED"
    assert report["final_decision"] == "STOP"


def test_stop_takes_priority_over_revise():
    report = ea.evaluate(
        make_report(
            artifact_digest_match="FAIL",
            source_commit_match="SKIP",
        )
    )

    assert report["final_decision"] == "STOP"


# --- REVISE cases ------------------------------------------------------


@pytest.mark.parametrize(
    "check",
    ["artifact_filename_match", "build_status", "tests_status"],
)
def test_r2_failure_revises(check):
    report = ea.evaluate(make_report(**{check: "FAIL"}))
    result = statuses(report)

    assert result["R2"] == "UNSUPPORTED"
    assert result["R1"] == "SUPPORTED"
    assert result["R3"] == "SUPPORTED"
    assert report["final_decision"] == "REVISE"


def test_r4_failure_revises():
    report = ea.evaluate(make_report(tests_status="FAIL"))
    result = statuses(report)

    assert result["R4"] == "UNSUPPORTED"
    assert report["final_decision"] == "REVISE"


def test_r5_skip_is_unsupported_and_revises():
    report = ea.evaluate(make_report(source_commit_match="SKIP"))
    requirement = next(
        item for item in report["requirements"] if item["id"] == "R5"
    )

    assert requirement["status"] == "UNSUPPORTED"
    assert "SKIP" in requirement["explanation"]
    assert report["final_decision"] == "REVISE"


def test_r5_fail_revises():
    report = ea.evaluate(make_report(source_commit_match="FAIL"))

    assert statuses(report)["R5"] == "UNSUPPORTED"
    assert report["final_decision"] == "REVISE"


def test_missing_check_is_not_treated_as_pass():
    checks = dict(ALL_PASS)
    del checks["source_commit_match"]

    report = ea.evaluate({"checks": checks})

    assert statuses(report)["R5"] == "UNSUPPORTED"
    assert report["final_decision"] == "REVISE"


def test_non_pass_values_are_not_treated_as_pass():
    report = ea.evaluate(make_report(artifact_digest_match="pass"))

    assert statuses(report)["R1"] == "UNSUPPORTED"
    assert report["final_decision"] == "STOP"


# --- R6 never affects the decision ------------------------------------


def test_r6_unsupported_does_not_block_accept():
    report = ea.evaluate(make_report())

    assert statuses(report)["R6"] == "UNSUPPORTED"
    assert report["final_decision"] == "ACCEPT"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, "ACCEPT"),
        ({"source_commit_match": "SKIP"}, "REVISE"),
        ({"artifact_digest_match": "FAIL"}, "STOP"),
    ],
)
def test_r6_supported_does_not_change_decision(overrides, expected):
    without_tamper = ea.evaluate(make_report(**overrides))
    with_tamper = ea.evaluate(
        make_report(**overrides),
        tamper_result={"tamper_detected": True},
    )

    assert statuses(without_tamper)["R6"] == "UNSUPPORTED"
    assert statuses(with_tamper)["R6"] == "SUPPORTED"
    assert without_tamper["final_decision"] == expected
    assert with_tamper["final_decision"] == expected


@pytest.mark.parametrize(
    "tamper_result",
    [
        {},
        {"tamper_detected": False},
        {"tamper_detected": "true"},
        {"tamper_detected": 1},
        [],
        "yes",
    ],
)
def test_r6_requires_explicit_boolean_true(tamper_result):
    report = ea.evaluate(
        make_report(),
        tamper_result=tamper_result,
    )

    assert statuses(report)["R6"] == "UNSUPPORTED"


def test_r6_is_not_mandatory_in_report():
    report = ea.evaluate(make_report())
    requirement = next(
        item for item in report["requirements"] if item["id"] == "R6"
    )

    assert requirement["mandatory"] is False


# --- missing or malformed underlying verification inputs ---------------


def test_missing_verification_inputs_stop(tmp_path):
    """Without real build inputs, fresh verification must not accept."""
    exit_code = ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert report["final_decision"] == "STOP"
    assert all(
        item["status"] == "UNSUPPORTED"
        for item in report["requirements"]
    )
    assert "not found" in report["notes"][0].lower()


def test_saved_invalid_json_report_does_not_override_fresh_verification(
    tmp_path, monkeypatch
):
    """The evaluator must use fresh verification, not parse the saved report."""
    (tmp_path / "verification-report.json").write_text(
        "{not json",
        encoding="utf-8",
    )
    mock_fresh_verification(
        monkeypatch,
        make_report(artifact_digest_match="FAIL"),
    )

    exit_code = ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert report["final_decision"] == "STOP"
    assert statuses(report)["R1"] == "UNSUPPORTED"


@pytest.mark.parametrize(
    "content",
    [
        [],
        "text",
        42,
        None,
        {},
        {"checks": "nope"},
        {"checks": []},
    ],
)
def test_saved_wrong_shape_report_does_not_override_fresh_verification(
    tmp_path, monkeypatch, content
):
    write_json(tmp_path / "verification-report.json", content)
    mock_fresh_verification(
        monkeypatch,
        make_report(artifact_digest_match="FAIL"),
    )

    exit_code = ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert report["final_decision"] == "STOP"
    assert statuses(report)["R1"] == "UNSUPPORTED"


def test_non_pass_verification_status_cannot_accept(
    tmp_path, monkeypatch
):
    """A non-PASS overall status must never produce ACCEPT."""
    report = make_report()
    report["verification_status"] = "FAIL"

    mock_fresh_verification(monkeypatch, report)

    exit_code = ea.run(tmp_path)
    assurance = json.loads(
        (tmp_path / "assurance-report.json").read_text(
            encoding="utf-8"
        )
    )

    assert exit_code == 1
    assert assurance["final_decision"] == "STOP"
    assert "Failing safe" in assurance["notes"][0]


# --- exit codes and report output --------------------------------------


def test_exit_code_zero_only_for_accept(tmp_path, monkeypatch):
    mock_fresh_verification(monkeypatch, make_report())

    assert ea.run(tmp_path) == 0


def test_exit_code_one_for_revise(tmp_path, monkeypatch):
    mock_fresh_verification(
        monkeypatch,
        make_report(source_commit_match="SKIP"),
    )

    assert ea.run(tmp_path) == 1


def test_exit_code_one_for_stop(tmp_path, monkeypatch):
    mock_fresh_verification(
        monkeypatch,
        make_report(artifact_digest_match="FAIL"),
    )

    assert ea.run(tmp_path) == 1


def test_report_structure_and_no_slsa_level_claim(tmp_path, monkeypatch):
    mock_fresh_verification(monkeypatch, make_report())

    ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert report["final_decision"] == "ACCEPT"
    assert [item["id"] for item in report["requirements"]] == [
        "R1", "R2", "R3", "R4", "R5", "R6"
    ]

    for requirement in report["requirements"]:
        for key in (
            "id",
            "name",
            "status",
            "checks_used",
            "explanation",
        ):
            assert key in requirement
        assert requirement["status"] in ("SUPPORTED", "UNSUPPORTED")

    assert "slsa_level" not in report
    assert "not a certification" in report["disclaimer"]


def test_run_reads_explicit_tamper_result_file(tmp_path, monkeypatch):
    mock_fresh_verification(monkeypatch, make_report())
    write_json(
        tmp_path / "tamper-test-report.json",
        {"tamper_detected": True},
    )

    exit_code = ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert statuses(report)["R6"] == "SUPPORTED"


def test_run_malformed_tamper_file_leaves_r6_unsupported(
    tmp_path, monkeypatch
):
    mock_fresh_verification(monkeypatch, make_report())
    (tmp_path / "tamper-test-report.json").write_text(
        "{broken",
        encoding="utf-8",
    )

    exit_code = ea.run(tmp_path)
    report = json.loads(
        (tmp_path / "assurance-report.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0  # R6 never changes the decision
    assert statuses(report)["R6"] == "UNSUPPORTED"
