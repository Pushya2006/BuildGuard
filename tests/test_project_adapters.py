"""Tests for project discovery, validation, commands and outcome rules."""

import os
import sys
from pathlib import Path

import pytest

from scripts import project_adapters as pa


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# --------------------------------------------------------------------------
# Path validation
# --------------------------------------------------------------------------

def test_valid_project_path_returns_resolved_directory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert pa.validate_project_path(str(project)) == project.resolve()


@pytest.mark.parametrize("raw", ["", "   ", None, 5])
def test_empty_or_non_string_path_is_rejected(raw):
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(raw)
    assert exc.value.code == "invalid_path"


def test_relative_dotdot_nul_and_unc_paths_are_rejected(tmp_path):
    cases = {
        "relative/dir": "invalid_path",
        str(tmp_path / ".." / "x"): "invalid_path",
        str(tmp_path) + "\x00": "invalid_path",
        "\\\\server\\share\\proj": "unsafe_location",
        "//server/share/proj": "unsafe_location",
    }
    for raw, code in cases.items():
        with pytest.raises(pa.ProjectError) as exc:
            pa.validate_project_path(raw)
        assert exc.value.code == code, raw


def test_missing_and_file_paths_are_rejected(tmp_path):
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(tmp_path / "nope"))
    assert exc.value.code == "not_found"

    file_path = write(tmp_path / "file.txt", "x")
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(file_path))
    assert exc.value.code == "not_a_directory"


def test_symlink_root_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this system")
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(link))
    assert exc.value.code == "symlink_root"


def test_root_home_and_system_folders_are_rejected(tmp_path):
    root = Path(tmp_path.anchor)
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(root))
    assert exc.value.code == "unsafe_location"

    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(Path.home()))
    assert exc.value.code == "unsafe_location"

    system = Path(os.environ["SystemRoot"]) if os.name == "nt" else Path("/etc")
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_project_path(str(system))
    assert exc.value.code == "unsafe_location"


def test_folder_inside_forbidden_root_is_rejected(tmp_path):
    runs = tmp_path / "runs"
    inner = runs / "run1"
    inner.mkdir(parents=True)
    for target in (runs, inner):
        with pytest.raises(pa.ProjectError) as exc:
            pa.validate_project_path(str(target), forbidden_roots=[runs])
        assert exc.value.code == "unsafe_location"


def test_project_id_distinguishes_same_named_folders(tmp_path):
    first = tmp_path / "a" / "app"
    second = tmp_path / "b" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    id_first = pa.make_project_id(first.resolve())
    id_second = pa.make_project_id(second.resolve())
    assert id_first != id_second
    assert id_first == pa.make_project_id(first.resolve())
    assert len(id_first) == 12


# --------------------------------------------------------------------------
# Discovery across layouts (read-only)
# --------------------------------------------------------------------------

def test_flat_script_project_with_a_test_file(tmp_path):
    write(tmp_path / "app.py", "X = 1\n")
    write(tmp_path / "test_app.py", "def test_x():\n    assert True\n")

    info = pa.inspect_project(tmp_path)

    assert info.layout == "scripts"
    assert info.tests.runner == "pytest"
    assert info.tests.confidence == "inferred"
    assert info.tests.test_files_found == 1
    assert info.tests.needs_configuration is False
    assert info.has_git is False


def test_src_layout_with_tests_directory_and_pytest_ini(tmp_path):
    write(tmp_path / "src" / "pkg" / "__init__.py")
    write(tmp_path / "tests" / "test_pkg.py", "def test_x():\n    assert True\n")
    write(tmp_path / "pytest.ini", "[pytest]\ntestpaths = tests\n")

    info = pa.inspect_project(tmp_path)

    assert info.layout == "src" and info.packages == ["pkg"]
    assert info.tests.confidence == "configured"
    assert "pytest.ini" in info.tests.configs
    assert "tests/" in info.indicators and "pytest.ini" in info.indicators


