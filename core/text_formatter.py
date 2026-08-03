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

# Emoji: precise unicode ranges (preserving CJK, Hiragana, Katakana, and Hangul)
_RE_EMOJI = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E6-\U0001F1FF"  # flags (regional indicator symbols)
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FAFF"  # symbols extended-A
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


# ──────────────────────────────────────────────────────────────
# Multi-language unit & symbol normalization for TTS
# ──────────────────────────────────────────────────────────────

_UNIT_EXPANSIONS = {
    "vi": {
        "percent": "phần trăm",
        "dollar": "đô-la",
        "euro": "ơ-rô",
        "pound": "bảng",
        "yen": "yên",
        "dong": "đồng",
        "kmh": "ki-lô-mét trên giờ",
        "ms": "mét trên giây",
        "mph": "dặm trên giờ",
        "cm2": "xăng-ti-mét vuông",
        "m2": "mét vuông",
        "km2": "ki-lô-mét vuông",
        "deg_c": "độ C",
        "deg_f": "độ F",
        "deg": "độ",
        "hz": "Héc",
        "khz": "Ki-lô-héc",
        "mhz": "Mê-ga-héc",
        "ghz": "Gi-ga-héc",
        "db": "Đề-xi-ben",
        "kg": "ki-lô-gam",
        "g": "gam",
        "mg": "mi-li-gam",
        "l": "lít",
        "ml": "mi-li-lít",
        "km": "ki-lô-mét",
        "cm": "xăng-ti-mét",
        "mm": "mi-li-mét",
        "nm": "na-nô-mét",
        "gb": "gi-ga-byte",
        "mb": "mê-ga-byte",
        "kb": "ki-lô-byte",
        "tb": "te-ra-byte",
        "approx": "khoảng ",
        "richter": "Rích-ter",
        "co2": "Cê-Ô-hai",
        "h2o": "H-hai-Ô",
        "dna": "Đê-En-A",
        "rna": "Ar-En-A",
    },
    "en": {
        "percent": "percent",
        "dollar": "dollars",
        "euro": "euros",
        "pound": "pounds",
        "yen": "yen",
        "dong": "dong",
        "kmh": "kilometers per hour",
        "ms": "meters per second",
        "mph": "miles per hour",
        "cm2": "square centimeters",
        "m2": "square meters",
        "km2": "square kilometers",
        "deg_c": "degrees Celsius",
        "deg_f": "degrees Fahrenheit",
        "deg": "degrees",
        "hz": "Hertz",
        "khz": "kilohertz",
        "mhz": "megahertz",
        "ghz": "gigahertz",
        "db": "decibels",
        "kg": "kilograms",
        "g": "grams",
        "mg": "milligrams",
        "l": "liters",
        "ml": "milliliters",
        "km": "kilometers",
        "cm": "centimeters",
        "mm": "millimeters",
        "nm": "nanometers",
        "gb": "gigabytes",
        "mb": "megabytes",
        "kb": "kilobytes",
        "tb": "terabytes",
        "approx": "approximately ",
        "richter": "Richter",
        "co2": "C-O-two",
        "h2o": "H-two-O",
        "dna": "D-N-A",
        "rna": "R-N-A",
    },
    "fr": {
        "percent": "pour cent",
        "dollar": "dollars",
        "euro": "euros",
        "pound": "livres",
        "yen": "yen",
        "dong": "dong",
        "kmh": "kilomètres par heure",
        "ms": "mètres par seconde",
        "mph": "miles par heure",
        "cm2": "centimètres carrés",
        "m2": "mètres carrés",
        "km2": "kilomètres carrés",
        "deg_c": "degrés Celsius",
        "deg_f": "degrés Fahrenheit",
        "deg": "degrés",
        "kg": "kilogrammes",
        "g": "grammes",
        "mg": "milligrammes",
        "l": "litres",
        "ml": "millilitres",
        "km": "kilomètres",
        "cm": "centimètres",
        "mm": "millimètres",
        "gb": "gigaoctets",
        "mb": "mégaoctets",
        "approx": "environ ",
    },
    "es": {
        "percent": "por ciento",
        "dollar": "dólares",
        "euro": "euros",
        "pound": "libras",
        "yen": "yenes",
        "dong": "dong",
        "kmh": "kilómetros por hora",
        "ms": "metros por segundo",
        "mph": "millas por hora",
        "cm2": "centímetros cuadrados",
        "m2": "metros cuadrados",
        "km2": "kilómetros cuadrados",
        "deg_c": "grados Celsius",
        "deg_f": "grados Fahrenheit",
        "deg": "grados",
        "kg": "kilogramos",
        "g": "gramos",
        "mg": "miligramos",
        "l": "litros",
        "ml": "mililitros",
        "km": "kilómetros",
        "cm": "centímetros",
        "mm": "milímetros",
        "gb": "gigabytes",
        "mb": "megabytes",
        "approx": "aproximadamente ",
    },
    "de": {
        "percent": "Prozent",
        "dollar": "Dollar",
        "euro": "Euro",
        "pound": "Pfund",
        "yen": "Yen",
        "dong": "Dong",
        "kmh": "Kilometer pro Stunde",
        "ms": "Meter pro Sekunde",
        "mph": "Meilen pro Stunde",
        "cm2": "Quadratzentimeter",
        "m2": "Quadratmeter",
        "km2": "Quadratkilometer",
        "deg_c": "Grad Celsius",
        "deg_f": "Grad Fahrenheit",
        "deg": "Grad",
        "kg": "Kilogramm",
        "g": "Gramm",
        "mg": "Milligramm",
        "l": "Liter",
        "ml": "Milliliter",
        "km": "Kilometer",
        "cm": "Zentimeter",
        "mm": "Millimeter",
        "gb": "Gigabyte",
        "mb": "Megabyte",
        "approx": "etwa ",
    },
    "ja": {
        "percent": "パーセント",
        "dollar": "ドル",
        "euro": "ユーロ",
        "pound": "ポンド",
        "yen": "円",
        "kmh": "時速キロ",
        "deg_c": "度",
        "deg": "度",
        "kg": "キログラム",
        "g": "グラム",
        "km": "キロメートル",
        "cm": "センチメートル",
        "mm": "ミリメートル",
        "approx": "約",
    },
    "ko": {
        "percent": "퍼센트",
        "dollar": "달러",
        "euro": "유로",
        "pound": "파운드",
        "yen": "엔",
        "kmh": "시속 킬로미터",
        "deg_c": "도",
        "deg": "도",
        "kg": "킬로그램",
        "g": "그램",
        "km": "킬로미터",
        "cm": "센티미터",
        "mm": "밀리미터",
        "approx": "약 ",
    },
    "zh": {
        "percent": " percent",
        "dollar": "美元",
        "euro": "欧元",
        "pound": "英镑",
        "yen": "元",
        "kmh": "公里每小时",
        "deg_c": "摄氏度",
        "deg": "度",
        "kg": "公斤",
        "g": "克",
        "km": "公里",
        "cm": "厘米",
        "mm": "毫米",
        "approx": "约 ",
    }
}


