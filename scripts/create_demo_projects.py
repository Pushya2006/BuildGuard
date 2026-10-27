"""Create small demo projects for the BuildGuard web UI.

Each project is real: BuildGuard runs its actual tests and builds its
actual artifact. Nothing here fakes a result.

    python scripts/create_demo_projects.py [--projects-root DIR] [--force]

Expected outcomes in the UI:
    demo-clean            ACCEPT   (clean Git commit, tests pass)
    demo-uncommitted      REVISE   (uncommitted change: source not traceable)
    demo-no-git           REVISE   (no Git metadata: source not traceable)
    demo-failing-tests    BUILD FAILED (tests fail; no artifact is created)
"""

import argparse
import shutil
import subprocess
from pathlib import Path

APP = "def add(a, b):\n    return a + b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
FAILING_TEST = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 4, 'deliberate demo failure'\n"
NESTED_BUILD = '"""A legitimate source package that happens to be named build."""\nNAME = "demo"\n'


def write_project(path: Path, test_source: str = TEST) -> None:
    (path / "src" / "build").mkdir(parents=True)
    (path / "app.py").write_text(APP, encoding="utf-8")
    (path / "test_app.py").write_text(test_source, encoding="utf-8")
    (path / "src" / "build" / "__init__.py").write_text(NESTED_BUILD, encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\ndist/\n", encoding="utf-8")


def git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=BuildGuard Demo", "-c", "user.email=demo@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=path, check=True, capture_output=True,
    )


def commit_all(path: Path) -> None:
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "Initial commit")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects-root", type=Path, default=Path(__file__).resolve().parents[1] / "projects")
    parser.add_argument("--force", action="store_true", help="Recreate demo projects that already exist.")
    args = parser.parse_args()

    root = args.projects_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    builders = {
        "demo-clean": lambda p: (write_project(p), commit_all(p)),
        "demo-uncommitted": lambda p: (
            write_project(p), commit_all(p),
            (p / "app.py").write_text(APP + "\n# uncommitted edit\n", encoding="utf-8"),
        ),
        "demo-no-git": lambda p: write_project(p),
        "demo-failing-tests": lambda p: write_project(p, FAILING_TEST),
    }

    for name, build_project in builders.items():
        path = root / name
        if path.exists():
            if not args.force:
                print(f"skip    {name} (already exists; use --force to recreate)")
                continue
            shutil.rmtree(path)
        path.mkdir()
        build_project(path)
        print(f"created {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
