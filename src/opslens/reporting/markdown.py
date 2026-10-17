"""Stable Markdown with escaped cells and no inferred narrative."""

import json

from opslens.reporting.models import VerifiedReport


def escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("`", "&#96;")
    )


def render_markdown(report: VerifiedReport) -> str:
    lines = [
        f"# {escape(report.title)}",
        "",
        f"Overall: {report.overall_status}",
        "",
        "## Verified Facts",
        "",
    ]
    if not report.verified_facts:
        lines.append("没有通过全部验证的事实。")
    for fact in report.verified_facts:
        lines.append(
            f"{fact.kind}: {fact.metric_id or 'data query'}"
            + (f"@{fact.metric_version} ({fact.unit})" if fact.metric_id else "")
        )
        lines += [
            "",
            "| " + " | ".join(escape(c) for c in fact.columns) + " |",
            "| " + " | ".join("---" for _ in fact.columns) + " |",
        ]
        for row in fact.rows:
            lines.append("| " + " | ".join("NULL" if c is None else escape(c) for c in row) + " |")
        lines += [
            "",
            f"Total rows: {fact.total_row_count}; truncated: {json.dumps(fact.truncated)}",
        ]
    lines += ["", "## Interpretations", ""]
    lines += [f"- [{i.status}] {escape(i.text)} ({i.reason_code})" for i in report.interpretations]
    if not report.interpretations:
        lines.append("不自动生成解释性或因果结论。")
    lines += ["", "## Uncertainty", ""]
    lines += [
        f"- {u.validator}: {u.reason_code} — {escape(u.summary)}" for u in report.uncertainties
    ]
    lines += [f"- {w}" for w in report.warnings]
    if not report.uncertainties and not report.warnings:
        lines.append("当前验证范围内没有未解决项。")
    return "\n".join(lines) + "\n"
