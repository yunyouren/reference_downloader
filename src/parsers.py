"""Reference parsing functionality extracted from reference_tool.py.

Provides PDF text extraction, reference section detection, and reference
entry splitting/parsing for both numeric and non-numeric reference styles.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from src.models import ReferenceItem

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader  # type: ignore

try:  # Optional dependency
    import pdfplumber  # type: ignore[import-not-found,import-untyped]
except ImportError:  # pragma: no cover
    pdfplumber = None  # type: ignore


DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\]]+", re.IGNORECASE)

# 数字编号条目提取：匹配 [1] / 1. / 1) / (1) / （1） 等起始编号，并捕获条目正文。
NUMERIC_REF_RE = re.compile(
    r"(?ms)^\s*(?:\[(\d+)\]|(\d+)[\.\)]|[\(（](\d+)[\)）])\s+(.*?)(?=^\s*(?:\[\d+\]|\d+[\.\)]|[\(（]\d+[\)）])\s+|\Z)"
)

# 参考文献章节标题（中英文常见写法）。
REF_HEADING_RE = re.compile(
    r"(?im)^\s*(references|bibliography|works cited|reference list|参考文献)\s*$"
)
# 参考文献章节结束位置的启发式检测：常见落在附录、致谢、作者介绍等之前。
REF_END_RE = re.compile(
    r"(?im)^\s*(appendix|appendices|acknowledg(e)?ments?|about the authors?)\b"
)
# 非数字编号分段时，用于判断"这一行很可能是一个新条目的开头"（APA 风格常见）。
AUTHOR_YEAR_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\((?:19|20)\d{2}[a-z]?\)"
)
# 非数字编号分段时，用于判断"这一行很可能是一个新条目的开头"（MLA 风格启发式）。
MLA_LIKE_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\.\s+.+"
)


def read_pdf_text_pypdf(pdf_path: Path) -> str:
    """使用 pypdf/PyPDF2 提取 PDF 全文文本（简单直观，但易混入页眉页脚）。"""
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def read_pdf_text_pdfplumber(pdf_path: Path, header_margin: float, footer_margin: float) -> str:
    """
    使用 pdfplumber 提取 PDF 文本，并通过裁剪 bbox 尽量避开页眉页脚。

    header_margin/footer_margin 为裁剪边距（单位：PDF 坐标点）。
    """
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            bbox = (0, header_margin, page.width, max(header_margin + 1, page.height - footer_margin))
            text = (page.within_bbox(bbox).extract_text() or "").strip()
            if not text:
                text = (page.extract_text() or "").strip()
            pages.append(text)
    return "\n".join(pages)


def read_pdf_text(
    pdf_path: Path,
    parser: str,
    header_margin: float,
    footer_margin: float,
) -> str:
    """根据命令行参数选择 PDF 文本提取后端。"""
    if parser == "pdfplumber":
        if pdfplumber is None:
            print("pdfplumber is not installed; falling back to pypdf.", file=sys.stderr)
            return read_pdf_text_pypdf(pdf_path)
        return read_pdf_text_pdfplumber(pdf_path, header_margin=header_margin, footer_margin=footer_margin)
    return read_pdf_text_pypdf(pdf_path)


def cleanup_reference_text(text: str) -> str:
    """
    清洗参考文献条目文本。

    目的：
    - 将常见的"智能引号"转为普通引号，便于后续正则处理与输出一致性；
    - 处理 PDF 复制文本中常见的断词/断行（例如行尾连字符 + 换行）；
    - 将多行压成一行，规整空白字符。
    """
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_references_section(full_text: str) -> str:
    """
    从全文文本中截取"参考文献/References"章节。

    实现思路：
    - 先用 REF_HEADING_RE 找到可能的标题行；
    - 为避免误匹配目录或前文提到的 "References"，优先选择位于全文后 70% 区域的标题；
    - 再用 REF_END_RE 启发式找一个结束点（附录/致谢等）。
    """
    heading_matches = list(REF_HEADING_RE.finditer(full_text))
    if not heading_matches:
        raise ValueError("Could not find references heading in PDF text.")

    # Prefer headings in later part of document.
    threshold = int(len(full_text) * 0.3)
    start_match = next((m for m in heading_matches if m.start() >= threshold), heading_matches[-1])
    start = start_match.end()
    tail = full_text[start:]
    end_match = REF_END_RE.search(tail)
    if end_match:
        return tail[: end_match.start()]
    return tail


def parse_numeric_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    解析"数字编号"风格的参考文献。

    特点：
    - 条目以编号开头（[1] / 1. / (1) 等）
    - 使用 NUMERIC_REF_RE 进行跨行匹配，提取每个条目块
    """
    refs: list[ReferenceItem] = []
    for match in NUMERIC_REF_RE.finditer(ref_section_text):
        number = int(match.group(1) or match.group(2) or match.group(3))
        raw = cleanup_reference_text(match.group(4))
        if not raw:
            continue
        dois = sorted({d.rstrip(".,;") for d in DOI_RE.findall(raw)})
        urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(raw)})
        refs.append(ReferenceItem(number=number, text=raw, dois=dois, urls=urls))
    return refs


def is_reference_start_line(line: str) -> bool:
    """
    判断一行文本是否像"非数字编号"参考文献条目的起始行。

    这是启发式规则：宁可略微误判，也要在常见 APA/MLA 样式下能较好分段。
    """
    if AUTHOR_YEAR_START_RE.match(line):
        return True
    if MLA_LIKE_START_RE.match(line):
        return True
    # Another common pattern: "Author et al., 2021, ..."
    if re.match(r"^[A-Z].+et al\.,\s*(?:19|20)\d{2}", line):
        return True
    return False


def parse_non_numeric_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    解析"非数字编号"风格的参考文献（例如 APA/MLA）。

    处理步骤：
    - 按行拆分并清理空行/纯页码行；
    - 用 is_reference_start_line() 判断何时开始新条目；
    - 如果分段效果很差，再退化为按空行块切分。
    """
    lines = [ln.strip() for ln in ref_section_text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"\d{1,3}", ln)]

    refs_text: list[str] = []
    current: list[str] = []
    for line in lines:
        if is_reference_start_line(line) and current:
            refs_text.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        refs_text.append(" ".join(current))

    # Fallback: if still poor segmentation, split by blank-line blocks.
    if len(refs_text) <= 1:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", ref_section_text) if b.strip()]
        if blocks:
            refs_text = blocks

    refs: list[ReferenceItem] = []
    for idx, raw_text in enumerate(refs_text, start=1):
        raw = cleanup_reference_text(raw_text)
        if len(raw) < 20:
            continue
        dois = sorted({d.rstrip(".,;") for d in DOI_RE.findall(raw)})
        urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(raw)})
        refs.append(ReferenceItem(number=idx, text=raw, dois=dois, urls=urls))
    return refs


def split_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    统一入口：自动选择更合适的参考文献解析策略。

    规则：
    - 先尝试数字编号解析；如果能解析出 >=2 条，基本可判定为数字风格；
    - 否则尝试非数字风格解析；
    - 两者都失败则抛出异常。
    """
    numeric = parse_numeric_references(ref_section_text)
    if numeric and len(numeric) >= 2:
        return numeric
    non_numeric = parse_non_numeric_references(ref_section_text)
    if non_numeric:
        return non_numeric
    raise ValueError("Unable to parse references from section.")
