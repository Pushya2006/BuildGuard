"""Project discovery and test-outcome classification for BuildGuard.

This module is the language-specific layer. Only a Python adapter exists
today; the small ``ProjectAdapter`` protocol exists so another language
can be added later without touching the common evidence pipeline.

SAFETY MODEL
------------
Discovery is read-only. It never imports, executes or installs anything
from the selected project: ``setup.py`` is never run, and configuration
files are only parsed as data (with size limits, ignoring symbolic
links). Nothing is ever written into the project folder.

Two functions launch a user-chosen Python interpreter, using only code
supplied by BuildGuard, in isolated mode (``-I``) from a neutral working
directory, so the project cannot shadow anything: ``inspect_interpreter``
and the interpreter-based dependency check.

This module does NOT sandbox anything. Running a project's tests (a later
batch) executes that project's code with the user's own permissions.

OUTCOME RULES
-------------
A test run is "passed" only when reliable evidence shows that at least one
test actually executed and none failed. Otherwise the outcome is one of:
failed, no_tests, unverified, timeout or error. Under the unchanged
assurance rules every outcome other than "passed" leaves R4 unsupported.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Iterable, Protocol

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - README requires Python 3.12
    tomllib = None

DEFAULT_TEST_TIMEOUT_SECONDS = 600
MIN_TEST_TIMEOUT_SECONDS = 5
MAX_TEST_TIMEOUT_SECONDS = 3600

MAX_CONFIG_FILE_BYTES = 1_000_000
MAX_JUNIT_BYTES = 5_000_000
DEFAULT_MAX_FILES = 20_000
MAX_WALK_DEPTH = 8
MAX_LISTED = 20
MAX_EXCLUDE_PATTERNS = 100
MAX_COMMAND_ARGS = 64

RUNNERS = ("pytest", "unittest", "custom")

# Directories never searched during discovery, at any depth.
_ALWAYS_SKIP_DIRS = frozenset(
    {
        "__pycache__", ".git", ".venv", "venv", ".tox", ".mypy_cache",
        ".ruff_cache", ".pytest_cache", "node_modules", ".eggs", "site-packages",
    }
)
# Conventional output directories, skipped only at the project root.
_ROOT_ONLY_SKIP_DIRS = frozenset({"build", "dist"})

_INTERPRETER_NAME = re.compile(r"^(python|pypy)[0-9.]*(w)?(\.exe)?$", re.IGNORECASE)
_SHELL_PROGRAMS = frozenset(
    {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "cscript", "wscript", "mshta"}
)
_IS_WINDOWS = os.name == "nt"
# PE "Machine" values for executables Windows can normally launch: x86, x64, ARM64.
_PE_MACHINES = frozenset({0x014C, 0x8664, 0xAA64})
_PE_DLL_FLAG = 0x2000

_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*)$"
)


class ProjectError(Exception):
    """A user-correctable problem, with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunnerInfo:
    runner: str | None          # "pytest" or None when nothing was found
    confidence: str             # "configured" | "inferred" | "none"
    reason: str
    ambiguous: bool
    needs_configuration: bool
    test_files_found: int
    test_dirs: list[str] = field(default_factory=list)
    configs: list[str] = field(default_factory=list)


@dataclass
class DependencyInfo:
    requirements_files: list[str] = field(default_factory=list)
    other_requirements_files: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class ProjectInfo:
    path: str
    name: str
    project_id: str
    indicators: list[str]
    layout: str                 # "src" | "package" | "scripts" | "empty"
    packages: list[str]
    has_git: bool
    tests: RunnerInfo
    dependencies: DependencyInfo
    warnings: list[str]
    truncated: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectConfig:
    runner: str = "pytest"
    command: tuple[str, ...] | None = None
    interpreter: str | None = None
    timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS
    extra_excludes: tuple[str, ...] = ()
    allow_untested: bool = False


@dataclass(frozen=True)
class RunCommand:
    argv: tuple[str, ...]
    runner: str
    count_method: str           # "junitxml" | "unittest-output" | "none"
    junit_path: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    status: str                 # passed|failed|no_tests|unverified|timeout|error
    reason: str
    executed: int | None
    collected: int | None
    failure_kind: str | None = None
    missing_modules: tuple[str, ...] = ()
    count_method: str = "none"


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker()) if checker else False


