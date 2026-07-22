#!/usr/bin/env python3
"""Merge zh.ass + en.ass into a bilingual en-zh.ass file."""

from __future__ import annotations

import argparse
import os
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueEvent:
    raw: str
    layer: str
    start: str
    end: str
    style: str
    name: str
    margin_l: str
    margin_r: str
    margin_v: str
    effect: str
    text: str
    index: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.start, self.end)

    def sort_key(self) -> tuple[int, int, int]:
        return (ass_time_sort_key(self.start), ass_time_sort_key(self.end), self.index)

    def with_text(self, text: str) -> str:
        return (
            f"Dialogue: {self.layer},{self.start},{self.end},{self.style},{self.name},"
            f"{self.margin_l},{self.margin_r},{self.margin_v},{self.effect},{text}"
        )


@dataclass(frozen=True)
class AssDocument:
    preamble: list[str]
    section_order: list[str]
    sections: dict[str, list[str]]
    dialogues: list[DialogueEvent]


def parse_ass(text: str) -> AssDocument:
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    section_order: list[str] = []
    current_section = ""
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            if current_section:
                sections[current_section] = current_lines
                section_order.append(current_section)
            else:
                preamble = current_lines
            current_section = line
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = current_lines
        section_order.append(current_section)
    else:
        preamble = current_lines

    dialogues = parse_dialogues(sections.get("[Events]", []))
    return AssDocument(preamble=preamble, section_order=section_order, sections=sections, dialogues=dialogues)


def parse_dialogues(event_lines: list[str]) -> list[DialogueEvent]:
    dialogues: list[DialogueEvent] = []
    for index, line in enumerate(event_lines):
        if not line.startswith("Dialogue:"):
            continue
        payload = line[len("Dialogue:") :].lstrip()
        parts = payload.split(",", 9)
        if len(parts) != 10:
            raise ValueError(f"invalid Dialogue line: {line}")
        dialogues.append(DialogueEvent(line, *parts, index=index))
    return dialogues


def ass_time_sort_key(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    whole_seconds, centiseconds = seconds.split(".")
    return (
        int(hours) * 360000
        + int(minutes) * 6000
        + int(whole_seconds) * 100
        + int(centiseconds)
    )


def style_name(line: str) -> str:
    if not line.startswith("Style:"):
        return ""
    body = line[len("Style:") :].lstrip()
    name, _, _ = body.partition(",")
    return name.strip()


def merge_style_section(primary_lines: list[str], secondary_lines: list[str]) -> list[str]:
    merged = list(primary_lines)
    existing = {style_name(line) for line in primary_lines if style_name(line)}
    extras = [
        line
        for line in secondary_lines
        if line.startswith("Style:") and style_name(line) and style_name(line) not in existing
    ]
    if not extras:
        return merged

    insert_at = len(merged)
    for index, line in enumerate(merged):
        if line.startswith("Style:"):
            insert_at = index + 1
    merged[insert_at:insert_at] = extras
    return merged


def merge_dialogues(zh_dialogues: list[DialogueEvent], en_dialogues: list[DialogueEvent]) -> list[str]:
    zh_by_key: dict[tuple[str, str], deque[DialogueEvent]] = defaultdict(deque)
    en_by_key: dict[tuple[str, str], deque[DialogueEvent]] = defaultdict(deque)
    for event in zh_dialogues:
        zh_by_key[event.key].append(event)
    for event in en_dialogues:
        en_by_key[event.key].append(event)

    output: list[tuple[tuple[int, int, int, int], str]] = []
    keys = {
        *zh_by_key.keys(),
        *en_by_key.keys(),
    }

    for key in keys:
        zh_items = zh_by_key[key]
        en_items = en_by_key[key]
        pair_count = min(len(zh_items), len(en_items))
        for _ in range(pair_count):
            zh_event = zh_items.popleft()
            en_event = en_items.popleft()
            merged_text = f"{zh_event.text}\\N{{\\r{en_event.style}}}{en_event.text}"
            output.append(((zh_event.sort_key()[0], zh_event.sort_key()[1], 0, zh_event.index), zh_event.with_text(merged_text)))
        while zh_items:
            zh_event = zh_items.popleft()
            output.append(((zh_event.sort_key()[0], zh_event.sort_key()[1], 0, zh_event.index), zh_event.raw))
        while en_items:
            en_event = en_items.popleft()
            output.append(((en_event.sort_key()[0], en_event.sort_key()[1], 1, en_event.index), en_event.raw))

    output.sort(key=lambda item: item[0])
    return [line for _, line in output]


def build_output_document(zh_doc: AssDocument, en_doc: AssDocument) -> str:
    sections = {name: list(lines) for name, lines in zh_doc.sections.items()}

    if "[V4+ Styles]" in sections:
        sections["[V4+ Styles]"] = merge_style_section(
            sections["[V4+ Styles]"],
            en_doc.sections.get("[V4+ Styles]", []),
        )

    zh_event_lines = sections.get("[Events]", [])
    event_prefix = [line for line in zh_event_lines if not line.startswith("Dialogue:")]
    event_dialogues = merge_dialogues(zh_doc.dialogues, en_doc.dialogues)
    sections["[Events]"] = event_prefix + event_dialogues

    out_lines: list[str] = []
    out_lines.extend(zh_doc.preamble)
    for section_name in zh_doc.section_order:
        out_lines.append(section_name)
        out_lines.extend(sections.get(section_name, []))
    return "\n".join(out_lines).rstrip() + "\n"


def default_output_path(zh_path: str) -> str:
    abs_path = os.path.abspath(zh_path)
    if abs_path.endswith(".zh.ass"):
        return abs_path[:-7] + ".en-zh.ass"
    if abs_path.endswith(".ass"):
        return abs_path[:-4] + ".en-zh.ass"
    return abs_path + ".en-zh.ass"


def merge_ass_files(zh_path: str, en_path: str, output_path: str | None = None) -> str:
    with open(zh_path, "r", encoding="utf-8") as f:
        zh_doc = parse_ass(f.read())
    with open(en_path, "r", encoding="utf-8") as f:
        en_doc = parse_ass(f.read())

    final_output = output_path or default_output_path(zh_path)
    content = build_output_document(zh_doc, en_doc)
    with open(final_output, "w", encoding="utf-8") as f:
        f.write(content)
    return os.path.abspath(final_output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge a .zh.ass and .en.ass pair into a bilingual .en-zh.ass file."
    )
    parser.add_argument("zh_ass", help="Path to the target-language .zh.ass file.")
    parser.add_argument("en_ass", help="Path to the source-language .en.ass file.")
    parser.add_argument("-o", "--output", help="Optional output path for the merged .en-zh.ass file.")
    args = parser.parse_args()

    output_path = merge_ass_files(args.zh_ass, args.en_ass, args.output)
    print(output_path)


if __name__ == "__main__":
    main()
