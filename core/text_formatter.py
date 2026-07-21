"""Text formatter for TTS — Chuẩn bị văn bản tiếng Việt cho tổng hợp giọng nói.

Thực hiện các bước xử lý:
  1. Xóa markdown formatting (headers, bold, italic, links, images)
  2. Xóa emoji và ký tự đặc biệt không phát âm được
  3. Thay dấu gạch ngang kéo dài (—) bằng dấu phẩy/hai chấm
  4. Chuẩn hóa chữ số sang chữ viết tiếng Việt
  5. Chuẩn hóa khoảng trắng thừa
"""
from __future__ import annotations

import re


# ──────────────────────────────────────────────────────────────
# Vietnamese number-to-words conversion
# ──────────────────────────────────────────────────────────────

_ONES = [
    "", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín",
]

_TENS_SPECIAL = {
    10: "mười",
    11: "mười một",
    14: "mười bốn",  # not "mười tư" in formal reading
    15: "mười lăm",
}


def _number_under_100(n: int) -> str:
    """Convert 0-99 to Vietnamese words."""
    if n == 0:
        return "không"
    if n < 10:
        return _ONES[n]
    if n in _TENS_SPECIAL:
        return _TENS_SPECIAL[n]
    if n < 20:
        tens_word = "mười"
        ones = n % 10
        if ones == 5:
            return f"{tens_word} lăm"
        if ones == 4:
            return f"{tens_word} bốn"
        return f"{tens_word} {_ONES[ones]}"
    tens = n // 10
    ones = n % 10
    tens_word = f"{_ONES[tens]} mươi"
    if ones == 0:
        return tens_word
    if ones == 1:
        return f"{tens_word} mốt"
    if ones == 4:
        return f"{tens_word} tư"
    if ones == 5:
        return f"{tens_word} lăm"
    return f"{tens_word} {_ONES[ones]}"


def _number_under_1000(n: int) -> str:
    """Convert 0-999 to Vietnamese words."""
    if n < 100:
        return _number_under_100(n)
    hundreds = n // 100
    remainder = n % 100
    result = f"{_ONES[hundreds]} trăm"
    if remainder == 0:
        return result
    if remainder < 10:
        return f"{result} lẻ {_ONES[remainder]}"
    return f"{result} {_number_under_100(remainder)}"


def number_to_vietnamese(n: int) -> str:
    """Convert an integer to Vietnamese words.

    Supports numbers from 0 to 999,999,999.
    """
    if n < 0:
        return f"âm {number_to_vietnamese(-n)}"
    if n == 0:
        return "không"
    if n < 1000:
        return _number_under_1000(n)

    parts = []

    # Billions (tỷ)
    if n >= 1_000_000_000:
        billions = n // 1_000_000_000
        parts.append(f"{_number_under_1000(billions)} tỷ")
        n %= 1_000_000_000

    # Millions (triệu)
    if n >= 1_000_000:
        millions = n // 1_000_000
        parts.append(f"{_number_under_1000(millions)} triệu")
        n %= 1_000_000

    # Thousands (nghìn)
    if n >= 1000:
        thousands = n // 1000
        parts.append(f"{_number_under_1000(thousands)} nghìn")
        n %= 1000

    # Remainder
    if n > 0:
        if n < 100 and parts:
            parts.append(f"không trăm {_number_under_100(n)}")
        else:
            parts.append(_number_under_1000(n))

    return " ".join(parts)


# ──────────────────────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────────────────────

# Markdown: headers, images, links, bold, italic, code
_RE_MD_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_RE_MD_CODE = re.compile(r"`([^`]+)`")
_RE_MD_HR = re.compile(r"^-{3,}$|^\*{3,}$", re.MULTILINE)

# Emoji: broad unicode ranges
_RE_EMOJI = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"  # dingbats
    "\U000024C2-\U0001F251"  # misc
    "\U0001F900-\U0001F9FF"  # supplemental
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "]+",
    flags=re.UNICODE,
)

# Em-dash
_RE_EM_DASH = re.compile(r"\s*—\s*")