def _system_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
            value = os.environ.get(var)
            if value:
                roots.append(Path(value))
    else:
        roots.extend(
            Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev", "/boot")
        )
    resolved = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def validate_project_path(raw: str, forbidden_roots: Iterable[Path] = ()) -> Path:
    """Validate a user-supplied project folder and return its real path.

    Rejects: empty or non-string input, NUL characters, relative paths,
    ``..`` segments, UNC/network paths, missing or non-directory paths,
    symbolic-link and junction roots, drive/filesystem roots, the user's
    home folder itself, system folders, unreadable folders, and anything
    inside ``forbidden_roots`` (for example BuildGuard's own runs folder).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectError("invalid_path", "Enter or choose a project folder.")
    raw = raw.strip()
    if len(raw) > 1024 or "\x00" in raw:
        raise ProjectError("invalid_path", "The path is too long or contains invalid characters.")
    if raw.startswith(("\\\\", "//")):
        raise ProjectError(
            "unsafe_location",
            "Network (UNC) paths are not supported. Choose a folder on a local drive.",
        )

    path = Path(raw)
    if not path.is_absolute():
        raise ProjectError("invalid_path", "The path must be absolute, for example C:\\Users\\you\\project.")
    if ".." in path.parts:
        raise ProjectError("invalid_path", "The path must not contain '..' segments.")

    try:
        if not path.exists():
            raise ProjectError("not_found", f"Folder not found: {raw}")
        if not path.is_dir():
            raise ProjectError("not_a_directory", "The selected path is a file, not a folder.")
        if path.is_symlink() or _is_junction(path):
            raise ProjectError(
                "symlink_root",
                "The selected folder is a symbolic link or junction. Choose the real folder instead.",
            )
        resolved = path.resolve(strict=True)
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError("not_accessible", f"The folder cannot be accessed: {exc}")

    if resolved.parent == resolved:
        raise ProjectError("unsafe_location", "A drive or filesystem root cannot be used as a project.")
    try:
        if resolved == Path.home().resolve():
            raise ProjectError(
                "unsafe_location",
                "Your home folder is not a project. Choose a specific project folder inside it.",
            )
    except RuntimeError:  # pragma: no cover - home directory unavailable
        pass

    for system_root in _system_roots():
        if resolved == system_root or system_root in resolved.parents:
            raise ProjectError("unsafe_location", "System folders cannot be used as projects.")

    for forbidden in forbidden_roots:
        try:
            forbidden = Path(forbidden).resolve()
        except OSError:
            continue
        if resolved == forbidden or forbidden in resolved.parents:
            raise ProjectError("unsafe_location", "That folder is inside BuildGuard's own output area.")

    try:
        next(os.scandir(resolved), None)
    except OSError as exc:
        raise ProjectError("not_accessible", f"The folder cannot be read: {exc}")

    return resolved


def make_project_id(resolved: Path) -> str:
    """Stable short identifier so same-named folders stay distinguishable."""
    text = str(resolved)
    if os.name == "nt":
        text = text.lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Discovery (read-only)
# ---------------------------------------------------------------------------

def _read_limited(path: Path, warnings: list[str]) -> str | None:
    """Read a small, regular, non-symlink config file as data."""
    try:
        if path.is_symlink():
            warnings.append(f"{path.name} is a symbolic link and was ignored.")
            return None
        if path.stat().st_size > MAX_CONFIG_FILE_BYTES:
            warnings.append(f"{path.name} is larger than {MAX_CONFIG_FILE_BYTES} bytes and was not read.")
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"{path.name} could not be read: {exc}")
        return None


def parse_requirement_lines(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return (distribution names, lines that could not be checked)."""
    names: list[str] = []
    unchecked: list[str] = []
    seen: set[str] = set()

    for raw in lines:
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        if (
            line.startswith(("-", ".", "/", "~"))
            or "://" in line
            or "@" in line
            or ";" in line
            or "\\" in line
        ):
            unchecked.append(line[:200])
            continue
        match = _REQUIREMENT.match(line)
        if not match:
            unchecked.append(line[:200])
            continue
        name = match.group(1)
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key not in seen:
            seen.add(key)
            names.append(name)

    return names, unchecked


