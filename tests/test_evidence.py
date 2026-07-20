from __future__ import annotations

from evo_wiki.evidence import gate_lightrag_references


def test_gate_removes_reference_set_without_query_signal():
    references = [
        {"file_path": "unrelated.txt", "content": ["这段内容只讨论其他主题。"]},
    ]

    accepted, decision = gate_lightrag_references(
        "虚构机构的成立年份是什么？",
        references,
    )

    assert accepted == []
    assert decision["code"] == "IRRELEVANT_REFERENCES"
    assert decision["accepted_reference_count"] == 0


def test_gate_does_not_treat_generic_year_word_as_evidence():
    references = [
        {"file_path": "unrelated.txt", "content": ["其他案件在成立后进入审理程序。"]},
    ]

    accepted, decision = gate_lightrag_references(
        "虚构机构的成立年份是什么？",
        references,
    )

    assert accepted == []
    assert decision["code"] == "IRRELEVANT_REFERENCES"


def test_gate_keeps_relevant_reference_and_reports_match():
    references = [
        {"file_path": "case.txt", "content": ["韩永仁案中涉及自首的认定。"]},
    ]

    accepted, decision = gate_lightrag_references(
        "韩永仁案中为什么认定自首？",
        references,
    )

    assert accepted == references
    assert decision["status"] == "passed"
    assert decision["matched_signal_count"] >= 2


def test_gate_does_not_guess_when_chunk_content_is_missing():
    references = [{"file_path": "case.txt", "content": []}]

    accepted, decision = gate_lightrag_references(
        "韩永仁案中为什么认定自首？",
        references,
    )

    assert accepted == references
    assert decision["code"] == "CHUNK_CONTENT_EMPTY"


def test_gate_skips_short_ascii_query_to_avoid_false_filtering():
    references = [{"file_path": "doc.md", "content": ["supporting chunk"]}]

    accepted, decision = gate_lightrag_references("hello?", references)

    assert accepted == references
    assert decision["code"] == "QUERY_SIGNAL_TOO_SHORT"
