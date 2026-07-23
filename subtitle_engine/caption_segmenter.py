import re
from typing import List
from subtitle_engine.domain import TimedWord, Caption, SegmentationRules


class CaptionSegmenter:
    def __init__(self, rules: SegmentationRules = None):
        self.rules = rules or SegmentationRules()

    def segment(self, words: List[TimedWord]) -> List[Caption]:
        if not words:
            return []

        captions: List[Caption] = []
        current_chunk: List[TimedWord] = []
        
        punctuation_regex = re.compile(r'[\.\!\?\;\:\,\-]$')

        for i, word in enumerate(words):
            current_chunk.append(word)

            # Check segmentation criteria
            is_last = (i == len(words) - 1)
            chunk_text = " ".join([w.text for w in current_chunk])
            num_words = len(current_chunk)
            
            # Pause duration check with next word
            pause_after = 0.0
            if not is_last:
                pause_after = (words[i + 1].start - word.end) * 1000.0  # ms

            has_punct = bool(punctuation_regex.search(word.text))
            exceeds_max_words = num_words >= self.rules.max_words
            exceeds_chars = len(chunk_text) >= (self.rules.max_chars_per_line * self.rules.max_lines)
            exceeds_pause = pause_after >= self.rules.pause_threshold_ms

            if is_last or exceeds_max_words or exceeds_chars or exceeds_pause or (has_punct and num_words >= self.rules.min_words):
                # Build Caption object
                c_start = current_chunk[0].start
                c_end = current_chunk[-1].end
                
                # Wrap text into lines based on max_chars_per_line
                lines = self._wrap_lines(current_chunk, self.rules.max_chars_per_line)
                
                captions.append(Caption(
                    id=f"cap_{len(captions) + 1:04d}",
                    start=round(c_start, 3),
                    end=round(c_end, 3),
                    text=chunk_text,
                    lines=lines,
                    words=list(current_chunk)
                ))
                current_chunk = []

        return captions

    def _wrap_lines(self, words: List[TimedWord], max_chars: int) -> List[str]:
        """Wraps words into 1 or 2 balanced lines without orphan words."""
        text_words = [w.text for w in words]
        full_text = " ".join(text_words)
        
        if len(full_text) <= max_chars or len(text_words) < 4:
            return [full_text]

        # Find optimal split index that minimizes length difference between line 1 and line 2
        best_split = len(text_words) // 2
        min_diff = float("inf")

        # Split index must leave at least 2 words on line 2 (avoiding single word dangling)
        for i in range(2, len(text_words) - 1):
            l1 = " ".join(text_words[:i])
            l2 = " ".join(text_words[i:])
            diff = abs(len(l1) - len(l2))
            if diff < min_diff:
                min_diff = diff
                best_split = i

        line1 = " ".join(text_words[:best_split])
        line2 = " ".join(text_words[best_split:])
        return [line1, line2]