def _load_pyproject(root: Path, warnings: list[str]) -> dict | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    if tomllib is None:  # pragma: no cover
        warnings.append("pyproject.toml was not parsed (Python 3.11+ is required).")
        return None
    text = _read_limited(path, warnings)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        warnings.append(f"pyproject.toml could not be parsed: {str(exc)[:120]}")
        return None
    return data if isinstance(data, dict) else None


def _ini_has_section(root: Path, filename: str, section: str, warnings: list[str]) -> bool:
    path = root / filename
    if not path.is_file():
        return False
    text = _read_limited(path, warnings)
    if text is None:
        return False
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        warnings.append(f"{filename} could not be parsed: {str(exc)[:120]}")
        return False
    return parser.has_section(section)


def _is_test_filename(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _scan_tree(root: Path, max_files: int) -> tuple[list[str], bool]:
    """Bounded walk that returns (relative test files, truncated)."""
    test_files: list[str] = []
    visited = 0
    truncated = False

    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)

        kept = []
        for name in dirnames:
            if name in _ALWAYS_SKIP_DIRS or name.endswith(".egg-info"):
                continue
            if depth == 0 and name in _ROOT_ONLY_SKIP_DIRS:
                continue
            child = current_path / name
            if child.is_symlink() or _is_junction(child):
                continue
            if depth + 1 > MAX_WALK_DEPTH:
                truncated = True
                continue
            kept.append(name)
        dirnames[:] = sorted(kept)

        for name in sorted(filenames):
            visited += 1
            if visited > max_files:
                return test_files, True
            if _is_test_filename(name) and not (current_path / name).is_symlink():
                if len(test_files) < 1000:
                    test_files.append((current_path / name).relative_to(root).as_posix())

    return test_files, truncated


def _detect_layout(root: Path) -> tuple[str, list[str]]:
    def packages_in(directory: Path) -> list[str]:
        try:
            return sorted(
                entry.name
                for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)
                and entry.name not in _ALWAYS_SKIP_DIRS
                and not entry.name.endswith(".egg-info")
                and (Path(entry.path) / "__init__.py").is_file()
            )
        except OSError:
            return []

    src = root / "src"
    if src.is_dir() and not src.is_symlink():
        pkgs = packages_in(src)
        if pkgs:
            return "src", pkgs[:MAX_LISTED]

    top = [p for p in packages_in(root) if p not in {"tests", "test"} and p not in _ROOT_ONLY_SKIP_DIRS]
    if top:
        return "package", top[:MAX_LISTED]

    try:
        modules = [
            e.name for e in os.scandir(root)
            if e.is_file(follow_symlinks=False)
            and e.name.endswith(".py")
            and not _is_test_filename(e.name)
            and e.name not in {"setup.py", "conftest.py"}
        ]
    except OSError:
        modules = []
    if modules:
        return "scripts", sorted(modules)[:MAX_LISTED]

    return "empty", []


def _discover_dependencies(root: Path, pyproject: dict | None, warnings: list[str]) -> DependencyInfo:
    info = DependencyInfo()
    names: list[str] = []
    unchecked: list[str] = []

    requirements = root / "requirements.txt"
    if requirements.is_file():
        text = _read_limited(requirements, warnings)
        if text is not None:
            found, skipped = parse_requirement_lines(text.splitlines())
            names += found
            unchecked += skipped
            info.requirements_files.append("requirements.txt")
            info.sources.append("requirements.txt")

    if pyproject:
        project_table = pyproject.get("project")
        declared = project_table.get("dependencies") if isinstance(project_table, dict) else None
        if isinstance(declared, list):
            found, skipped = parse_requirement_lines(str(item) for item in declared)
            names += found
            unchecked += skipped
            info.sources.append("pyproject.toml [project.dependencies]")

    try:
        info.other_requirements_files = sorted(
            e.name for e in os.scandir(root)
            if e.is_file(follow_symlinks=False)
            and re.fullmatch(r"requirements[-_.][\w.-]*\.txt", e.name, re.IGNORECASE)
        )[:MAX_LISTED]
    except OSError:
        pass

    seen: set[str] = set()
    for name in names:
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key not in seen:
            seen.add(key)
            info.names.append(name)
    info.unchecked = unchecked[:MAX_LISTED]
    return info


