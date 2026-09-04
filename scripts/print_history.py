#!/usr/bin/env python3
"""Print a readable KOT transcript for one FrontierHarness task."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "data" / "history-index.json"


def render_content(role: str, content: object) -> list[str]:
    if isinstance(content, str):
        return [f"[{role}] {content}"]
    if not isinstance(content, list):
        return []
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            lines.append(f"[{role} text] {block.get('text', '')}")
        elif kind == "thinking":
            lines.append(f"[{role} thinking] {block.get('thinking', '')}")
        elif kind == "tool_use":
            lines.append(f"[{role} tool_use {block.get('name')}] {json.dumps(block.get('input'), ensure_ascii=False)}")
        elif kind == "tool_result":
            lines.append(f"[{role} tool_result {block.get('tool_name') or block.get('tool_use_id')}] {json.dumps(block.get('content'), ensure_ascii=False)}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="For example: terminal-bench/chess-best-move")
    parser.add_argument("--tail", type=int, default=0, help="Print only the final N rendered blocks")
    args = parser.parse_args()

    index = json.loads(INDEX.read_text(encoding="utf-8"))["histories"]
    matches = [row for row in index if row["task_id"] == args.task_id]
    if len(matches) != 1:
        known = "\n".join(row["task_id"] for row in index)
        raise SystemExit(f"unknown task {args.task_id!r}; available tasks:\n{known}")
    row = matches[0]
    transcript = ROOT / row["transcript"]
    rendered: list[str] = []
    with transcript.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("type") == "user" and not record.get("is_meta"):
                rendered.extend(render_content("user", record.get("content")))
            elif record.get("type") == "assistant":
                message = record.get("message") or {}
                rendered.extend(render_content("assistant", message.get("content")))
            elif record.get("type") == "user" and record.get("is_meta"):
                rendered.extend(render_content("tool", record.get("content")))
    if args.tail > 0:
        rendered = rendered[-args.tail :]
    print(f"task: {row['task_id']}\njob: {row['job']}\ntrial: {row['trial']}\ntranscript: {row['transcript']}\n")
    print("\n\n".join(rendered))


if __name__ == "__main__":
    main()
