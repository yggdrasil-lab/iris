from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import re
from typing import Any, Dict, List, Optional

MAX_STORED_BODY_CHARS = 65536
MAX_RAW_EXCERPT_CHARS = 4000

_SKIP_HTML_TAGS = {"script", "style", "noscript", "head", "title"}
_BLOCK_HTML_TAGS = {
    "p",
    "div",
    "tr",
    "li",
    "br",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
}
_INLINE_PAD_TAGS = {"td", "th"}
_INVISIBLE_CHARS_RE = re.compile(r"[\u034f\u200b\u200c\u200d\u200e\u200f\u2007\u2060\u00ad\ufeff]+")
_INVISIBLE_CODEPOINTS = {
    0x034F,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x2007,
    0x2060,
    0x00AD,
    0xFEFF,
}
_INVISIBLE_ENTITY_NAMES = {"zwnj", "zwj", "lrm", "rlm", "shy"}
_ANGLE_TRACKING_URL_RE = re.compile(r"<https?://[^>]+>", re.IGNORECASE)
_LONG_TRACKING_URL_RE = re.compile(r"https?://\S{40,}", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_HTML_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_HTML_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_MAX_TRACKING_ATTR_LEN = 80


def _strip_invisible_and_tracking(text: str) -> str:
    """Remove invisible Unicode preheaders, tracking URLs, and redundant whitespace."""
    if not text:
        return ""
    cleaned = _INVISIBLE_CHARS_RE.sub("", text)
    cleaned = _ANGLE_TRACKING_URL_RE.sub("", cleaned)
    cleaned = _LONG_TRACKING_URL_RE.sub("", cleaned)
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


class _PlainTextHTMLParser(HTMLParser):
    """Stdlib HTML-to-text extractor stripping scripts, styles, and markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth != 0:
            return
        if lowered in _BLOCK_HTML_TAGS:
            self._chunks.append("\n")
        elif lowered in _INLINE_PAD_TAGS:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _SKIP_HTML_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth != 0:
            return
        if lowered in _BLOCK_HTML_TAGS:
            self._chunks.append("\n")
        elif lowered in _INLINE_PAD_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return _strip_invisible_and_tracking("".join(self._chunks))


class _HTMLStorageCleaner(HTMLParser):
    """Strip inline styles, tracking URLs, comments, and invisible spacers from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._skip_depth = 0
        self._chunks: List[str] = []

    def _format_attrs(self, attrs: list[tuple[str, Optional[str]]]) -> str:
        parts: List[str] = []
        for name, value in attrs:
            lowered = name.lower()
            if lowered == "style":
                continue
            if lowered in ("href", "src") and value is not None and len(value.strip()) > _MAX_TRACKING_ATTR_LEN:
                continue
            if value is None:
                parts.append(name)
            else:
                parts.append(f'{name}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(parts)) if parts else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth == 0:
            self._chunks.append(f"<{tag}{self._format_attrs(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in ("script", "style"):
            return
        if self._skip_depth == 0:
            self._chunks.append(f"<{tag}{self._format_attrs(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth == 0:
            self._chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(_INVISIBLE_CHARS_RE.sub("", data))

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth != 0:
            return
        lowered = name.lower()
        if lowered in _INVISIBLE_ENTITY_NAMES:
            return
        if lowered == "nbsp":
            self._chunks.append(" ")
            return
        self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth != 0:
            return
        try:
            code = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
        except ValueError:
            self._chunks.append(f"&#{name};")
            return
        if code in _INVISIBLE_CODEPOINTS:
            return
        if code == 0x00A0:
            self._chunks.append(" ")
            return
        self._chunks.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def get_html(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def clean_html_for_storage(
    html_body: Optional[str],
    max_chars: int = MAX_STORED_BODY_CHARS,
) -> Optional[str]:
    """Strip bloated styles, scripts, head, comments, spacers, and long tracking URLs from HTML."""
    if html_body is None:
        return None
    stripped = _HTML_COMMENT_RE.sub("", html_body)
    stripped = _HTML_SCRIPT_RE.sub("", stripped)
    stripped = _HTML_STYLE_RE.sub("", stripped)
    stripped = _HTML_HEAD_RE.sub("", stripped)
    stripped = _INVISIBLE_CHARS_RE.sub("", stripped)

    parser = _HTMLStorageCleaner()
    parser.feed(stripped)
    parser.close()
    cleaned = parser.get_html().strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def sanitize_html_to_text(html_body: Optional[str]) -> str:
    """Strip HTML scripts, styles, and tags into normalized plain text."""
    if not html_body:
        return ""
    parser = _PlainTextHTMLParser()
    parser.feed(html_body)
    parser.close()
    return parser.get_text()


def build_raw_excerpt(
    text_body: Optional[str],
    html_body: Optional[str],
    max_chars: int = MAX_RAW_EXCERPT_CHARS,
) -> str:
    """Build bounded plain-text excerpt preferring cleaned text_body over sanitized HTML."""
    normalized = _strip_invisible_and_tracking(text_body or "")
    if not normalized:
        normalized = sanitize_html_to_text(html_body)
    return normalized[:max_chars]


def normalize_message_id(
    raw_message_id: Optional[str],
    from_address: str,
    subject: str,
    received_at: datetime,
    text_body: Optional[str],
) -> str:
    """Return trimmed RFC 5322 Message-ID or deterministic sha256 fallback."""
    cleaned = (raw_message_id or "").strip()
    if cleaned:
        if len(cleaned) <= 512:
            return cleaned
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    canonical = (
        f"{from_address.strip().lower()}\n"
        f"{subject.strip()}\n"
        f"{received_at.astimezone(timezone.utc).isoformat()}\n"
        f"{(text_body or '')[:4096]}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def ensure_utc(value: Optional[datetime]) -> datetime:
    """Ensure datetime is timezone-aware in UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def truncate_body(value: Optional[str], max_chars: int = MAX_STORED_BODY_CHARS) -> Optional[str]:
    """Trim whitespace and bound stored body length."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def has_document_attachment(attachments_meta: Optional[List[Dict[str, Any]]]) -> bool:
    """Return True when attachments metadata includes a PDF document or image file."""
    if not attachments_meta:
        return False
    for att in attachments_meta:
        if not isinstance(att, dict):
            continue
        raw_content_type = str(att.get("content_type") or "").strip().lower()
        mime_type = raw_content_type.split(";", 1)[0].strip()
        filename = str(att.get("filename") or "").strip().lower()
        if mime_type == "application/pdf" or mime_type.startswith("image/") or filename.endswith(".pdf"):
            return True
    return False


def has_calendar_attachment(attachments_meta: Optional[List[Dict[str, Any]]]) -> bool:
    """Return True when attachments metadata includes an .ics file or text/calendar part."""
    if not attachments_meta:
        return False
    for att in attachments_meta:
        if not isinstance(att, dict):
            continue
        raw_content_type = str(att.get("content_type") or "").strip().lower()
        mime_type = raw_content_type.split(";", 1)[0].strip()
        filename = str(att.get("filename") or "").strip().lower()
        if mime_type == "text/calendar" or filename.endswith(".ics"):
            return True
    return False
