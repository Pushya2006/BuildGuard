import zipfile


from scripts import build


def create_project(tmp_path):
    """Create a small external project for isolated build tests."""
    project = tmp_path / "sample_project"
    project.mkdir()
    (project / "app.py").write_text(
        "def hello():\n    return 'hello'\n",
        encoding="utf-8",
    )
    (project / "test_app.py").write_text(
        "from app import hello\n\n"
        "def test_hello():\n"
        "    assert hello() == 'hello'\n",
        encoding="utf-8",
    )
    return project


def test_secret_files_are_excluded_from_packaging(tmp_path):
    project = create_project(tmp_path)
    (project / ".env").write_text("SECRET=value", encoding="utf-8")
    (project / ".env.production").write_text(
        "SECRET=value", encoding="utf-8"
    )
    (project / "credentials").write_text(
        "SECRET=value", encoding="utf-8"
    )
    (project / "secrets.json").write_text(
        '{"token": "secret"}', encoding="utf-8"
    )
    (project / "private.key").write_text(
        "private key", encoding="utf-8"
    )
    (project / ".env.example").write_text(
        "EXAMPLE=value", encoding="utf-8"
    )

    files = build.collect_files_to_package(project, external_project=True)
    names = {path.relative_to(project).as_posix() for path in files}

    assert ".env" not in names
    assert ".env.production" not in names
    assert "credentials" not in names
    assert "secrets.json" not in names
    assert "private.key" not in names
    assert ".env.example" in names


def test_generated_files_and_dist_are_excluded(tmp_path):
    project = create_project(tmp_path)
    (project / "generated.pyc").write_bytes(b"bytecode")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "cached.py").write_text(
        "ignored", encoding="utf-8"
    )
    (project / "dist").mkdir()
    (project / "dist" / "old-artifact.zip").write_bytes(b"old")

    files = build.collect_files_to_package(project, external_project=True)
    names = {path.relative_to(project).as_posix() for path in files}

    assert "generated.pyc" not in names
    assert "__pycache__/cached.py" not in names
    assert "dist/old-artifact.zip" not in names


def test_external_project_artifact_contains_project_files(tmp_path):
    project = create_project(tmp_path)
    dist_dir = project / "dist"

    artifact_path = build.create_build_artifact(project, dist_dir)

    assert artifact_path.is_file()

    with zipfile.ZipFile(artifact_path) as archive:
        names = set(archive.namelist())

    assert "app.py" in names
    assert "test_app.py" in names
    assert not any(name.startswith("dist/") for name in names)


def test_build_evidence_records_actual_artifact_digest(tmp_path):
    project = create_project(tmp_path)
    artifact_path = build.create_build_artifact(project, project / "dist")
    digest = build.calculate_sha256(artifact_path)

    evidence = build.build_evidence(
        artifact_path,
        digest,
        tests_status="passed",
    )

    assert evidence["artifact_filename"] == artifact_path.name
    assert evidence["artifact_sha256"] == digest
    assert evidence["tests_status"] == "passed"
    assert evidence["build_status"] == "success"



def test_failed_project_tests_stop_build(tmp_path, monkeypatch):
    project = create_project(tmp_path)
    (project / "test_app.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    dist_dir = project / "dist"


    monkeypatch.setattr(
        "sys.argv",
        ["build.py", "--project-dir", str(project)],
    )

    exit_code = build.main()

    assert exit_code == 1
    assert not (dist_dir / build.ARTIFACT_FILENAME).exists()
    assert not (dist_dir / build.EVIDENCE_FILENAME).exists()

def test_external_project_does_not_package_symlink_outside_project(tmp_path):
    project = create_project(tmp_path)
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("SECRET=value", encoding="utf-8")

    linked_file = project / "linked_secret.txt"

    try:
        linked_file.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("Symbolic links are unavailable on this system")

    files = build.collect_files_to_package(project, external_project=True)
    names = {path.relative_to(project).as_posix() for path in files}

    assert "linked_secret.txt" not in names
def test_default_packaging_skips_symlink_source(tmp_path, monkeypatch):
    project = tmp_path / "buildguard"
    project.mkdir()

    outside_file = tmp_path / "outside.py"
    outside_file.write_text(
        "SECRET = 'outside'\n",
        encoding="utf-8",
    )

    (project / "app.py").write_text(
        "VALUE = 'inside'\n",
        encoding="utf-8",
    )

    linked_source = project / "linked.py"
    try:
        linked_source.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("Symbolic links are unavailable on this system")

    monkeypatch.setattr(
        build,
        "ARTIFACT_SOURCE_PATHS",
        ["app.py", "linked.py"],
    )

    files = build.collect_files_to_package(project)

    assert project / "app.py" in files
    assert linked_source not in files