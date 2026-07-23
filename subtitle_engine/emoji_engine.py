import re
from typing import List, Dict, Optional
from subtitle_engine.domain import Caption, TimedWord

# Comprehensive Vietnamese Keyword to Emoji Dictionary
VIETNAMESE_EMOJI_DICTIONARY: Dict[str, str] = {
    # Wealth & Money
    "tiền": "💰", "bạc": "🪙", "giàu": "💎", "nghèo": "💸", "tài sản": "🏦", "vàng": "👑",
    # Emotions & Mind
    "tâm": "🧘", "tĩnh": "🕊️", "bình yên": "🕊️", "buồn": "😢", "vui": "😊", "yêu": "❤️",
    "giận": "😡", "sợ": "😱", "lo": "😟", "ngộ": "💡", "hiểu": "🧠", "trí tuệ": "🧠",
    # Virtues & Morals
    "đạo lý": "📜", "chân lý": "✨", "thiện": "😇", "ác": "😈", "nhân quả": "☯️", "nghiệp": "☸️",
    # Actions & States
    "thành công": "🏆", "thất bại": "❌", "cảnh báo": "⚠️", "đúng": "✅", "sai": "🚫",
    "học": "📚", "sách": "📖", "đọc": "📖", "nói": "💬", "lắng nghe": "👂", "nhìn": "👁️",
    # Nature & Elements
    "gió": "🍃", "mưa": "🌧️", "nước": "💧", "lửa": "🔥", "đất": "🌍", "trời": "☁️",
    "hoa": "🌸", "cây": "🌳", "núi": "⛰️", "biển": "🌊", "nắng": "☀️", "đêm": "🌙",
    # Time & Objects
    "thời gian": "⏳", "ngày": "☀️", "năm": "📅", "nhà": "🏠", "xe": "🚗", "quà": "🎁"
}


class EmojiEngine:
    def __init__(self, dictionary: Optional[Dict[str, str]] = None, max_emojis_per_caption: int = 1):
        self.dictionary = dictionary or VIETNAMESE_EMOJI_DICTIONARY
        self.max_emojis_per_caption = max_emojis_per_caption

    def find_emoji(self, word: str) -> Optional[str]:
        """Finds matching emoji for a word (stemmed/lowercased)."""
        clean_word = re.sub(r'[^\w\s]', '', word.lower()).strip()
        return self.dictionary.get(clean_word)

    def enhance_caption(self, caption: Caption) -> Caption:
        """Enhances a caption by injecting contextual emojis at matching word positions."""
        if not caption.words:
            return caption

        enhanced_words: List[TimedWord] = []
        emojis_added = 0

        for word_obj in caption.words:
            emoji = self.find_emoji(word_obj.text)
            if emoji and emojis_added < self.max_emojis_per_caption:
                new_text = f"{word_obj.text} {emoji}"
                emojis_added += 1
                enhanced_words.append(TimedWord(
                    id=word_obj.id,
                    text=new_text,
                    spoken_text=word_obj.spoken_text or word_obj.text,
                    start=word_obj.start,
                    end=word_obj.end,
                    confidence=word_obj.confidence
                ))
            else:
                enhanced_words.append(word_obj)

        enhanced_text = " ".join([w.text for w in enhanced_words])
        return Caption(
            id=caption.id,
            start=caption.start,
            end=caption.end,
            text=enhanced_text,
            lines=[enhanced_text],
            words=enhanced_words
        )

    def enhance_captions(self, captions: List[Caption]) -> List[Caption]:
        """Enhances all captions in a scene with contextual emojis."""
        return [self.enhance_caption(c) for c in captions]
