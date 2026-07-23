from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml


def test_release_builder_stages_allow_list_only(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "build_release.py"),
            "--output-dir",
            str(tmp_path),
            "--skip-python-build",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    release = tmp_path / "evo-wiki-2.0.1"
    assert (release / "README.md").is_file()
    assert (release / "LICENSE").is_file()
    assert (release / "CHANGELOG.md").is_file()
    assert (release / "docs" / "development-standard.md").is_file()
    assert (release / "docs" / "upgrading-to-2.0.md").is_file()
    for skill in (
        "evo-wiki",
        "evo-wiki-wiki",
        "evo-wiki-lightrag",
        "evo-wiki-operations",
    ):
        assert (release / "skills" / skill / "SKILL.md").is_file()
        assert (
            release
            / "skills"
            / skill
            / "agents"
            / "openai.yaml"
        ).is_file()
        assert not (
            release / "skills" / skill / "README.md"
        ).exists()
        metadata = yaml.safe_load(
            (
                release
                / "skills"
                / skill
                / "agents"
                / "openai.yaml"
            ).read_text(encoding="utf-8")
        )
        interface = metadata["interface"]
        assert 25 <= len(interface["short_description"]) <= 64
        assert f"${skill}" in interface["default_prompt"]
    assert (
        release
        / "examples"
        / "local-platform"
        / "wiki.example.json"
    ).is_file()
    assert (
        release
        / "examples"
        / "local-platform"
        / "wiki-src-template"
        / "sources"
        / "example-source.md"
    ).is_file()
    assert not (
        release
        / "skills"
        / "evo-wiki"
        / "scripts"
        / "build_release.py"
    ).exists()
    assert not list(release.rglob("*.sqlite3"))
    assert not list(release.rglob("lightrag-config.json"))
    assert not (release / "python").exists()
    released_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in release.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    assert "韩永仁" not in released_text
    assert "李万华" not in released_text
    assert (tmp_path / "evo-wiki-2.0.1.zip").is_file()

    checksum_lines = (
        release / "SHA256SUMS"
    ).read_text(encoding="utf-8").splitlines()
    assert checksum_lines
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256(
            (release / relative).read_bytes()
        ).hexdigest() == digest
