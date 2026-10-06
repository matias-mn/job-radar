"""Pull plain text out of the resume .docx using only the stdlib."""

import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The resume body starts at this header. Everything above it (name, phone,
# email, LinkedIn) is contact info we don't send to the API.
FIRST_SECTION = "EDUCATION"


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


def load_resume(path: str | Path) -> tuple[str, str]:
    """Returns (resume text without contact info, short sha256 of that text)."""
    paragraphs = docx_paragraphs(Path(path).expanduser())
    start = next((i for i, p in enumerate(paragraphs) if p.upper().startswith(FIRST_SECTION)), 0)
    text = "\n".join(paragraphs[start:])
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]
