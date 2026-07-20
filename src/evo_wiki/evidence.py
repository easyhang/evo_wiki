"""Small, conservative evidence gates for LightRAG references.

This is deliberately an evidence-selection guard, not a semantic claim
verifier. It only removes a reference set when no meaningful query signal
appears in any returned chunk. Removing evidence does not suppress an answer:
the query gateway may fall back to LightRAG bypass and request human review.
"""

from __future__ import annotations

import re
from typing import Any


_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_GENERIC_CJK_TOKENS = {
    "请给",
    "给出",
    "当前",
    "语料",
    "没有",
    "涉及",
    "说明",
    "依据",
    "并说",
    "的确",
    "机构",
    "成立",
    "年份",
}
_GENERIC_CJK_CHARACTERS = set("请给出当前语料没有涉及的并说明依据份")


def evidence_signal_tokens(text: str) -> set[str]:
    """Extract conservative ASCII words and overlapping CJK bigrams."""
    tokens = {token.lower() for token in _ASCII_TOKEN_RE.findall(text)}
    cjk = "".join(character for character in text if _CJK_RE.fullmatch(character))
    tokens.update(
        cjk[index : index + 2]
        for index in range(max(0, len(cjk) - 1))
        if cjk[index : index + 2] not in _GENERIC_CJK_TOKENS
        and not any(character in _GENERIC_CJK_CHARACTERS for character in cjk[index : index + 2])
    )
    return tokens


def gate_lightrag_references(
    query: str,
    references: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reject the evidence set only when it is entirely unrelated.

    A reference with no chunk content is retained but reported because a
    delivery layer cannot prove its relevance. If there are fewer than two
    query signals, lexical evaluation is skipped to avoid filtering short or
    highly paraphrased questions.
    """
    normalized = [dict(reference) for reference in references if isinstance(reference, dict)]
    input_count = len(normalized)
    if not normalized:
        return [], {
            "status": "warning",
            "code": "REFERENCES_EMPTY",
            "matched_signal_count": 0,
            "input_reference_count": 0,
            "accepted_reference_count": 0,
        }

    query_tokens = evidence_signal_tokens(query)
    if len(query_tokens) < 2:
        return normalized, {
            "status": "not_evaluated",
            "code": "QUERY_SIGNAL_TOO_SHORT",
            "matched_signal_count": 0,
            "input_reference_count": input_count,
            "accepted_reference_count": input_count,
        }

    combined_content = " ".join(
        part
        for reference in normalized
        for part in _reference_content(reference)
    )
    if not combined_content:
        return normalized, {
            "status": "warning",
            "code": "CHUNK_CONTENT_EMPTY",
            "matched_signal_count": 0,
            "input_reference_count": input_count,
            "accepted_reference_count": input_count,
        }

    matched = sorted(token for token in query_tokens if token in combined_content.lower())
    if not matched:
        return [], {
            "status": "warning",
            "code": "IRRELEVANT_REFERENCES",
            "matched_signal_count": 0,
            "input_reference_count": input_count,
            "accepted_reference_count": 0,
        }

    return normalized, {
        "status": "passed",
        "code": None,
        "matched_signal_count": len(matched),
        "matched_signals": matched[:20],
        "input_reference_count": input_count,
        "accepted_reference_count": input_count,
    }


def _reference_content(reference: dict[str, Any]) -> list[str]:
    content = reference.get("content")
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        return [part for part in content if isinstance(part, str) and part]
    return []