def _detect_runner(root: Path, pyproject: dict | None, test_files: list[str], warnings: list[str]) -> RunnerInfo:
    configs: list[str] = []
    if (root / "pytest.ini").is_file():
        configs.append("pytest.ini")
    tool = pyproject.get("tool") if pyproject else None
    if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
        configs.append("pyproject.toml [tool.pytest]")
    if _ini_has_section(root, "tox.ini", "pytest", warnings):
        configs.append("tox.ini [pytest]")
    if _ini_has_section(root, "setup.cfg", "tool:pytest", warnings):
        configs.append("setup.cfg [tool:pytest]")
    if (root / "conftest.py").is_file() or (root / "tests" / "conftest.py").is_file():
        configs.append("conftest.py")

    signals: list[str] = []
    if (root / "tox.ini").is_file() and not any(c.startswith("tox.ini") for c in configs):
        signals.append("tox.ini is present, but BuildGuard does not run tox")
    if (root / "noxfile.py").is_file():
        signals.append("noxfile.py is present, but BuildGuard does not run nox")
    if (root / "manage.py").is_file():
        signals.append("manage.py suggests a Django project, which may need a custom test command")

    dirs: list[str] = []
    for relative in test_files:
        parent = Path(relative).parent.as_posix()
        if parent not in dirs and len(dirs) < MAX_LISTED:
            dirs.append(parent)

    if configs:
        return RunnerInfo("pytest", "configured", "pytest configuration found: " + ", ".join(configs) + ".",
                          False, False, len(test_files), dirs, configs)
    if test_files:
        ambiguous = bool(signals)
        reason = (f"{len(test_files)} file(s) named test_*.py or *_test.py found; pytest collects these, "
                  "including unittest-style tests.")
        if ambiguous:
            reason += " " + "; ".join(signals) + ". Confirm pytest or set a test command."
        return RunnerInfo("pytest", "inferred", reason, ambiguous, ambiguous, len(test_files), dirs, configs)

    reason = "No pytest configuration and no test_*.py or *_test.py files were found."
    if signals:
        reason += " " + "; ".join(signals) + "."
    return RunnerInfo(None, "none", reason, bool(signals), True, 0, dirs, configs)