# Standalone numbers (not inside words)
# Matches: "24", "1940", "1.134" (with dot separator)
_RE_DATE_FULL = re.compile(
    r"(?:ngày|Ngày|đêm|Đêm|sáng|Sáng|chiều|Chiều)\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
)
_RE_THANG = re.compile(r"tháng\s+(\d{1,2})")
_RE_MUNG = re.compile(r"(?:mùng|Mùng)\s+(\d{1,2})")
_RE_SO = re.compile(r"(?:số|Số)\s+(\d[\d.]*)")
_RE_STANDALONE_NUM = re.compile(r"(?<!\w)(\d[\d.]*)(?!\w)")


# ──────────────────────────────────────────────────────────────
# Formatting pipeline
# ──────────────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """Remove markdown formatting, keeping plain text content."""
    text = _RE_MD_HR.sub("", text)
    text = _RE_MD_IMAGE.sub("", text)        # remove images entirely
    text = _RE_MD_LINK.sub(r"\1", text)      # keep link text
    text = _RE_MD_BOLD.sub(r"\1", text)      # keep bold text
    text = _RE_MD_ITALIC.sub(r"\1", text)    # keep italic text
    text = _RE_MD_CODE.sub(r"\1", text)      # keep code text
    text = _RE_MD_HEADER.sub("", text)       # remove header markers
    return text


def _remove_emoji(text: str) -> str:
    """Remove emoji characters."""
    return _RE_EMOJI.sub("", text)


def _replace_em_dashes(text: str) -> str:
    """Replace em-dashes with commas for natural TTS pauses."""
    return _RE_EM_DASH.sub(", ", text)


def _parse_dotted_number(s: str) -> int | None:
    """Parse a number that may use dots as thousand separators (e.g. '1.134')."""
    clean = s.replace(".", "")
    try:
        return int(clean)
    except ValueError:
        return None


def _normalize_numbers(text: str) -> str:
    """Convert Arabic numerals to Vietnamese words in natural reading order."""

    # Full dates: "ngày 24 tháng 10 năm 1940"
    def _replace_full_date(m):
        prefix = m.group(0).split()[0]  # "ngày" / "Ngày" / "đêm" etc.
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        return f"{prefix} {number_to_vietnamese(day)} tháng {number_to_vietnamese(month)} năm {number_to_vietnamese(year)}"

    text = _RE_DATE_FULL.sub(_replace_full_date, text)

    # "tháng 10" → "tháng mười"
    def _replace_thang(m):
        month = int(m.group(1))
        return f"tháng {number_to_vietnamese(month)}"

    text = _RE_THANG.sub(_replace_thang, text)

    # "mùng 3" → "mùng ba"
    def _replace_mung(m):
        prefix = m.group(0).split()[0]  # preserve case
        day = int(m.group(1))
        return f"{prefix} {number_to_vietnamese(day)}"

    text = _RE_MUNG.sub(_replace_mung, text)

    # "số 1.134" → "số một nghìn một trăm ba mươi tư"
    def _replace_so(m):
        prefix = m.group(0).split()[0]
        n = _parse_dotted_number(m.group(1))
        if n is not None:
            return f"{prefix} {number_to_vietnamese(n)}"
        return m.group(0)

    text = _RE_SO.sub(_replace_so, text)

    # Standalone numbers (years, ages, etc.)
    def _replace_standalone(m):
        n = _parse_dotted_number(m.group(1))
        if n is not None and n > 0:
            return number_to_vietnamese(n)
        return m.group(0)

    text = _RE_STANDALONE_NUM.sub(_replace_standalone, text)

    return text


def _normalize_whitespace(text: str) -> str:
    """Clean up excessive whitespace while preserving paragraph breaks."""
    # Collapse 3+ newlines into 2 (paragraph break)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove trailing whitespace on each line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip()


def format_for_voice(text: str) -> str:
    """Format text for optimal TTS rendering.

    Pipeline:
      1. Strip markdown formatting
      2. Remove emoji
      3. Replace em-dashes with commas
      4. Normalize numbers to Vietnamese words
      5. Clean up whitespace
    """
    text = _strip_markdown(text)
    text = _remove_emoji(text)
    text = _replace_em_dashes(text)
    text = _normalize_numbers(text)
    text = _normalize_whitespace(text)
    return text
