"""Regression tests for source identity and packaging exclusions."""

import json
import subprocess
import zipfile

import pytest

from scripts import build


def make_project(tmp_path, name="proj"):
    project = tmp_path / name
    project.mkdir()
    (project / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (project / "test_app.py").write_text(
        "from app import f\n\ndef test_f():\n    assert f() == 1\n",
        encoding="utf-8",
    )
    return project


def packaged_names(project):
    files = build.collect_files_to_package(project, external_project=True)
    return {path.relative_to(project).as_posix() for path in files}


@pytest.fixture()
def no_git_discovery(tmp_path, monkeypatch):
    """Stop git from discovering a repository above tmp_path."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


# --- Fix 1: projects without Git metadata -------------------------------

def test_source_identity_has_every_key_without_git(tmp_path, no_git_discovery):
    project = make_project(tmp_path)

    identity = build.get_source_identity(project)

    assert identity["source_type"] == "local"
    assert identity["local_commit_sha"] is None
    assert identity["repository_url"] is None
    assert identity["source_dirty"] is None
    assert identity["source_uri"] is None


def test_build_evidence_works_for_project_without_git(tmp_path, no_git_discovery):
    project = make_project(tmp_path)
    artifact = build.create_build_artifact(project, project / "dist")
    digest = build.calculate_sha256(artifact)

    evidence = build.build_evidence(artifact, digest, project_dir=project)

    assert evidence["local_commit_sha"] is None
    assert evidence["repository_url"] is None
    assert evidence["source_type"] == "local"
    assert evidence["artifact_sha256"] == digest


def test_main_builds_project_without_git(tmp_path, monkeypatch, no_git_discovery):
    project = make_project(tmp_path)
    monkeypatch.setattr("sys.argv", ["build.py", "--project-dir", str(project)])

    assert build.main() == 0

    evidence = json.loads((project / "dist" / build.EVIDENCE_FILENAME).read_text())
    assert evidence["local_commit_sha"] is None
    assert (project / "dist" / build.ARTIFACT_FILENAME).is_file()


def _git(project, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=project, check=True, capture_output=True,
    )


def test_source_identity_reports_clean_and_dirty_git_tree(tmp_path):
    project = make_project(tmp_path)
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "init")

    clean = build.get_source_identity(project)
    assert clean["source_type"] == "git"
    assert len(clean["local_commit_sha"]) == 40
    assert clean["source_dirty"] is False

    (project / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert build.get_source_identity(project)["source_dirty"] is True


def test_remote_url_credentials_are_removed():
    cleaned = build.sanitize_remote_url("https://user:token@github.com/o/r.git")
    assert cleaned == "https://github.com/o/r.git"
    assert "token" not in cleaned


# --- Fix 2: packaging exclusions ----------------------------------------

def test_nested_build_and_dist_source_directories_are_packaged(tmp_path):
    project = make_project(tmp_path)
    for rel in ("src/build/__init__.py", "src/dist/tool.py", "pkg/build/x.py"):
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X = 1\n", encoding="utf-8")

    names = packaged_names(project)

    assert "src/build/__init__.py" in names
    assert "src/dist/tool.py" in names
    assert "pkg/build/x.py" in names


def test_root_level_generated_output_directories_are_still_excluded(tmp_path):
    project = make_project(tmp_path)
    for rel in ("build/lib/a.py", "dist/old.zip"):
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated", encoding="utf-8")

    names = packaged_names(project)

    assert not any(n.startswith(("build/", "dist/")) for n in names)
    assert "app.py" in names


def test_caches_and_environments_are_excluded_at_any_depth(tmp_path):
    project = make_project(tmp_path)
    for rel in (
        ".git/config", ".venv/bin/python", "venv/x.py",
        "pkg/__pycache__/m.py", "pkg/.pytest_cache/n", "node_modules/p/i.js",
        "pkg.egg-info/PKG-INFO", ".mypy_cache/m", ".tox/t",
    ):
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ignored", encoding="utf-8")

    assert packaged_names(project) == {"app.py", "test_app.py"}


def test_secrets_are_still_excluded_inside_nested_source_directories(tmp_path):
    project = make_project(tmp_path)
    nested = project / "src" / "build"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("X = 1\n", encoding="utf-8")
    (nested / ".env").write_text("SECRET=1", encoding="utf-8")
    (nested / "server.pem").write_text("key", encoding="utf-8")

    names = packaged_names(project)

    assert "src/build/module.py" in names
    assert "src/build/.env" not in names
    assert "src/build/server.pem" not in names


def test_symlinked_directory_contents_are_not_packaged(tmp_path):
    project = make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    try:
        (project / "linked_dir").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this system")

    assert not any(n.startswith("linked_dir") for n in packaged_names(project))


def test_custom_dist_directory_inside_project_is_not_packaged(tmp_path):
    project = make_project(tmp_path)
    custom = project / "out"
    custom.mkdir()
    (custom / "previous.zip").write_bytes(b"old")

    files = build.collect_files_to_package(
        project, external_project=True, dist_dir=custom
    )
    names = {p.relative_to(project).as_posix() for p in files}

    assert "out/previous.zip" not in names
    assert "app.py" in names


def test_artifact_zip_contains_nested_build_package(tmp_path):
    project = make_project(tmp_path)
    (project / "src" / "build").mkdir(parents=True)
    (project / "src" / "build" / "__init__.py").write_text("", encoding="utf-8")

    artifact = build.create_build_artifact(project, project / "dist")

    with zipfile.ZipFile(artifact) as archive:
        assert "src/build/__init__.py" in archive.namelist()


def test_untracked_project_inside_another_repo_is_not_treated_as_git(tmp_path):
    outer = make_project(tmp_path, "outer")
    _git(outer, "init", "-q")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "init")

    inner = outer / "projects" / "inner"
    inner.mkdir(parents=True)
    (inner / "app.py").write_text("X = 1\n", encoding="utf-8")

    identity = build.get_source_identity(inner)

    assert identity["source_type"] == "local"
    assert identity["local_commit_sha"] is None


def test_tracked_subdirectory_project_uses_enclosing_repository(tmp_path):
    outer = make_project(tmp_path, "mono")
    sub = outer / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "app.py").write_text("X = 1\n", encoding="utf-8")
    _git(outer, "init", "-q")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "init")

    identity = build.get_source_identity(sub)

    assert identity["source_type"] == "git"
    assert len(identity["local_commit_sha"]) == 40
    assert identity["source_dirty"] is False