def test_flat_package_layout_and_pyproject_pytest_config(tmp_path):
    write(tmp_path / "mypkg" / "__init__.py")
    write(tmp_path / "pyproject.toml",
          '[project]\nname = "x"\ndependencies = ["requests>=2", "rich[extra]"]\n'
          "[tool.pytest.ini_options]\naddopts = \"-q\"\n")

    info = pa.inspect_project(tmp_path)

    assert info.layout == "package" and info.packages == ["mypkg"]
    assert info.tests.runner == "pytest" and info.tests.confidence == "configured"
    assert info.dependencies.names == ["requests", "rich"]
    assert "pyproject.toml [project.dependencies]" in info.dependencies.sources


def test_project_without_tests_needs_configuration(tmp_path):
    write(tmp_path / "script.py", "print('hi')\n")

    info = pa.inspect_project(tmp_path)

    assert info.tests.runner is None
    assert info.tests.confidence == "none"
    assert info.tests.needs_configuration is True
    assert info.tests.test_files_found == 0


def test_tox_only_project_is_flagged_ambiguous(tmp_path):
    write(tmp_path / "tox.ini", "[tox]\nenvlist = py312\n[testenv]\ncommands = nose2\n")
    write(tmp_path / "test_a.py", "def test_a():\n    pass\n")

    info = pa.inspect_project(tmp_path)

    assert info.tests.ambiguous is True
    assert info.tests.needs_configuration is True
    assert "tox" in info.tests.reason


def test_tox_ini_with_pytest_section_is_configured(tmp_path):
    write(tmp_path / "tox.ini", "[pytest]\naddopts = -q\n")
    info = pa.inspect_project(tmp_path)
    assert info.tests.confidence == "configured"
    assert "tox.ini [pytest]" in info.tests.configs


def test_django_style_project_is_flagged(tmp_path):
    write(tmp_path / "manage.py", "")
    write(tmp_path / "app_tests.py", "def test_a():\n    pass\n")
    assert pa.inspect_project(tmp_path).tests.ambiguous is True


def test_discovery_never_executes_or_writes_project_code(tmp_path):
    marker = tmp_path / "EXECUTED.txt"
    write(tmp_path / "setup.py", f"open(r'{marker}', 'w').write('x')\n")
    write(tmp_path / "conftest.py", f"open(r'{marker}', 'w').write('x')\n")
    write(tmp_path / "test_a.py", f"open(r'{marker}', 'w').write('x')\n")
    write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    before = snapshot(tmp_path)

    info = pa.inspect_project(tmp_path)

    assert not marker.exists()
    assert snapshot(tmp_path) == before
    assert any("never executed" in w for w in info.warnings)


def test_excluded_directories_are_not_searched(tmp_path):
    write(tmp_path / "app.py", "X = 1\n")
    for hidden in (".venv/lib/test_a.py", "node_modules/x/test_b.py", "build/test_c.py",
                   "dist/test_d.py", ".git/test_e.py", "pkg.egg-info/test_f.py"):
        write(tmp_path / hidden, "def test_x():\n    pass\n")
    write(tmp_path / "src" / "build" / "test_real.py", "def test_x():\n    pass\n")

    info = pa.inspect_project(tmp_path)

    assert info.tests.test_files_found == 1
    assert info.tests.test_dirs == ["src/build"]


def test_malformed_and_oversized_config_files_become_warnings(tmp_path):
    write(tmp_path / "pyproject.toml", "this is = = not toml [[[")
    write(tmp_path / "requirements.txt", "x" * (pa.MAX_CONFIG_FILE_BYTES + 10))

    info = pa.inspect_project(tmp_path)

    assert any("pyproject.toml could not be parsed" in w for w in info.warnings)
    assert any("requirements.txt is larger" in w for w in info.warnings)
    assert info.dependencies.names == []


def test_symlinked_config_file_is_ignored(tmp_path):
    outside = write(tmp_path / "outside.txt", "requests\n")
    project = tmp_path / "proj"
    project.mkdir()
    try:
        (project / "requirements.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this system")

    info = pa.inspect_project(project)

    assert info.dependencies.names == []
    assert any("symbolic link" in w for w in info.warnings)


def test_large_trees_are_truncated_with_a_warning(tmp_path):
    for index in range(30):
        write(tmp_path / f"f{index}.txt", "x")
    info = pa.inspect_project(tmp_path, max_files=10)
    assert info.truncated is True
    assert any("discovery stopped early" in w for w in info.warnings)