def normalize_units_for_tts(text: str, language: str = "vi") -> str:
    """Normalize unit symbols (%, $, km/h, °C, kg, etc.) into full words according to language."""
    lang = language.lower() if language else "vi"
    if "-" in lang:
        lang = lang.split("-")[0]

    dict_units = _UNIT_EXPANSIONS.get(lang) or _UNIT_EXPANSIONS.get("en")
    num_pat = r"(\d+(?:[.,]\d+)?)"
    boundary = r"(?![a-zA-Z0-9/²2])"

    # 1. Speed & Velocity (MUST run before km, m)
    kmh_word = dict_units.get("kmh", "kilometers per hour")
    ms_word = dict_units.get("ms", "meters per second")
    mph_word = dict_units.get("mph", "miles per hour")

    text = re.sub(num_pat + r'\s*km/h' + boundary, r'\g<1> ' + kmh_word, text, flags=re.IGNORECASE)
    text = re.sub(r'\bkm/h' + boundary, kmh_word, text, flags=re.IGNORECASE)

    text = re.sub(num_pat + r'\s*m/s' + boundary, r'\g<1> ' + ms_word, text, flags=re.IGNORECASE)
    text = re.sub(r'\bm/s' + boundary, ms_word, text, flags=re.IGNORECASE)

    text = re.sub(num_pat + r'\s*mph' + boundary, r'\g<1> ' + mph_word, text, flags=re.IGNORECASE)
    text = re.sub(r'\bmph' + boundary, mph_word, text, flags=re.IGNORECASE)

    # 2. Area (MUST run before cm, m, km)
    text = re.sub(num_pat + r'\s*(?:cm²|cm2)' + boundary, r'\g<1> ' + dict_units.get("cm2", "square centimeters"), text, flags=re.IGNORECASE)
    text = re.sub(num_pat + r'\s*(?:m²|m2)' + boundary, r'\g<1> ' + dict_units.get("m2", "square meters"), text, flags=re.IGNORECASE)
    text = re.sub(num_pat + r'\s*(?:km²|km2)' + boundary, r'\g<1> ' + dict_units.get("km2", "square kilometers"), text, flags=re.IGNORECASE)

    # 3. Temperature & Angles (MUST run before C/F)
    text = re.sub(num_pat + r'\s*(?:°C|ºC|oC)' + boundary, r'\g<1> ' + dict_units.get("deg_c", "degrees Celsius"), text)
    text = re.sub(num_pat + r'\s*(?:°F|ºF|oF)' + boundary, r'\g<1> ' + dict_units.get("deg_f", "degrees Fahrenheit"), text)
    text = re.sub(num_pat + r'\s*°', r'\g<1> ' + dict_units.get("deg", "degrees"), text)

    # 4. Frequencies & Power & Sound (kHz/MHz/GHz before Hz)
    if "khz" in dict_units:
        text = re.sub(num_pat + r'\s*kHz' + boundary, r'\g<1> ' + dict_units["khz"], text, flags=re.IGNORECASE)
    if "mhz" in dict_units:
        text = re.sub(num_pat + r'\s*MHz' + boundary, r'\g<1> ' + dict_units["mhz"], text, flags=re.IGNORECASE)
    if "ghz" in dict_units:
        text = re.sub(num_pat + r'\s*GHz' + boundary, r'\g<1> ' + dict_units["ghz"], text, flags=re.IGNORECASE)
    if "hz" in dict_units:
        text = re.sub(num_pat + r'\s*Hz' + boundary, r'\g<1> ' + dict_units["hz"], text, flags=re.IGNORECASE)
    if "db" in dict_units:
        text = re.sub(num_pat + r'\s*dB' + boundary, r'\g<1> ' + dict_units["db"], text, flags=re.IGNORECASE)

    # 5. Mass & Weight (kg/mg before g)
    if "kg" in dict_units:
        text = re.sub(num_pat + r'\s*kg' + boundary, r'\g<1> ' + dict_units["kg"], text, flags=re.IGNORECASE)
    if "mg" in dict_units:
        text = re.sub(num_pat + r'\s*mg' + boundary, r'\g<1> ' + dict_units["mg"], text, flags=re.IGNORECASE)
    if "g" in dict_units:
        text = re.sub(num_pat + r'\s*g' + boundary, r'\g<1> ' + dict_units["g"], text)

    # 6. Volume (ml before l)
    if "ml" in dict_units:
        text = re.sub(num_pat + r'\s*ml' + boundary, r'\g<1> ' + dict_units["ml"], text, flags=re.IGNORECASE)
    if "l" in dict_units:
        text = re.sub(num_pat + r'\s*(?:l|L)' + boundary, r'\g<1> ' + dict_units["l"], text)

    # 7. Distance (km/cm/mm/nm)
    if "km" in dict_units:
        text = re.sub(num_pat + r'\s*km' + boundary, r'\g<1> ' + dict_units["km"], text, flags=re.IGNORECASE)
    if "cm" in dict_units:
        text = re.sub(num_pat + r'\s*cm' + boundary, r'\g<1> ' + dict_units["cm"], text, flags=re.IGNORECASE)
    if "mm" in dict_units:
        text = re.sub(num_pat + r'\s*mm' + boundary, r'\g<1> ' + dict_units["mm"], text, flags=re.IGNORECASE)
    if "nm" in dict_units:
        text = re.sub(num_pat + r'\s*nm' + boundary, r'\g<1> ' + dict_units["nm"], text, flags=re.IGNORECASE)

    # 8. Storage (GB/MB/KB/TB)
    if "gb" in dict_units:
        text = re.sub(num_pat + r'\s*GB' + boundary, r'\g<1> ' + dict_units["gb"], text, flags=re.IGNORECASE)
    if "mb" in dict_units:
        text = re.sub(num_pat + r'\s*MB' + boundary, r'\g<1> ' + dict_units["mb"], text, flags=re.IGNORECASE)
    if "kb" in dict_units:
        text = re.sub(num_pat + r'\s*KB' + boundary, r'\g<1> ' + dict_units["kb"], text, flags=re.IGNORECASE)
    if "tb" in dict_units:
        text = re.sub(num_pat + r'\s*TB' + boundary, r'\g<1> ' + dict_units["tb"], text, flags=re.IGNORECASE)

    # 9. Percent (%: 50% or 50.5% or 50 %)
    percent_word = dict_units.get("percent", "percent")
    text = re.sub(num_pat + r'\s*%', r'\g<1> ' + percent_word, text)

    # 10. Currency ($50, 50$, 50€, 50£, 50¥, 500.000₫, 500000đ)
    dollar_word = dict_units.get("dollar", "dollars")
    euro_word = dict_units.get("euro", "euros")
    pound_word = dict_units.get("pound", "pounds")
    yen_word = dict_units.get("yen", "yen")
    dong_word = dict_units.get("dong", "đồng")

    text = re.sub(r'\$' + num_pat + boundary, r'\g<1> ' + dollar_word, text)
    text = re.sub(num_pat + r'\s*\$', r'\g<1> ' + dollar_word, text)
    text = re.sub(num_pat + r'\s*€', r'\g<1> ' + euro_word, text)
    text = re.sub(num_pat + r'\s*£', r'\g<1> ' + pound_word, text)
    text = re.sub(num_pat + r'\s*¥', r'\g<1> ' + yen_word, text)
    text = re.sub(num_pat + r'\s*(?:₫|VNĐ|VND|đ(?!\w)|Đ(?!\w))', r'\g<1> ' + dong_word, text)


    # 11. Science / Chem / Math
    if "richter" in dict_units:
        text = re.sub(r'\bRichter\b', dict_units["richter"], text, flags=re.IGNORECASE)
    if "co2" in dict_units:
        text = re.sub(r'\bCO2\b', dict_units["co2"], text, flags=re.IGNORECASE)
        text = re.sub(r'\bCO₂\b', dict_units["co2"], text, flags=re.IGNORECASE)
    if "h2o" in dict_units:
        text = re.sub(r'\bH2O\b', dict_units["h2o"], text, flags=re.IGNORECASE)
        text = re.sub(r'\bH₂O\b', dict_units["h2o"], text, flags=re.IGNORECASE)
    if "dna" in dict_units:
        text = re.sub(r'\bDNA\b', dict_units["dna"], text)
    if "rna" in dict_units:
        text = re.sub(r'\bRNA\b', dict_units["rna"], text)

    # Math symbols (~, +)
    if "approx" in dict_units:
        text = re.sub(r'~(?=\d)', dict_units["approx"], text)

    return text



def _normalize_whitespace(text: str) -> str:
    """Clean up excessive whitespace while preserving paragraph breaks."""
    # Collapse 3+ newlines into 2 (paragraph break)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove trailing whitespace on each line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip()


def format_for_voice(text: str, language: str = "vi") -> str:
    """Format text for optimal TTS rendering.

    Pipeline:
      1. Strip markdown formatting
      2. Remove emoji
      3. Replace em-dashes with commas
      4. Normalize measurement units & symbols to words (multi-language)
      5. Normalize numbers to words (only for Vietnamese)
      6. Clean up whitespace
    """
    text = _strip_markdown(text)
    text = _remove_emoji(text)
    text = _replace_em_dashes(text)
    text = normalize_units_for_tts(text, language=language)
    if language == "vi":
        text = _normalize_numbers(text)
    text = _normalize_whitespace(text)
    return text

