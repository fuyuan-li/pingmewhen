from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from pypdf import PdfReader

from relay_agent.event_log import EventLog, default_data_dir


MAX_PDF_BYTES = 10 * 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".text", ".log"}
ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{2,60}\s(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct)\b[^\n]{0,60}",
    re.IGNORECASE,
)


class InvalidContext(ValueError):
    pass


class ContextStore:
    def __init__(self, events: EventLog, root: Path | None = None) -> None:
        self._events = events
        self._root = root or default_data_dir() / "contexts"
        self._root.mkdir(parents=True, exist_ok=True)

    def save_document(self, filename: str, content: bytes) -> dict[str, Any]:
        safe_name = Path(filename or "context.pdf").name
        suffix = Path(safe_name).suffix.lower()
        if not content:
            raise InvalidContext("The selected file is empty.")
        if len(content) > MAX_PDF_BYTES:
            raise InvalidContext("Context files must be 10 MB or smaller.")

        if suffix == ".pdf":
            try:
                reader = PdfReader(BytesIO(content))
                extracted = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
                pages = len(reader.pages)
            except Exception as error:
                raise InvalidContext("Relay could not read that PDF.") from error
        elif suffix in TEXT_EXTENSIONS:
            extracted = content.decode("utf-8", errors="replace").strip()
            pages = 1
        else:
            raise InvalidContext(
                "Relay accepts PDF or text files (.pdf, .txt, .md, .csv). "
                "Word documents and images aren't supported yet."
            )

        context_id = uuid4().hex
        directory = self._root / context_id
        directory.mkdir(parents=True)
        (directory / f"source{suffix or '.pdf'}").write_bytes(content)
        (directory / "extracted.txt").write_text(extracted, encoding="utf-8")
        metadata = {
            "id": context_id,
            "filename": safe_name,
            "pages": pages,
            "characters": len(extracted),
            "address_candidate": self._find_address(extracted),
        }
        self._events.append("context.saved", metadata)
        return metadata

    # Backwards-compatible alias.
    def save_pdf(self, filename: str, content: bytes) -> dict[str, Any]:
        return self.save_document(filename, content)

    def _find_address(self, extracted: str) -> str | None:
        match = ADDRESS_PATTERN.search(extracted)
        if not match:
            return None
        return " ".join(match.group(0).strip(" ,.;").split())

    def read_text(self, context_id: str, limit: int = 20_000) -> str:
        path = self._root / context_id / "extracted.txt"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")[:limit]
