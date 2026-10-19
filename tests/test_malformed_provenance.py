import json

import pytest

from scripts import verify_evidence


@pytest.mark.parametrize(
    "provenance",
    [
        [],
        "invalid-structure",
        42,
        None,
    ],
)
def test_malformed_top_level_provenance_returns_failure(
    tmp_path, provenance
):
    """Non-object provenance JSON must fail without crashing."""
    provenance_path = (
        tmp_path / verify_evidence.PROVENANCE_FILENAME
    )
    provenance_path.write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )

    report = verify_evidence.verify(tmp_path)

    assert report["verification_status"] == "FAIL"
    assert (
        report["checks"]["provenance_structure"] == "FAIL"
    )
    assert (
        report["checks"]["provenance_artifact_match"] == "FAIL"
    )


@pytest.mark.parametrize(
    "provenance",
    [
        {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [
                {
                    "name": "buildguard-artifact.zip",
                    "digest": {"sha256": "0" * 64},
                }
            ],
            "predicate": "unexpected-string",
        },
        {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": ["unexpected-string"],
            "predicate": {},
        },
        {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [
                {
                    "name": "buildguard-artifact.zip",
                    "digest": "unexpected-string",
                }
            ],
            "predicate": {},
        },
    ],
)
def test_malformed_nested_provenance_returns_failure(
    tmp_path, provenance
):
    """Malformed nested provenance data must fail safely."""
    provenance_path = (
        tmp_path / verify_evidence.PROVENANCE_FILENAME
    )
    provenance_path.write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )

    report = verify_evidence.verify(tmp_path)

    assert report["verification_status"] == "FAIL"
    assert (
        report["checks"]["provenance_artifact_match"] == "FAIL"
    )