def test_project_info_is_json_serialisable(tmp_path):
    import json
    write(tmp_path / "test_a.py", "def test_a():\n    pass\n")
    data = pa.inspect_project(tmp_path).to_dict()
    assert json.loads(json.dumps(data))["tests"]["runner"] == "pytest"


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------

def test_requirement_parsing_handles_real_world_lines():
    lines = ["# comment", "", "requests==2.31.0  # pinned", "Flask[async]>=2", "-r base.txt",
             "-e .", "git+https://github.com/x/y.git#egg=y", "numpy ; python_version < '3.9'",
             "./local/pkg", "pkg @ https://example.com/p.whl", "Foo_Bar", "foo-bar"]

    names, unchecked = pa.parse_requirement_lines(lines)

    assert names == ["requests", "Flask", "Foo_Bar"]          # duplicate foo-bar dropped
    assert "-r base.txt" in unchecked and "-e ." in unchecked
    assert any("python_version" in u for u in unchecked)
    assert len(unchecked) == 6


def test_missing_dependencies_are_reported_not_installed():
    deps = pa.DependencyInfo(names=["pytest", "definitely-not-installed-pkg-xyz"], unchecked=["-r other.txt"])

    report = pa.check_dependencies(deps)

    assert "pytest" in report["installed"]
    assert report["missing"] == ["definitely-not-installed-pkg-xyz"]
    assert report["unchecked"] == ["-r other.txt"]
    assert "Nothing is installed" in report["note"]


def test_dependency_check_through_another_interpreter_process():
    versions = pa.installed_versions_via_interpreter(sys.executable, ["pytest", "no-such-package-xyz"])
    assert versions["pytest"]
    assert versions["no-such-package-xyz"] is None


# --------------------------------------------------------------------------
# Interpreter validation
# --------------------------------------------------------------------------

def test_current_interpreter_is_accepted_and_inspected():
    path = pa.validate_interpreter(sys.executable)
    info = pa.inspect_interpreter(path)
    assert info["has_pytest"] is True
    assert info["version"].startswith("3.")


@pytest.mark.parametrize("bad", ["", "python", "relative/python.exe", None])
def test_bad_interpreter_paths_are_rejected(bad):
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_interpreter(bad)
    assert exc.value.code == "invalid_interpreter"


def test_non_python_file_and_missing_file_are_rejected(tmp_path):
    notes = write(tmp_path / "notes.txt", "x")
    with pytest.raises(pa.ProjectError):
        pa.validate_interpreter(str(notes))
    with pytest.raises(pa.ProjectError):
        pa.validate_interpreter(str(tmp_path / "python.exe"))


def test_executable_that_is_not_python_is_rejected(tmp_path):
    fake = write(tmp_path / "python.exe", "not a program")
    path = pa.validate_interpreter(str(fake))
    with pytest.raises(pa.ProjectError) as exc:
        pa.inspect_interpreter(path)
    assert exc.value.code == "invalid_interpreter"


def pe_bytes(machine: int = 0x8664, characteristics: int = 0x0022, pe_offset: int = 0x80) -> bytes:
    """Smallest header that looks like a Windows PE file (test data only)."""
    data = bytearray(pe_offset + 24)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    data[pe_offset:pe_offset + 4] = b"PE\x00\x00"
    data[pe_offset + 4:pe_offset + 6] = machine.to_bytes(2, "little")
    data[pe_offset + 22:pe_offset + 24] = characteristics.to_bytes(2, "little")
    return bytes(data)


def test_pe_header_check_accepts_a_valid_executable():
    assert pa._check_pe_header(pe_bytes()) is None
    assert pa._check_pe_header(pe_bytes(machine=0x014C)) is None
    assert pa._check_pe_header(pe_bytes(machine=0xAA64)) is None


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"not a program",
        b"MZ" + b"\x00" * 100,                  # DOS/16-bit style: MZ but no PE header
        pe_bytes(machine=0x0000),                # unsupported processor
        pe_bytes(characteristics=0x2022),        # DLL flag set
        b"\x7fELF" + b"\x00" * 100,              # a non-Windows executable format
    ],
)
def test_pe_header_check_rejects_everything_else(header):
    assert pa._check_pe_header(header)