def inspect_project(resolved: Path, max_files: int = DEFAULT_MAX_FILES) -> ProjectInfo:
    """Describe a validated project folder without executing anything in it."""
    warnings: list[str] = []
    indicators = [
        name for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
                          "pytest.ini", "tox.ini", "conftest.py")
        if (resolved / name).is_file()
    ]
    for name in ("tests", "test"):
        if (resolved / name).is_dir() and not (resolved / name).is_symlink():
            indicators.append(f"{name}/")

    if "setup.py" in indicators:
        warnings.append("setup.py was detected but is never executed by BuildGuard.")

    pyproject = _load_pyproject(resolved, warnings)
    test_files, truncated = _scan_tree(resolved, max_files)
    if truncated:
        warnings.append("The folder is large; discovery stopped early and results may be incomplete.")

    layout, packages = _detect_layout(resolved)
    return ProjectInfo(
        path=str(resolved),
        name=resolved.name,
        project_id=make_project_id(resolved),
        indicators=indicators,
        layout=layout,
        packages=packages,
        has_git=(resolved / ".git").exists(),
        tests=_detect_runner(resolved, pyproject, test_files, warnings),
        dependencies=_discover_dependencies(resolved, pyproject, warnings),
        warnings=warnings,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Interpreter selection and dependency reporting
# ---------------------------------------------------------------------------

def _minimal_env() -> dict:
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONUTF8"] = "1"
    return env


def validate_interpreter(raw: str) -> Path:
    """Check that a path looks like an existing Python executable."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or len(raw) > 1024:
        raise ProjectError("invalid_interpreter", "Enter the full path to a Python executable.")
    path = Path(raw.strip())
    if not path.is_absolute():
        raise ProjectError("invalid_interpreter", "The interpreter path must be absolute.")
    if ".." in path.parts:
        raise ProjectError("invalid_interpreter", "The interpreter path must not contain '..'.")
    if not path.is_file():
        raise ProjectError("invalid_interpreter", f"Interpreter not found: {raw}")
    if not _INTERPRETER_NAME.match(path.name):
        raise ProjectError("invalid_interpreter", "The file name must look like python, python3 or python.exe.")
    return path


def _check_pe_header(header: bytes) -> str | None:
    """Return a reason if ``header`` is not a launchable 32/64-bit Windows PE
    executable, else None.

    Only the headers are parsed. This stops Windows from showing an
    "Unsupported 16-Bit Application" or "Bad Image" dialog for plain text,
    DOS/NE files and DLLs. It does NOT prove an executable is safe.
    """
    if len(header) < 0x40 or header[:2] != b"MZ":
        return "The file is not a Windows executable."
    pe_offset = int.from_bytes(header[0x3C:0x40], "little")
    if pe_offset < 0x40 or pe_offset + 24 > len(header):
        return "The file is not a 32-bit or 64-bit Windows executable."
    if header[pe_offset:pe_offset + 4] != b"PE\x00\x00":
        return "The file is not a 32-bit or 64-bit Windows executable."
    machine = int.from_bytes(header[pe_offset + 4:pe_offset + 6], "little")
    characteristics = int.from_bytes(header[pe_offset + 22:pe_offset + 24], "little")
    if machine not in _PE_MACHINES:
        return "The executable targets an unsupported processor type."
    if characteristics & _PE_DLL_FLAG:
        return "The file is a library (DLL), not an executable."
    return None


def _ensure_launchable(path: Path | str) -> None:
    """On Windows, refuse to launch a file whose header is not a valid PE
    executable. The running interpreter is exempt. No-op on other platforms.
    """
    if not _IS_WINDOWS:
        return
    try:
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(sys.executable)):
            return
        with open(path, "rb") as handle:
            header = handle.read(4096)
    except OSError as exc:
        raise ProjectError("invalid_interpreter", f"The interpreter could not be read: {exc}")
    reason = _check_pe_header(header)
    if reason:
        raise ProjectError("invalid_interpreter", f"That file is not a usable Python interpreter. {reason}")


_INSPECT_SNIPPET = (
    "import json, sys, importlib.util as u\n"
    "print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])),"
    " 'has_pytest': u.find_spec('pytest') is not None}))"
)


def inspect_interpreter(path: Path, timeout: int = 20) -> dict:
    """Ask an interpreter for its version and whether pytest is installed.

    Runs BuildGuard-supplied code only, in isolated mode, from a neutral
    working directory.
    """
    _ensure_launchable(path)
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", _INSPECT_SNIPPET],
            cwd=tempfile.gettempdir(), env=_minimal_env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=timeout, shell=False, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProjectError("invalid_interpreter", "The interpreter did not respond in time.")
    except OSError as exc:
        raise ProjectError("invalid_interpreter", f"The interpreter could not be started: {exc}")

    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        version = str(data["version"])
        has_pytest = bool(data["has_pytest"])
        major, minor = (int(part) for part in version.split(".")[:2])
    except (IndexError, KeyError, ValueError, TypeError):
        raise ProjectError("invalid_interpreter", "That executable did not behave like a Python interpreter.")

    if (major, minor) < (3, 8):
        raise ProjectError("invalid_interpreter", f"Python {version} is too old; Python 3.8 or newer is required.")
    return {"path": str(path), "version": version, "has_pytest": has_pytest}


_VERSIONS_SNIPPET = (
    "import json, sys\n"
    "from importlib import metadata as m\n"
    "out = {}\n"
    "for n in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        out[n] = m.version(n)\n"
    "    except Exception:\n"
    "        out[n] = None\n"
    "print(json.dumps(out))"
)


def installed_versions_in_process(names: Iterable[str]) -> dict:
    versions = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def installed_versions_via_interpreter(python: str | Path, names: list[str], timeout: int = 30) -> dict:
    _ensure_launchable(python)
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", _VERSIONS_SNIPPET, json.dumps(names)],
            cwd=tempfile.gettempdir(), env=_minimal_env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=timeout, shell=False, check=False,
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, OSError, IndexError, ValueError):
        raise ProjectError("invalid_interpreter", "Could not read installed packages from that interpreter.")
    return {str(k): (str(v) if v else None) for k, v in data.items()}


def check_dependencies(deps: DependencyInfo, interpreter: str | None = None) -> dict:
    """Report which declared requirements are missing. Never installs anything.

    This is informational: a missing package does not block a run, but the
    report lets the UI explain a later failure. Requirement lines that
    cannot be evaluated reliably (URLs, markers, local paths, includes)
    are listed as unchecked rather than guessed.
    """
    names = list(deps.names)
    if interpreter and Path(interpreter) != Path(sys.executable):
        versions = installed_versions_via_interpreter(interpreter, names) if names else {}
        checked_with = str(interpreter)
    else:
        versions = installed_versions_in_process(names)
        checked_with = sys.executable

    return {
        "checked_with": checked_with,
        "installed": {n: v for n, v in versions.items() if v},
        "missing": [n for n, v in versions.items() if not v],
        "unchecked": list(deps.unchecked),
        "note": "Declared requirement names only; versions and extras are not compared. Nothing is installed.",
    }


# ---------------------------------------------------------------------------
# Configuration and commands
# ---------------------------------------------------------------------------

def validate_command(argv) -> tuple[str, ...]:
    """Validate a custom test command given as an argument list (no shell)."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ProjectError("invalid_command", "The test command must be a non-empty list of arguments.")
    if len(argv) > MAX_COMMAND_ARGS:
        raise ProjectError("invalid_command", f"The test command has more than {MAX_COMMAND_ARGS} arguments.")
    cleaned = []
    for item in argv:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > 1024:
            raise ProjectError("invalid_command", "Each argument must be a non-empty string without control characters.")
        cleaned.append(item)

    program = re.split(r"[\\/]", cleaned[0])[-1].lower()
    stem = re.sub(r"\.(exe|cmd|bat|com)$", "", program)
    if stem in _SHELL_PROGRAMS or program.endswith((".cmd", ".bat")):
        raise ProjectError(
            "invalid_command",
            "Shells and batch files are not allowed as the test program. Run Python or pytest directly.",
        )
    return tuple(cleaned)


def split_command_line(text: str) -> tuple[str, ...]:
    """Split typed command text into arguments, keeping Windows backslashes."""
    if not isinstance(text, str) or not text.strip():
        raise ProjectError("invalid_command", "Enter a test command.")
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        raise ProjectError("invalid_command", "The command has unbalanced quotes.")
    cleaned = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return validate_command(cleaned)


def validate_exclude_patterns(patterns) -> tuple[str, ...]:
    """Validate extra packaging exclusions (they can only add exclusions)."""
    if patterns is None:
        return ()
    if not isinstance(patterns, (list, tuple)) or len(patterns) > MAX_EXCLUDE_PATTERNS:
        raise ProjectError("invalid_config", f"extra_excludes must be a list of at most {MAX_EXCLUDE_PATTERNS} patterns.")
    cleaned = []
    for item in patterns:
        if not isinstance(item, str) or not item.strip() or len(item) > 200 or "\x00" in item:
            raise ProjectError("invalid_config", "Each exclusion pattern must be a short, non-empty string.")
        pattern = item.strip().replace("\\", "/")
        if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern) or ".." in pattern.split("/"):
            raise ProjectError("invalid_config", f"Exclusion '{item}' must be relative and must not contain '..'.")
        if pattern not in cleaned:
            cleaned.append(pattern)
    return tuple(cleaned)


