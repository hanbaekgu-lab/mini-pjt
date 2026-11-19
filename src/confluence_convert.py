"""Markdown → Confluence 저장 형식(storage format, XHTML) 변환기.

MCP 서버의 create_page가 게시 직전에 호출한다(에이전트는 Markdown만 넘긴다).
회의 자료에 쓰는 문법만 지원: 제목, 문단, 구분선, 인용(→ info 패널), 목록, 체크리스트(→ 작업 목록), 표, **굵게**, `코드`,
펜스 코드 블록(```  → code 매크로, 고정폭 타임라인 도식용),
지난주 대비 증감 표시(``▲ +1.86%p`` → 초록 글자, ``▼ -0.50%p`` → 빨강 글자).
"""
from __future__ import annotations

import html
import re

_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
UP_COLOR, DOWN_COLOR = "rgb(0,135,90)", "rgb(222,53,11)"  # 증가 초록, 감소 빨강
_UP = re.compile(r"▲ \+\d+(?:\.\d+)?(?:%p|MM|개|건)?")
_DOWN = re.compile(r"▼ -\d+(?:\.\d+)?(?:%p|MM|개|건)?")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = _UP.sub(lambda m: f'<span style="color: {UP_COLOR};">{m.group(0)}</span>', out)
    out = _DOWN.sub(lambda m: f'<span style="color: {DOWN_COLOR};">{m.group(0)}</span>', out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _align(sep: str) -> list[str]:
    out = []
    for c in _cells(sep):
        out.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left")
    return out


def _table(rows: list[str]) -> str:
    head, aligns, body = _cells(rows[0]), _align(rows[1]), [_cells(r) for r in rows[2:]]
    style = lambda i: f' style="text-align: {aligns[i]};"' if i < len(aligns) and aligns[i] != "left" else ""  # noqa: E731
    th = "".join(f"<th{style(i)}>{_inline(c)}</th>" for i, c in enumerate(head))
    trs = "".join("<tr>" + "".join(f"<td{style(i)}>{_inline(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in body)
    return f"<table><tbody><tr>{th}</tr>{trs}</tbody></table>"


def md_to_storage(md: str) -> str:
    lines, out, i, task_no = md.splitlines(), [], 0, 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
        elif re.fullmatch(r"-{3,}|\*{3,}", line.strip()):  # 구분선 (게시 꼬리말 앞)
            out.append("<hr />")
            i += 1
        elif line.startswith("```"):  # 펜스 코드 블록 → code 매크로 (본문은 CDATA로 그대로)
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i].rstrip())
                i += 1
            i += 1  # 닫는 ```
            body = "\n".join(code).replace("]]>", "]]]]><![CDATA[>")
            out.append('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
                       f"<![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>")
        elif line.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append('<ac:structured-macro ac:name="info"><ac:rich-text-body>'
                       f"<p>{_inline(' '.join(quote))}</p></ac:rich-text-body></ac:structured-macro>")
        elif line.startswith("- [ ] ") or line.startswith("- [x] "):
            tasks = []
            while i < len(lines) and re.match(r"^- \[[ x]\] ", lines[i]):
                task_no += 1
                status = "complete" if lines[i].startswith("- [x]") else "incomplete"
                tasks.append(f"<ac:task><ac:task-id>{task_no}</ac:task-id><ac:task-status>{status}</ac:task-status>"
                             f"<ac:task-body>{_inline(lines[i][6:].strip())}</ac:task-body></ac:task>")
                i += 1
            out.append("<ac:task-list>" + "".join(tasks) + "</ac:task-list>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- ") and not re.match(r"^- \[[ x]\] ", lines[i]):
                items.append(f"<li>{_inline(lines[i][2:].strip())}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
            i += 1
    return "".join(out)
