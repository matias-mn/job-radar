"""Pull plain text out of the resume (.docx or .pdf). The .docx path uses only the stdlib."""

import glob
import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The resume body starts at this header. Everything above it (name, phone,
# email, LinkedIn) is contact info we don't send to the API.
FIRST_SECTION = "EDUCATION"

SUFFIXES = (".docx", ".pdf")


def docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag in (f"{W}tab", f"{W}br"):
                parts.append(" ")
        text = " ".join("".join(parts).split())
        if text:
            paragraphs.append(text)
    return paragraphs


def pdf_lines(path: Path) -> list[str]:
    from pypdf import PdfReader  # only needed for a .pdf resume

    lines = []
    for page in PdfReader(path).pages:
        # Layout mode rebuilds visual lines. Plain mode gives one word per line
        # for Google Docs exports, which would hide the section headers.
        for line in page.extract_text(extraction_mode="layout").splitlines():
            text = " ".join(line.split())
            if text:
                lines.append(text)
    return lines


def resolve_resume(path: str | Path) -> Path:
    """resume_path may be a glob ("~/Resumes/Resume MASTER*"): the newest .docx/.pdf match wins,
    so a re-export saved as "... MASTER-1.pdf" is picked up without a config change."""
    expanded = Path(path).expanduser()
    if not glob.has_magic(str(expanded)):
        return expanded
    matches = [p for p in map(Path, glob.glob(str(expanded)))
               if p.suffix.lower() in SUFFIXES and not p.name.startswith("~$")]  # ~$ = Word lock file
    if not matches:
        raise FileNotFoundError(f"no .docx or .pdf resume matches {path}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def load_resume(path: str | Path) -> tuple[str, str]:
    """Returns (resume text without contact info, short sha256 of that text)."""
    file = resolve_resume(path)
    suffix = file.suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError(f"resume must be .docx or .pdf, got {file.name}")
    paragraphs = pdf_lines(file) if suffix == ".pdf" else docx_paragraphs(file)
    start = next((i for i, p in enumerate(paragraphs) if p.upper().startswith(FIRST_SECTION)), 0)
    text = "\n".join(paragraphs[start:])
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]