def _forbid_launch(*args, **kwargs):
    raise AssertionError("an invalid executable must not be launched")


def test_invalid_executable_is_rejected_before_launch_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "_IS_WINDOWS", True)
    monkeypatch.setattr(pa.subprocess, "run", _forbid_launch)
    fake = tmp_path / "python.exe"
    fake.write_bytes(b"MZ" + b"\x00" * 100)  # a DOS-style file triggers the 16-bit dialog
    path = pa.validate_interpreter(str(fake))
    with pytest.raises(pa.ProjectError) as exc:
        pa.inspect_interpreter(path)
    assert exc.value.code == "invalid_interpreter"
    with pytest.raises(pa.ProjectError) as exc:
        pa.installed_versions_via_interpreter(path, ["pytest"])
    assert exc.value.code == "invalid_interpreter"


def test_valid_executable_header_still_reaches_the_launch_step(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "_IS_WINDOWS", True)
    launched = []

    def fake_run(argv, **kwargs):
        launched.append(argv)
        return pa.subprocess.CompletedProcess(
            argv, 0, stdout='{"version": "3.12.1", "has_pytest": true}', stderr="")

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    candidate = tmp_path / "python.exe"
    candidate.write_bytes(pe_bytes())
    info = pa.inspect_interpreter(pa.validate_interpreter(str(candidate)))
    assert launched and info["version"] == "3.12.1"


def test_running_interpreter_is_still_accepted_with_the_windows_check_on(monkeypatch):
    monkeypatch.setattr(pa, "_IS_WINDOWS", True)
    info = pa.inspect_interpreter(pa.validate_interpreter(sys.executable))
    assert info["version"].startswith("3.")


# --------------------------------------------------------------------------
# Configuration and commands
# --------------------------------------------------------------------------

def test_default_config_is_pytest_with_standard_timeout():
    config = pa.parse_config(None)
    assert config.runner == "pytest"
    assert config.timeout_seconds == pa.DEFAULT_TEST_TIMEOUT_SECONDS
    assert config.allow_untested is False


def test_valid_custom_config():
    config = pa.parse_config({
        "runner": "custom", "command": ["python", "-m", "pytest", "-q"], "timeout_seconds": 30,
        "extra_excludes": ["docs/**", "data\\big.bin", "docs/**"], "allow_untested": True,
        "interpreter": sys.executable,
    })
    assert config.command == ("python", "-m", "pytest", "-q")
    assert config.extra_excludes == ("docs/**", "data/big.bin")
    assert config.allow_untested is True and config.timeout_seconds == 30


@pytest.mark.parametrize("data", [
    "text", {"runner": "make"}, {"bogus": 1}, {"runner": "custom"}, {"runner": "pytest", "command": ["x"]},
    {"timeout_seconds": 1}, {"timeout_seconds": 99999}, {"timeout_seconds": "60"}, {"timeout_seconds": True},
    {"allow_untested": "yes"}, {"extra_excludes": ["/abs"]}, {"extra_excludes": ["../up"]},
    {"extra_excludes": ["C:\\x"]}, {"extra_excludes": [""]}, {"extra_excludes": "docs"},
    {"interpreter": "relative/python"},
])
def test_invalid_configurations_are_rejected(data):
    with pytest.raises(pa.ProjectError) as exc:
        pa.parse_config(data)
    assert exc.value.code in {"invalid_config", "invalid_command", "invalid_interpreter"}


@pytest.mark.parametrize("argv", [
    [], ["cmd", "/c", "dir"], ["C:\\Windows\\System32\\cmd.exe", "/c", "x"], ["powershell", "-c", "x"],
    ["bash", "-c", "x"], ["run.bat"], ["python", ""], ["python", "a\x00b"], ["x"] * 65, "python -m pytest",
])
def test_unsafe_commands_are_rejected(argv):
    with pytest.raises(pa.ProjectError) as exc:
        pa.validate_command(argv)
    assert exc.value.code == "invalid_command"


def test_command_line_splitting_keeps_windows_paths():
    argv = pa.split_command_line('"C:\\Program Files\\Python312\\python.exe" -m pytest -k "not slow"')
    assert argv == ("C:\\Program Files\\Python312\\python.exe", "-m", "pytest", "-k", "not slow")
    with pytest.raises(pa.ProjectError):
        pa.split_command_line('python -m "pytest')
    with pytest.raises(pa.ProjectError):
        pa.split_command_line("   ")


