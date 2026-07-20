#!/usr/bin/env python3
"""Build the allow-listed Evo Wiki developer kit."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "evo_wiki" / "version.py"
SKILL_NAMES = (
    "evo-wiki-wiki",
    "evo-wiki-lightrag",
    "evo-wiki-operations",
)


def project_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"',
        VERSION_FILE.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("cannot read Evo Wiki version")
    return match.group(1)


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"required release file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_skill(
    source: Path,
    destination: Path,
    *,
    include_scripts: bool = True,
) -> None:
    copy_file(source / "SKILL.md", destination / "SKILL.md")
    copy_file(
        source / "agents" / "openai.yaml",
        destination / "agents" / "openai.yaml",
    )
    scripts = source / "scripts"
    if include_scripts and scripts.is_dir():
        shutil.copytree(
            scripts,
            destination / "scripts",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


def build_python_distributions(destination: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="evo-wiki-python-build-"
    ) as directory:
        output = Path(directory)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--outdir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        distributions = sorted(
            [
                *output.glob("*.whl"),
                *output.glob("*.tar.gz"),
            ]
        )
        if len(distributions) != 2:
            raise RuntimeError(
                "Python build must produce one wheel and one sdist"
            )
        destination.mkdir(parents=True, exist_ok=True)
        for artifact in distributions:
            shutil.copy2(artifact, destination / artifact.name)


def write_checksums(root: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def build_release(
    output_dir: Path,
    *,
    skip_python_build: bool,
) -> Path:
    version = project_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    release = output_dir / f"evo-wiki-{version}"
    archive = output_dir / f"evo-wiki-{version}.zip"
    if release.exists() or archive.exists():
        raise RuntimeError(
            f"release target already exists: {release} or {archive}"
        )

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".evo-wiki-{version}-",
            dir=output_dir,
        )
    )
    try:
        copy_file(ROOT / "README.md", staging / "README.md")
        copy_file(ROOT / "LICENSE", staging / "LICENSE")
        copy_skill(
            ROOT,
            staging / "skills" / "evo-wiki",
            include_scripts=False,
        )
        for name in SKILL_NAMES:
            copy_skill(
                ROOT / "skills" / name,
                staging / "skills" / name,
            )
        shutil.copytree(
            ROOT / "examples" / "local-platform",
            staging / "examples" / "local-platform",
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                "artifacts",
                "lightrag-config.json",
            ),
        )
        if not skip_python_build:
            build_python_distributions(staging / "python")
        write_checksums(staging)
        staging.replace(release)
        shutil.make_archive(
            str(archive.with_suffix("")),
            "zip",
            root_dir=release.parent,
            base_dir=release.name,
        )
        return release
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
    )
    parser.add_argument(
        "--skip-python-build",
        action="store_true",
        help="Stage Skills/examples only; intended for layout tests.",
    )
    args = parser.parse_args()
    release = build_release(
        args.output_dir.resolve(),
        skip_python_build=args.skip_python_build,
    )
    print(release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
