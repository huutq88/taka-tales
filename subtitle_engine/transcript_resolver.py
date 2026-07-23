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

        # Interpolate missing timing for unmapped script tokens
        total_start = aligned_words[0].start
        total_end = aligned_words[-1].end

        resolved_words: List[TimedWord] = []
        for i in range(S):
            if token_times[i] is not None:
                st, et = token_times[i]
            else:
                # Find previous known time
                prev_time = None
                for p in range(i - 1, -1, -1):
                    if token_times[p] is not None:
                        prev_time = token_times[p][1]
                        break
                if prev_time is None:
                    prev_time = total_start

                # Find next known time
                next_time = None
                for n in range(i + 1, S):
                    if token_times[n] is not None:
                        next_time = token_times[n][0]
                        break
                if next_time is None:
                    next_time = total_end

                # Distribute linearly between prev_time and next_time
                unmapped_count = sum(1 for k in range(i, S) if token_times[k] is None and (k == i or token_times[k-1] is None))
                st = prev_time + (next_time - prev_time) * (1.0 / (unmapped_count + 1))
                et = st + max(0.1, (next_time - prev_time) / (unmapped_count + 1))

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
                resolved_words[i].end = max(resolved_words[i].start + 0.05, mid)
                resolved_words[i + 1].start = resolved_words[i].end

        return resolved_words
