import re
import difflib
from typing import List, Optional
from subtitle_engine.domain import TimedWord


class TranscriptResolver:
    @staticmethod
    def normalize_token(text: str) -> str:
        """Normalizes token for matching (lowercase, alphanumeric only)."""
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return clean.strip()

    def resolve(self, original_transcript: Optional[str], aligned_words: List[TimedWord]) -> List[TimedWord]:
        """
        Reconciles the original script text with ASR word timestamps using Sequence Alignment.
        Guarantees 100% exact script text & punctuation while accurately matching ASR audio timestamps.
        """
        if not original_transcript or not original_transcript.strip():
            return aligned_words

        script_tokens = [w for w in re.split(r'\s+', original_transcript.strip()) if w]
        if not script_tokens or not aligned_words:
            return aligned_words

        S = len(script_tokens)
        A = len(aligned_words)

        # 1. Exact 1-to-1 match
        if S == A:
            resolved_words: List[TimedWord] = []
            for i in range(S):
                resolved_words.append(TimedWord(
                    id=f"w_{i:04d}",
                    text=script_tokens[i],
                    spoken_text=aligned_words[i].text,
                    start=aligned_words[i].start,
                    end=aligned_words[i].end,
                    confidence=aligned_words[i].confidence
                ))
            return resolved_words

        # 2. Sequence Alignment matching (SequenceMatcher / Needleman-Wunsch)
        script_norm = [self.normalize_token(t) for t in script_tokens]
        asr_norm = [self.normalize_token(w.text) for w in aligned_words]

        matcher = difflib.SequenceMatcher(None, script_norm, asr_norm)
        matching_blocks = matcher.get_matching_blocks()

        # Map each script_token index to an estimated (start, end) time
        token_times: List[Optional[tuple]] = [None] * S

        for block in matching_blocks:
            si, ai, length = block.a, block.b, block.size
            for k in range(length):
                s_idx = si + k
                a_idx = ai + k
                if s_idx < S and a_idx < A:
                    token_times[s_idx] = (aligned_words[a_idx].start, aligned_words[a_idx].end)

        # 3. Fill missing timing for unmapped tokens in contiguous spans
        total_start = aligned_words[0].start
        total_end = max(aligned_words[-1].end, total_start + 1.0)

        i = 0
        while i < S:
            if token_times[i] is None:
                span_start = i
                while i < S and token_times[i] is None:
                    i += 1
                span_end = i - 1  # inclusive
                span_len = span_end - span_start + 1

                # Prev known end time
                if span_start > 0 and token_times[span_start - 1] is not None:
                    prev_t = token_times[span_start - 1][1]
                else:
                    prev_t = total_start

                # Next known start time
                if span_end < S - 1 and token_times[span_end + 1] is not None:
                    next_t = token_times[span_end + 1][0]
                else:
                    next_t = total_end

                if next_t <= prev_t:
                    next_t = prev_t + (span_len * 0.25)

                step = (next_t - prev_t) / float(span_len)
                for k in range(span_len):
                    curr_idx = span_start + k
                    st = prev_t + (k * step)
                    et = prev_t + ((k + 1) * step)
                    token_times[curr_idx] = (round(st, 3), round(et, 3))
            else:
                i += 1

        resolved_words: List[TimedWord] = []
        for i in range(S):
            st, et = token_times[i]
            resolved_words.append(TimedWord(
                id=f"w_{i:04d}",
                text=script_tokens[i],
                start=round(st, 3),
                end=round(et, 3),
                confidence=0.95
            ))

        # Enforce strict ascending time order and non-overlapping word boundaries
        for i in range(len(resolved_words) - 1):
            if resolved_words[i].end > resolved_words[i + 1].start:
                mid = (resolved_words[i].start + resolved_words[i + 1].end) / 2.0
                resolved_words[i].end = max(resolved_words[i].start + 0.05, round(mid, 3))
                resolved_words[i + 1].start = resolved_words[i].end

        return resolved_words
