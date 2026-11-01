# -*- coding: utf-8 -*-
"""Build the bank technical brief as a typeset PDF."""
import re
import subprocess
from pathlib import Path

ROOT = Path(r"d:\ai-banking-agent")
SRC = ROOT / "docs" / "智汇银行智能服务系统技术说明书.md"
PDF_DIR = ROOT / "docs" / "pdf"
DIAGRAMS = PDF_DIR / "diagrams.tex"
BODY = PDF_DIR / "body.md"
TEX = PDF_DIR / "body.tex"
PREAMBLE = PDF_DIR / "preamble.tex"
COVER = PDF_DIR / "cover.tex"
OUT = ROOT / "docs" / "智汇银行智能服务系统技术说明书.pdf"

YAML = """---
documentclass: ctexart
classoption:
  - a4paper
  - zihao=5
  - fontset=none
geometry:
  - top=2.45cm
  - bottom=2.2cm
  - left=1.95cm
  - right=1.95cm
  - headheight=16pt
  - includehead
colorlinks: true
linkcolor: golddeep
toccolor: golddeep
urlcolor: terra
---
"""


def diagram_replacements() -> list[str]:
    raw = DIAGRAMS.read_text(encoding="utf-8")
    found = re.findall(r"DIAGRAM\d\n(.*?)(?=\nDIAGRAM\d|\Z)", raw, flags=re.S)
    if len(found) != 3:
        raise SystemExit(f"expected 3 diagrams, got {len(found)}")
    return [item.strip() + "\n" for item in found]


def build_markdown() -> None:
    text = SRC.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n.*?```", text, flags=re.S)
    if len(blocks) != 3:
        raise SystemExit(f"expected 3 mermaid blocks, got {len(blocks)}")
    for block, repl in zip(blocks, diagram_replacements()):
        text = text.replace(block, repl, 1)
    marker = "本文档说明系统的总体结构"
    start = text.find(marker)
    if start < 0:
        raise SystemExit("intro paragraph not found")
    intro_and_rest = text[start:]
    intro_and_rest = intro_and_rest.replace("\n---\n\n## 1.", "\n\n## 1.", 1)
    lines = (YAML + "\n## 编制说明\n\n" + intro_and_rest).splitlines()
    kept = []
    rules = 0
    for line in lines:
        if line.strip() == "---":
            rules += 1
            if rules <= 2:
                kept.append(line)
            continue
        kept.append(line)
    BODY.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _close_split_tables(tex: str) -> str:
    """Give a longtable a bottom rule on every page, not only the last one."""
    old = "\\endhead\n\\bottomrule\\noalign{}\n\\endlastfoot"
    new = (
        "\\endhead\n"
        "\\specialrule{0.6pt}{0.45em}{0pt}\n"
        "\\endfoot\n"
        "\\bottomrule\\noalign{}\n"
        "\\endlastfoot"
    )
    if old not in tex:
        raise SystemExit("longtable footer pattern not found")
    return tex.replace(old, new)


def run_pandoc() -> None:
    subprocess.run(
        [
            "pandoc",
            str(BODY),
            "-s",
            "-o",
            str(TEX),
            "-H",
            str(PREAMBLE),
            "-B",
            str(COVER),
        ],
        check=True,
    )
    TEX.write_text(_close_split_tables(TEX.read_text(encoding="utf-8")), encoding="utf-8")
    for _ in range(2):
        subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-output-directory",
                str(PDF_DIR),
                str(TEX),
            ],
            check=True,
        )
    (PDF_DIR / "body.pdf").replace(OUT)


if __name__ == "__main__":
    build_markdown()
    run_pandoc()
    print(OUT)