def test_pytest_command_is_an_argument_list_with_junit(tmp_path):
    junit = tmp_path / "junit.xml"
    command = pa.build_test_command(pa.ProjectConfig(), junit)
    assert command.argv == (sys.executable, "-m", "pytest", "-v", "--junitxml", str(junit))
    assert command.count_method == "junitxml"

    custom_python = pa.build_test_command(pa.ProjectConfig(interpreter="/x/python"), None)
    assert custom_python.argv[0] == "/x/python" and custom_python.count_method == "none"


def test_unittest_and_custom_commands():
    unittest_cmd = pa.build_test_command(pa.ProjectConfig(runner="unittest"))
    assert unittest_cmd.argv[1:] == ("-m", "unittest", "discover", "-v")
    assert unittest_cmd.count_method == "unittest-output"

    custom = pa.build_test_command(pa.ProjectConfig(runner="custom", command=("tox", "-q")))
    assert custom.argv == ("tox", "-q") and custom.count_method == "none"


# --------------------------------------------------------------------------
# Outcome classification
# --------------------------------------------------------------------------

JUNIT = '<?xml version="1.0"?><testsuites><testsuite name="p" tests="{t}" failures="{f}" errors="{e}" skipped="{s}"/></testsuites>'


def junit(tmp_path, t=1, f=0, e=0, s=0):
    return write(tmp_path / "junit.xml", JUNIT.format(t=t, f=f, e=e, s=s))


def test_pytest_pass_requires_executed_tests(tmp_path):
    outcome = pa.classify_test_outcome("pytest", 0, "", junit_path=junit(tmp_path, t=3))
    assert (outcome.status, outcome.executed, outcome.collected) == ("passed", 3, 3)


def test_pytest_all_skipped_is_not_passed(tmp_path):
    outcome = pa.classify_test_outcome("pytest", 0, "", junit_path=junit(tmp_path, t=2, s=2))
    assert outcome.status == "no_tests" and outcome.executed == 0


def test_pytest_exit_zero_without_report_is_unverified(tmp_path):
    outcome = pa.classify_test_outcome("pytest", 0, "", junit_path=tmp_path / "missing.xml")
    assert outcome.status == "unverified" and outcome.executed is None


def test_pytest_corrupt_report_is_unverified(tmp_path):
    bad = write(tmp_path / "junit.xml", "<not xml")
    assert pa.classify_test_outcome("pytest", 0, "", junit_path=bad).status == "unverified"


def test_pytest_exit_zero_with_recorded_failures_is_failed(tmp_path):
    outcome = pa.classify_test_outcome("pytest", 0, "", junit_path=junit(tmp_path, t=2, f=1))
    assert outcome.status == "failed"


@pytest.mark.parametrize("code,status,kind", [
    (1, "failed", "test_failures"), (2, "failed", "collection_error"), (3, "error", "internal_error"),
    (4, "error", "usage_error"), (5, "no_tests", None), (9, "error", None),
])
def test_pytest_exit_codes_map_to_honest_statuses(tmp_path, code, status, kind):
    outcome = pa.classify_test_outcome("pytest", code, "", junit_path=junit(tmp_path, t=1, f=1))
    assert outcome.status == status and outcome.failure_kind == kind


def test_missing_dependency_is_reported_with_module_names(tmp_path):
    output = "E   ModuleNotFoundError: No module named 'requests'\nE   No module named 'numpy.core'\n"
    outcome = pa.classify_test_outcome("pytest", 2, output, junit_path=tmp_path / "none.xml")
    assert outcome.status == "failed"
    assert outcome.failure_kind == "missing_dependencies"
    assert outcome.missing_modules == ("requests", "numpy")
    assert "requests" in outcome.reason


def test_timeout_and_launch_error_take_priority(tmp_path):
    assert pa.classify_test_outcome("pytest", None, "", timed_out=True).status == "timeout"
    error = pa.classify_test_outcome("pytest", None, "", launch_error="file not found")
    assert error.status == "error" and "file not found" in error.reason


