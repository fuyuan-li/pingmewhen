from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from pypdf import PdfReader

from relay_agent.event_log import EventLog, default_data_dir


MAX_PDF_BYTES = 10 * 1024 * 1024
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

    def save_pdf(self, filename: str, content: bytes) -> dict[str, Any]:
        safe_name = Path(filename or "context.pdf").name
        if not safe_name.lower().endswith(".pdf"):
            raise InvalidContext("Relay currently accepts PDF context files only.")
        if not content:
            raise InvalidContext("The selected PDF is empty.")
        if len(content) > MAX_PDF_BYTES:
            raise InvalidContext("PDF context files must be 10 MB or smaller.")

        try:
            reader = PdfReader(BytesIO(content))
            extracted = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        except Exception as error:
            raise InvalidContext("Relay could not read that PDF.") from error

        context_id = uuid4().hex
        directory = self._root / context_id
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(content)
        (directory / "extracted.txt").write_text(extracted, encoding="utf-8")
        metadata = {
            "id": context_id,
            "filename": safe_name,
            "pages": len(reader.pages),
            "characters": len(extracted),
            "address_candidate": self._find_address(extracted),
        }
        self._events.append("context.saved", metadata)
        return metadata

    def _find_address(self, extracted: str) -> str | None:
        match = ADDRESS_PATTERN.search(extracted)
        if not match:
            return None
        return " ".join(match.group(0).strip(" ,.;").split())
