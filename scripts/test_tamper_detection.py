"""Automated tamper-detection experiment for BuildGuard."""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import verify_evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test whether BuildGuard detects artifact tampering."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "dist",
        help="Directory containing build evidence and provenance.",
    )
    args = parser.parse_args()
    dist_dir = args.dist_dir.resolve()

    artifact_name = "buildguard-artifact.zip"
    evidence_name = "build-evidence.json"
    provenance_name = "provenance.json"

    required_files = [
        dist_dir / artifact_name,
        dist_dir / evidence_name,
        dist_dir / provenance_name,
    ]

    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        print("Tamper test cannot run; required files are missing:")
        for path in missing:
            print(f"  - {path}")
        return 1

    try:
        with tempfile.TemporaryDirectory(
            prefix="buildguard-tamper-"
        ) as temp:
            test_dir = Path(temp)

            for filename in (
                artifact_name,
                evidence_name,
                provenance_name,
            ):
                shutil.copy2(
                    dist_dir / filename,
                    test_dir / filename,
                )

            # Modify only the temporary artifact copy.
            with open(test_dir / artifact_name, "ab") as artifact:
                artifact.write(b"\nBUILDGUARD_TAMPER_TEST")

            report = verify_evidence.verify(test_dir)
            checks = report.get("checks", {})

            digest_failed = (
                checks.get("artifact_digest_match") == "FAIL"
            )
            provenance_failed = (
                checks.get("provenance_artifact_match") == "FAIL"
            )
            detected = digest_failed and provenance_failed

            result = {
                "tamper_detected": detected,
                "verification_status_after_tampering": report.get(
                    "verification_status"
                ),
                "artifact_digest_match": checks.get(
                    "artifact_digest_match", "MISSING"
                ),
                "provenance_artifact_match": checks.get(
                    "provenance_artifact_match", "MISSING"
                ),
                "test_method": (
                    "Modified a temporary artifact copy and ran the "
                    "existing evidence verifier against the copied "
                    "evidence and provenance."
                ),
            }

        output_path = dist_dir / "tamper-test-report.json"
        output_path.write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )

        print(f"Tamper-test report written: {output_path}")
        print(f"Tamper detected: {result['tamper_detected']}")
        print(
            "Artifact digest check: "
            f"{result['artifact_digest_match']}"
        )
        print(
            "Provenance artifact check: "
            f"{result['provenance_artifact_match']}"
        )

        return 0 if detected else 1

    except OSError as exc:
        print(f"Tamper test failed due to a file operation error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
