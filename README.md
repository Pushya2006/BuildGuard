# BuildGuard

## Software Supply-Chain Assurance Through Build Evidence Verification

BuildGuard is a Python-based prototype that evaluates software build evidence to help identify artifact integrity problems, missing traceability information, and unsupported assurance claims.

It combines automated testing, artifact generation, SHA-256 digest verification, SLSA-format provenance, tamper detection, and rule-based assurance evaluation in a CI/CD workflow.

## Current Features

- Automated project testing with pytest.
- ZIP artifact generation and SHA-256 digest recording.
- Build evidence generation.
- SLSA Provenance v1-format document generation.
- Verification of artifact, evidence, and provenance consistency.
- Automated artifact tamper-detection experiment.
- Rule-based assurance evaluation.
- JSON reports for verification, assurance, and tamper detection.
- Optional building and evidence processing for an external Python project.

## Architecture

The pipeline follows these stages:

1. Run project tests.
2. Build the artifact and collect build evidence.
3. Generate provenance describing the artifact and build.
4. Verify the artifact, evidence, and provenance.
5. Run a tamper-detection experiment against a temporary artifact copy.
6. Evaluate the assurance requirements.
7. Produce reports and upload artifacts through GitHub Actions.

## Requirements

- Python 3.12 or compatible Python version.
- pip.
- Git for repository-based development.
- GitHub account for running the configured GitHub Actions workflow.

## Setup (Windows PowerShell)

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run Tests

```powershell
python -m pytest -q
```

## Run the Local BuildGuard Pipeline

Run these commands in order:

```powershell
python scripts/build.py
python scripts/generate_provenance.py
python scripts/verify_evidence.py
python scripts/test_tamper_detection.py
python scripts/evaluate_assurance.py
```

The build and verification scripts use the repository's `dist/` directory by default.

The tamper-detection script modifies only a temporary copy of the artifact. It does not intentionally modify the original artifact.

## Generated Files

The pipeline creates the following files in `dist/`:

| File | Purpose |
|---|---|
| `buildguard-artifact.zip` | Generated software artifact |
| `build-evidence.json` | Build metadata, test status, and artifact digest |
| `provenance.json` | SLSA Provenance v1-format statement |
| `verification-report.json` | Individual evidence verification results |
| `tamper-test-report.json` | Results of the tamper-detection experiment |
| `assurance-report.json` | Assurance requirements and final decision |

## Evidence Verification

The verifier checks:

- Whether the artifact exists.
- Whether the artifact filename matches the evidence.
- Whether the independently calculated SHA-256 digest matches.
- Whether the recorded build and test statuses indicate success.
- Whether the provenance document has the expected structure.
- Whether the provenance subject matches the artifact and digest.
- Whether source repository and commit information can be verified when available.

The verification report records each check as `PASS`, `FAIL`, or `SKIP`.

A `SKIP` means that a check could not be performed with the available evidence. It does not mean that the missing evidence has been verified.

## Assurance Evaluation

BuildGuard evaluates six requirements:

| ID | Requirement |
|---|---|
| R1 | Artifact integrity |
| R2 | Build evidence integrity |
| R3 | Provenance integrity |
| R4 | Test evidence |
| R5 | Source traceability |
| R6 | Tamper detection |

The evaluator produces one of three decisions:

- **ACCEPT** — the mandatory assurance requirements are supported.
- **REVISE** — required evidence is missing or unsupported, but the conditions for stopping have not been met.
- **STOP** — a blocking integrity or assurance condition requires the process to stop.

R6 is informational and does not independently determine the final decision. The assurance report provides the requirement statuses and decision reason.

## Testing an External Python Project

BuildGuard can run tests and package a supported external Python project.

For example, to build the sample project in a separate directory:

```powershell
python scripts/build.py --project-dir "$HOME\OneDrive\Desktop\BuildGuard-External-Test"
```

Generate provenance for that project's output:

```powershell
python scripts/generate_provenance.py --dist-dir "$HOME\OneDrive\Desktop\BuildGuard-External-Test\dist"
```

Verify the evidence:

```powershell
python scripts/verify_evidence.py --dist-dir "$HOME\OneDrive\Desktop\BuildGuard-External-Test\dist"
```

Run tamper detection:

```powershell
python scripts/test_tamper_detection.py --dist-dir "$HOME\OneDrive\Desktop\BuildGuard-External-Test\dist"
```

Evaluate assurance:

```powershell
python scripts/evaluate_assurance.py --dist-dir "$HOME\OneDrive\Desktop\BuildGuard-External-Test\dist"
```

These commands assume that the external project has a compatible pytest test suite and the files expected by the build process. The build uses the currently active Python environment; it does not provide complete dependency isolation for arbitrary projects.

## GitHub Actions CI/CD

The workflow is configured in `.github/workflows/ci.yml`.

It runs on pushes to the `main` branch and performs the following steps:

1. Checks out the repository.
2. Sets up Python 3.12.
3. Installs project dependencies.
4. Runs the tests.
5. Builds the artifact and collects evidence.
6. Generates provenance.
7. Verifies the evidence and provenance.
8. Runs tamper detection.
9. Evaluates assurance.
10. Uploads the artifact and JSON reports.

The workflow attempts to upload each artifact and report even if an earlier step fails. Only files that were successfully generated and are available can be uploaded. You can inspect the available artifacts and reports from the corresponding GitHub Actions workflow run.

## Important Limitations

- The generated provenance is not cryptographically signed.
- The verifier checks document structure and consistency; it does not establish provenance authenticity or a trusted builder.
- A local build does not automatically have GitHub Actions repository, commit, or workflow metadata. Source traceability may therefore be skipped or unsupported.
- The tamper-detection experiment checks whether a modified artifact is detected against existing evidence. It does not prove resistance to every possible attack.
- The assurance decision reflects the project's implemented rules and available evidence. It is not a security certification.

## SLSA Alignment

BuildGuard generates an in-toto Statement using the SLSA Provenance v1 predicate type.

This is a format-alignment claim only. BuildGuard does not claim SLSA certification or achievement of a specific SLSA build level.

## Project Goal

The goal is to demonstrate how build evidence can be collected, checked, and evaluated systematically within a software supply-chain workflow, making missing or inconsistent evidence visible before an artifact is accepted.