_CONFIG_KEYS = {"runner", "command", "interpreter", "timeout_seconds", "extra_excludes", "allow_untested"}


def parse_config(data) -> ProjectConfig:
    """Build a validated ProjectConfig from user input; unknown keys are errors."""
    if data is None:
        return ProjectConfig()
    if not isinstance(data, dict):
        raise ProjectError("invalid_config", "Configuration must be an object.")
    unknown = sorted(set(data) - _CONFIG_KEYS)
    if unknown:
        raise ProjectError("invalid_config", f"Unknown configuration option(s): {', '.join(unknown)}.")

    runner = data.get("runner", "pytest")
    if runner not in RUNNERS:
        raise ProjectError("invalid_config", f"runner must be one of: {', '.join(RUNNERS)}.")

    command = None
    if runner == "custom":
        command = validate_command(data.get("command"))
    elif data.get("command") is not None:
        raise ProjectError("invalid_config", "command is only used with runner 'custom'.")

    interpreter = data.get("interpreter")
    if interpreter is not None:
        interpreter = str(validate_interpreter(interpreter))

    timeout = data.get("timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS)
    if (not isinstance(timeout, int) or isinstance(timeout, bool)
            or not MIN_TEST_TIMEOUT_SECONDS <= timeout <= MAX_TEST_TIMEOUT_SECONDS):
        raise ProjectError(
            "invalid_config",
            f"timeout_seconds must be a whole number from {MIN_TEST_TIMEOUT_SECONDS} to {MAX_TEST_TIMEOUT_SECONDS}.",
        )

    allow_untested = data.get("allow_untested", False)
    if not isinstance(allow_untested, bool):
        raise ProjectError("invalid_config", "allow_untested must be true or false.")

    return ProjectConfig(runner, command, interpreter, timeout,
                         validate_exclude_patterns(data.get("extra_excludes")), allow_untested)


def build_test_command(config: ProjectConfig, junit_path: Path | None = None,
                       default_python: str | None = None) -> RunCommand:
    """Build the test argv (never a shell string) for a configuration."""
    python = config.interpreter or default_python or sys.executable

    if config.runner == "pytest":
        argv = [python, "-m", "pytest", "-v"]
        if junit_path is not None:
            argv += ["--junitxml", str(junit_path)]
        return RunCommand(tuple(argv), "pytest", "junitxml" if junit_path else "none",
                          str(junit_path) if junit_path else None)
    if config.runner == "unittest":
        return RunCommand((python, "-m", "unittest", "discover", "-v"), "unittest", "unittest-output")
    if config.runner == "custom":
        return RunCommand(validate_command(config.command), "custom", "none")
    raise ProjectError("invalid_config", f"Unknown runner: {config.runner}")


# ---------------------------------------------------------------------------
# Test-outcome classification
# ---------------------------------------------------------------------------

def parse_junit(path: Path | str | None) -> dict | None:
    """Sum the counts in a pytest JUnit XML report, or None if unusable."""
    if path is None:
        return None
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_JUNIT_BYTES:
            return None
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            return None
        totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
        return totals
    except (ET.ParseError, OSError, ValueError):
        return None


def parse_unittest_output(output: str) -> dict | None:
    """Read 'Ran N tests' and 'skipped=K' from unittest output."""
    if re.search(r"^NO TESTS RAN", output, re.MULTILINE):
        return {"ran": 0, "skipped": 0}
    match = re.search(r"^Ran (\d+) tests? in ", output, re.MULTILINE)
    if not match:
        return None
    skipped = re.search(r"skipped=(\d+)", output)
    return {"ran": int(match.group(1)), "skipped": int(skipped.group(1)) if skipped else 0}


def find_missing_modules(output: str) -> tuple[str, ...]:
    """Extract module names from 'No module named ...' errors, in order."""
    found: list[str] = []
    for name in re.findall(r"No module named ['\"]([^'\"]+)['\"]", output):
        top = name.split(".")[0]
        if top not in found:
            found.append(top)
    return tuple(found)


def classify_test_outcome(
    runner: str,
    exit_code: int | None,
    output: str,
    *,
    timed_out: bool = False,
    launch_error: str | None = None,
    junit_path: Path | str | None = None,
) -> RunOutcome:
    """Decide the honest status of a test run.

    "passed" requires reliable evidence that at least one test executed
    and none failed. Anything less is reported as it really is.
    """
    if launch_error:
        return RunOutcome("error", f"The test command could not be run: {launch_error}", None, None)
    if timed_out:
        return RunOutcome("timeout", "The test run exceeded the time limit and was stopped.", None, None)

    missing = find_missing_modules(output or "")

    def failed(reason: str, kind: str, executed=None, collected=None, method="none") -> RunOutcome:
        if missing:
            return RunOutcome("failed", "Missing dependencies in the test interpreter: " + ", ".join(missing) + ".",
                              executed, collected, "missing_dependencies", missing, method)
        return RunOutcome("failed", reason, executed, collected, kind, (), method)

    if runner == "pytest":
        junit = parse_junit(junit_path)
        collected = junit["tests"] if junit else None
        executed = junit["tests"] - junit["skipped"] if junit else None
        if exit_code == 5:
            return RunOutcome("no_tests", "pytest collected no tests.", 0, 0, None, (), "junitxml")
        if exit_code == 4:
            return RunOutcome("error", "pytest reported a usage or configuration error (exit code 4).",
                              None, None, "usage_error")
        if exit_code == 3:
            return RunOutcome("error", "pytest hit an internal error (exit code 3).", None, None, "internal_error")
        if exit_code == 2:
            return failed("Test collection or execution was interrupted (exit code 2).", "collection_error",
                          executed, collected, "junitxml")
        if exit_code == 1:
            return failed("One or more tests failed (exit code 1).", "test_failures", executed, collected, "junitxml")
        if exit_code == 0:
            if junit is None:
                return RunOutcome("unverified",
                                  "pytest exited 0 but produced no usable JUnit report, so the number of "
                                  "executed tests could not be verified.", None, None, None, (), "junitxml")
            if executed == 0:
                return RunOutcome("no_tests",
                                  f"pytest exited 0 but no tests executed ({collected} collected, "
                                  f"{junit['skipped']} skipped).", 0, collected, None, (), "junitxml")
            if junit["failures"] or junit["errors"]:
                return failed("The test report records failures or errors.", "test_failures",
                              executed, collected, "junitxml")
            return RunOutcome("passed", f"{executed} test(s) executed and passed.", executed, collected,
                              None, (), "junitxml")
        return RunOutcome("error", f"pytest exited with unexpected code {exit_code}.", None, None)

    if runner == "unittest":
        parsed = parse_unittest_output(output or "")
        ran = parsed["ran"] if parsed else None
        executed = (parsed["ran"] - parsed["skipped"]) if parsed else None
        if exit_code == 5 or (parsed and parsed["ran"] == 0):
            return RunOutcome("no_tests", "unittest discovered no tests.", 0, 0, None, (), "unittest-output")
        if exit_code == 0:
            if parsed is None:
                return RunOutcome("unverified", "unittest exited 0 but its test count could not be read.",
                                  None, None, None, (), "unittest-output")
            if executed == 0:
                return RunOutcome("no_tests", "unittest ran tests but all were skipped.", 0, ran, None, (),
                                  "unittest-output")
            return RunOutcome("passed", f"{executed} test(s) executed and passed.", executed, ran, None, (),
                              "unittest-output")
        return failed(f"unittest reported failures (exit code {exit_code}).", "test_failures",
                      executed, ran, "unittest-output")

    # custom command: BuildGuard cannot verify how many tests executed.
    if exit_code == 0:
        return RunOutcome("unverified",
                          "The custom test command exited 0, but BuildGuard cannot verify that any tests "
                          "executed.", None, None, None, (), "none")
    return failed(f"The custom test command failed (exit code {exit_code}).", "test_failures")


# ---------------------------------------------------------------------------
# Adapter interface (Python only for now)
# ---------------------------------------------------------------------------

class ProjectAdapter(Protocol):
    language: str

    def discover(self, path: str, forbidden_roots: Iterable[Path] = ()) -> ProjectInfo: ...
    def check_dependencies(self, info: ProjectInfo, interpreter: str | None = None) -> dict: ...
    def test_command(self, config: ProjectConfig, junit_path: Path | None = None) -> RunCommand: ...
    def classify_outcome(self, command: RunCommand, exit_code: int | None, output: str, **kwargs) -> RunOutcome: ...
    def extra_excludes(self, config: ProjectConfig) -> tuple[str, ...]: ...


class PythonAdapter:
    language = "python"

    def discover(self, path: str, forbidden_roots: Iterable[Path] = ()) -> ProjectInfo:
        return inspect_project(validate_project_path(path, forbidden_roots))

    def check_dependencies(self, info: ProjectInfo, interpreter: str | None = None) -> dict:
        return check_dependencies(info.dependencies, interpreter)

    def test_command(self, config: ProjectConfig, junit_path: Path | None = None) -> RunCommand:
        return build_test_command(config, junit_path)

    def classify_outcome(self, command: RunCommand, exit_code: int | None, output: str, **kwargs) -> RunOutcome:
        return classify_test_outcome(command.runner, exit_code, output, junit_path=command.junit_path, **kwargs)

    def extra_excludes(self, config: ProjectConfig) -> tuple[str, ...]:
        return config.extra_excludes


ADAPTERS = {"python": PythonAdapter()}


def get_adapter(language: str = "python") -> ProjectAdapter:
    try:
        return ADAPTERS[language]
    except KeyError:
        raise ProjectError("unsupported_language", f"Language '{language}' is not supported yet.")
