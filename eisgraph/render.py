"""Render surya-2 per-page JSON into reading-ordered text for extraction."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

SKIP_LABELS = {"PageHeader", "PageFooter"}  # running headers/footers add noise to every chunk
PAGE_MARK = "[[page {}]]"


class _Flatten(HTMLParser):
    """HTML -> text: <br>/<p>/<li> become newlines, table rows become ' | '-joined lines."""

    def __init__(self):
        super().__init__()
        self.out, self.row = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.row.append("")
        elif tag in ("br", "p", "li") and self.row is None:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "tr" and self.row is not None:
            self.out.append("\n" + " | ".join(c.strip() for c in self.row))
            self.row = None

    def handle_data(self, data):
        if self.row is not None and self.row:
            self.row[-1] += data
        elif self.row is None:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _Flatten()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _truthy(v) -> bool:
    return v is True or str(v).lower() == "true"


def render_page(page: dict) -> str:
    blocks = sorted(page["ocr_data"]["blocks"], key=lambda b: int(b.get("reading_order", 0)))
    parts = []
    for b in blocks:
        if b.get("label") in SKIP_LABELS or _truthy(b.get("skipped")) or _truthy(b.get("error")):
            continue
        text = html_text(b.get("html") or "")
        if not text:
            continue
        if b.get("label") == "SectionHeader":
            text = "## " + text.replace("\n", " ")
        parts.append(text)
    return "\n\n".join(parts)


def render_doc(doc_dir: Path) -> str:
    """All pages of one barcode, in page order, each preceded by a [[page N]] marker."""
    pages = []
    for f in (doc_dir / "json").glob("*.json"):
        raw = f.read_text(errors="replace")
        try:
            page = json.loads(raw)
        except json.JSONDecodeError:  # a few jobs have plain OCR text saved under .json; keep it as-is
            pages.append((int(f.stem) if f.stem.isdigit() else 0, raw.strip()))
            continue
        pages.append((int(page.get("page_number") or f.stem), render_page(page)))
    return "\n\n".join(f"{PAGE_MARK.format(n)}\n{t}" for n, t in sorted(pages))
