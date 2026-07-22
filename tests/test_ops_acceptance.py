from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evo_wiki.cli import main
from evo_wiki.ops_acceptance import (
    QG001AcceptanceService,
    _notification_payload_safe,
    _stop_labelled_gateway,
    _workspace_manifest,
    cleanup_acceptance_run,
)
from evo_wiki.state.contracts import StateError


def test_acceptance_plan_is_zero_source_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "guard.txt").write_text("unchanged", encoding="utf-8")
    report = tmp_path / "summary.json"
    before = _workspace_manifest(source)

    import evo_wiki.ops_acceptance as acceptance

    monkeypatch.setattr(acceptance, "_command_ok", lambda _command: True)
    monkeypatch.setattr(
        acceptance,
        "_docker_image_present",
        lambda _image: True,
    )
    monkeypatch.setattr(acceptance.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(
        QG001AcceptanceService,
        "_source_summary",
        lambda _self: {
            "database_schema_version": 1,
            "state_commit_seq": 15,
            "remote": {
                "document_count": 9,
                "status_counts": {"processed": 9},
                "chunks_count": 34,
                "pipeline_idle": True,
            },
        },
    )

    service = QG001AcceptanceService(
        source_root=source,
        report_path=report,
        provider_env_file=None,
        allow_image_pull=False,
    )
    result = service.plan()

    assert result["status"] == "ready"
    assert result["mode"] == "dry_run"
    assert result["source_workspace_mutated"] is False
    assert result["workspace_mutated"] is False
    assert not report.exists()
    assert _workspace_manifest(source) == before


def test_acceptance_cli_writes_only_sanitized_dry_run_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "guard.txt").write_text("unchanged", encoding="utf-8")
    report = tmp_path / "report.json"
    before = _workspace_manifest(source)

    import evo_wiki.ops_acceptance as acceptance

    monkeypatch.setattr(acceptance, "_command_ok", lambda _command: True)
    monkeypatch.setattr(
        acceptance,
        "_docker_image_present",
        lambda _image: True,
    )
    monkeypatch.setattr(acceptance.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(
        QG001AcceptanceService,
        "_source_summary",
        lambda _self: {
            "database_schema_version": 1,
            "state_commit_seq": 15,
            "remote": {
                "document_count": 9,
                "status_counts": {"processed": 9},
                "chunks_count": 34,
                "pipeline_idle": True,
            },
        },
    )

    exit_code = main(
        [
            "gateway",
            "acceptance",
            "--root",
            str(source),
            "--report",
            str(report),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    persisted = json.loads(report.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert output == persisted
    assert persisted["source_workspace_mutated"] is False
    assert str(source) not in report.read_text(encoding="utf-8")
    assert _workspace_manifest(source) == before


def test_acceptance_cleanup_requires_exact_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "gateway",
            "acceptance-cleanup",
            "--run-id",
            "qg001-1234567890abcdef",
            "--confirm",
            "different",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert (
        payload["error_code"]
        == "QG_ACCEPTANCE_CLEANUP_CONFIRMATION_MISMATCH"
    )


def test_acceptance_apply_requires_card_confirmation(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.env"
    provider.write_text("SAFE=value\n", encoding="utf-8")
    service = QG001AcceptanceService(
        source_root=tmp_path,
        report_path=tmp_path.parent / "report.json",
        provider_env_file=provider,
        allow_image_pull=False,
    )
    with pytest.raises(StateError) as raised:
        service.apply(confirm="wrong")
    assert (
        raised.value.error_code
        == "QG_ACCEPTANCE_CONFIRMATION_MISMATCH"
    )


def test_notification_privacy_scanner_rejects_nested_content() -> None:
    assert _notification_payload_safe(
        {
            "schema_version": 1,
            "event_type": "AUDIT_OPENED",
            "subject": {"type": "audit", "id": "audit-1"},
            "counts": {"open": 1},
        }
    )
    assert not _notification_payload_safe(
        {"subject": {"id": "audit-1"}, "answer": "secret"}
    )


def test_acceptance_cleanup_finds_runtime_without_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evo_wiki.ops_acceptance as acceptance

    run_id = "qg001-1234567890abcdef"
    runtime = tmp_path / f"evo-wiki-{run_id}-orphan"
    runtime.mkdir()
    (runtime / "temporary.txt").write_text("temporary", encoding="utf-8")
    monkeypatch.setattr(
        acceptance.tempfile,
        "gettempdir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        acceptance,
        "_run_output",
        lambda *_args, **_kwargs: "",
    )

    result = cleanup_acceptance_run(run_id)

    assert result["status"] == "cleaned"
    assert result["runtime_directories_removed"] == 1
    assert result["gateway_processes_stopped"] == 0
    assert not runtime.exists()


def test_acceptance_cleanup_stops_only_matching_gateway_pid(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "evo-wiki-qg001-fixture"
    synthetic = runtime / "synthetic"
    synthetic.mkdir(parents=True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "evo_wiki.cli",
            "gateway",
            "serve",
            "--root",
            str(synthetic.resolve()),
        ]
    )
    (runtime / "gateway.pid").write_text(
        f"{process.pid}\n",
        encoding="ascii",
    )
    try:
        assert _stop_labelled_gateway(runtime) is True
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
