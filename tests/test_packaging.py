import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATHS = (
    "src",
    "tests",
    "examples",
    "skills",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "docs",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    ".env.example",
)
EXCLUDED_PARTS = frozenset({".docs", "hgit", ".worktree", ".git", ".venv", "private"})
SENTINELS = {
    "data/private/observations/sentinel.json": "private-observation-release-sentinel\n",
    "data/catalog.sqlite3": "local-database-release-sentinel\n",
    "src/agent_costbook/local.sqlite3": "private-nested-database-sentinel\n",
    ".docs/private-contract.md": "local development contract\n",
    "hgit/ao/installation.json": "{}\n",
    ".worktree/receipt.json": "{}\n",
    ".git/config": "[core]\n",
    ".venv/pyvenv.cfg": "home = /nonexistent\n",
    ".env": "ACB_ADMIN_TOKEN=real-env-sentinel-not-for-release\n",
}
SDIST_PREFIX = "agent_costbook-1.2.0/"
REQUIRED_SDIST_MEMBERS = (
    "src/agent_costbook/__init__.py",
    "tests/test_api.py",
    "examples/estimate-request.json",
    "skills/costbook-contribute/SKILL.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".env.example",
    "LICENSE",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "docs/reference.md",
    "docs/data-sources.md",
    "scripts/check-docs.py",
    "scripts/check-demo.py",
    "PKG-INFO",
)


def _is_excluded_member(name: str) -> bool:
    parts = name.split("/")
    relative = name.removeprefix(SDIST_PREFIX).removeprefix("src/")
    if name.endswith((".sqlite", ".sqlite3", ".db")):
        return relative not in {"agent_costbook/public_catalog/catalog.sqlite3", "tests/fixtures/ac-v0.2-sample.sqlite3"}
    return "data" in parts or ".env" in parts or any(part in EXCLUDED_PARTS for part in parts)


def _seed_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in PUBLIC_PATHS:
        source = ROOT / relative
        destination = project / relative
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for relative, text in SENTINELS.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def _archive_names(path: Path) -> set[str]:
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path) as archive:
        return set(archive.getnames())


def _sdist_text(path: Path, member: str) -> str:
    with tarfile.open(path) as archive:
        extracted = archive.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def test_sdist_and_wheel_keep_public_files_and_drop_seeded_local_files(tmp_path):
    project = _seed_project(tmp_path)
    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    sdists = list(dist.glob("agent_costbook-1.2.0.tar.gz"))
    wheels = list(dist.glob("agent_costbook-1.2.0-*.whl"))
    assert len(sdists) == 1
    assert len(wheels) == 1

    sdist_names = _archive_names(sdists[0])
    wheel_names = _archive_names(wheels[0])
    for relative in REQUIRED_SDIST_MEMBERS:
        assert f"{SDIST_PREFIX}{relative}" in sdist_names
    sample = _sdist_text(sdists[0], f"{SDIST_PREFIX}.env.example")
    assert "ACB_DB=" in sample
    assert "real-env-sentinel-not-for-release" not in sample
    leaked_sdist = sorted(name for name in sdist_names if _is_excluded_member(name))
    leaked_wheel = sorted(name for name in wheel_names if _is_excluded_member(name))
    assert leaked_sdist == []
    assert leaked_wheel == []

    for resource in ("catalog.sqlite3", "manifest.json", "source.json"):
        assert f"{SDIST_PREFIX}src/agent_costbook/public_catalog/{resource}" in sdist_names
        assert f"agent_costbook/public_catalog/{resource}" in wheel_names

    assert "agent_costbook/__init__.py" in wheel_names
    metadata = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
    scripts = [name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")]
    assert len(metadata) == 1
    assert len(scripts) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        entry_points = archive.read(scripts[0]).decode("utf-8")
        package_metadata = archive.read(metadata[0]).decode("utf-8")
        license_files = [name for name in wheel_names if name.endswith(".dist-info/licenses/LICENSE")]
        assert len(license_files) == 1
        assert archive.read(license_files[0]).decode("utf-8") == (ROOT / "LICENSE").read_text()
    assert "License-Expression: MIT" in package_metadata
    assert "License-File: LICENSE" in package_metadata
    assert "License-Expression: MIT" in _sdist_text(sdists[0], f"{SDIST_PREFIX}PKG-INFO")
    for command in (
        "ac = agent_costbook.offline:main",
        "agent-costbook = agent_costbook.offline:main",
        "agent-costbook-export = agent_costbook.export:main",
        "agent-costbook-backup = agent_costbook.backup:main",
        "agent-costbook-migrate = agent_costbook.migrations:main",
    ):
        assert command in entry_points
    assert not any(
        name.startswith(("tests/", "examples/", "skills/", "src/", "docs/", "scripts/")) or name == ".env.example"
        for name in wheel_names
    )

    python = tmp_path / "venv" / "bin" / "python"
    venv = subprocess.run(
        ["uv", "venv", str(tmp_path / "venv")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert venv.returncode == 0, venv.stderr
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheels[0])],
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    version = subprocess.run(
        [str(python), "-c", "import agent_costbook; print(agent_costbook.__version__, end='')"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout == "1.2.0"
    startup = subprocess.run(
        [str(python), "-c", "from agent_costbook.public_api import create_public_app; app = create_public_app(); assert app.state.manifest['reviewed'] is True"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert startup.returncode == 0, startup.stderr
    bindir = python.parent
    for command in (
        "ac",
        "agent-costbook",
        "agent-costbook-export",
        "agent-costbook-backup",
        "agent-costbook-migrate",
    ):
        assert (bindir / command).is_file()
