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
    "案件",
    "问题",
    "相关",
    "有关",
    "内容",
    "情况",
    "什么",
    "为何",
    "为什么",
    "怎么",
    "如何",
    "是否",
    "可以",
    "需要",
    "应当",
    "法律",
    "法条",
    "条文",
    "规定",
}
_GENERIC_CJK_CHARACTERS = set("请给出当前语料没有涉及的并说明依据份")
_MIN_MATCHED_SIGNALS = 2
_BROAD_LAW_QUERY_RE = re.compile(
    r"(?:法条|条文|构成要件|法定刑|量刑标准|法律规定|法律依据|司法解释"
    r"|刑法第|民法第|行政法第)"
)
_STATUTORY_SUPPORT_RE = re.compile(
    r"(?:《[^》]{2,80}》|(?:中华人民共和国)?(?:刑法|民法典|"
    r"刑事诉讼法|民事诉讼法|行政处罚法)\s*第?[一二三四五六七八九十百千万零〇0-9]{0,12}条"
    r"|第[一二三四五六七八九十百千万零〇0-9]{1,12}条)"
)


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
    """Keep references only when they can support a trusted local citation.

    A reference with no chunk content is retained but reported because a
    delivery layer cannot prove its relevance. If there are fewer than two
    query signals, lexical evaluation is skipped here; the query gateway
    treats that outcome as insufficient for a *trusted* citation and can
    still deliver a general-model answer for human review.
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

    if _BROAD_LAW_QUERY_RE.search(query) and not _STATUTORY_SUPPORT_RE.search(
        combined_content
    ):
        return [], {
            "status": "warning",
            "code": "STATUTORY_SUPPORT_MISSING",
            "matched_signal_count": 0,
            "input_reference_count": input_count,
            "accepted_reference_count": 0,
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

    if len(matched) < _MIN_MATCHED_SIGNALS:
        return [], {
            "status": "warning",
            "code": "INSUFFICIENT_QUERY_SIGNAL_OVERLAP",
            "matched_signal_count": len(matched),
            "matched_signals": matched[:20],
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