def test_unittest_outcomes():
    ok = "Ran 4 tests in 0.01s\n\nOK (skipped=1)\n"
    assert pa.classify_test_outcome("unittest", 0, ok).executed == 3
    assert pa.classify_test_outcome("unittest", 0, ok).status == "passed"

    all_skipped = "Ran 2 tests in 0.01s\n\nOK (skipped=2)\n"
    assert pa.classify_test_outcome("unittest", 0, all_skipped).status == "no_tests"

    none = "Ran 0 tests in 0.000s\n\nNO TESTS RAN\n"
    assert pa.classify_test_outcome("unittest", 5, none).status == "no_tests"
    assert pa.classify_test_outcome("unittest", 0, none).status == "no_tests"

    bad = "Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n"
    assert pa.classify_test_outcome("unittest", 1, bad).status == "failed"

    assert pa.classify_test_outcome("unittest", 0, "garbage").status == "unverified"


def test_custom_command_can_never_be_passed():
    ok = pa.classify_test_outcome("custom", 0, "all good")
    assert ok.status == "unverified" and ok.executed is None
    assert pa.classify_test_outcome("custom", 1, "boom").status == "failed"


def test_only_passed_outcomes_are_ever_reported_as_passed(tmp_path):
    for runner, code, output in [("custom", 0, ""), ("unittest", 0, ""), ("pytest", 0, "")]:
        assert pa.classify_test_outcome(runner, code, output).status != "passed"


# --------------------------------------------------------------------------
# Real pytest integration: the classification matches what pytest really does
# --------------------------------------------------------------------------

def run_real_pytest(project: Path):
    import subprocess
    junit_path = project.parent / f"{project.name}-junit.xml"
    command = pa.build_test_command(pa.ProjectConfig(), junit_path)
    result = subprocess.run(list(command.argv), cwd=project, capture_output=True, text=True,
                            env={**os.environ, "PYTEST_ADDOPTS": "-p no:cacheprovider", "PYTHONDONTWRITEBYTECODE": "1"})
    return pa.classify_test_outcome("pytest", result.returncode, result.stdout + result.stderr, junit_path=junit_path)


def test_real_pytest_passing_failing_empty_and_all_skipped_projects(tmp_path):
    passing = tmp_path / "passing"
    write(passing / "test_a.py", "def test_a():\n    assert True\n")
    failing = tmp_path / "failing"
    write(failing / "test_a.py", "def test_a():\n    assert False\n")
    empty = tmp_path / "empty"
    write(empty / "app.py", "X = 1\n")
    skipped = tmp_path / "skipped"
    write(skipped / "test_a.py", "import pytest\n\n@pytest.mark.skip\ndef test_a():\n    pass\n")
    broken = tmp_path / "broken"
    write(broken / "test_a.py", "import definitely_missing_module_xyz\n\ndef test_a():\n    pass\n")

    assert run_real_pytest(passing).status == "passed"
    assert run_real_pytest(failing).status == "failed"
    assert run_real_pytest(empty).status == "no_tests"
    assert run_real_pytest(skipped).status == "no_tests"
    missing = run_real_pytest(broken)
    assert missing.failure_kind == "missing_dependencies"
    assert missing.missing_modules == ("definitely_missing_module_xyz",)


# --------------------------------------------------------------------------
# Adapter interface
# --------------------------------------------------------------------------

def test_python_adapter_end_to_end(tmp_path):
    project = tmp_path / "proj"
    write(project / "app.py", "X = 1\n")
    write(project / "test_app.py", "def test_x():\n    assert True\n")

    adapter = pa.get_adapter("python")
    info = adapter.discover(str(project))
    config = pa.ProjectConfig(extra_excludes=("docs/**",))
    command = adapter.test_command(config, tmp_path / "j.xml")

    assert adapter.language == "python"
    assert info.name == "proj" and info.tests.runner == "pytest"
    assert adapter.extra_excludes(config) == ("docs/**",)
    assert command.runner == "pytest"
    assert "missing" in adapter.check_dependencies(info)


def test_unknown_language_is_reported_clearly():
    with pytest.raises(pa.ProjectError) as exc:
        pa.get_adapter("cobol")
    assert exc.value.code == "unsupported_language"
