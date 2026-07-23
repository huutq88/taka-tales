import os
import pathlib
import json
import re
from typing import List, Optional
from subtitle_engine.domain import TimedWord


class AlignmentProvider:
    def align(self, audio_path: pathlib.Path, transcript: Optional[str] = None, language: str = "vi") -> List[TimedWord]:
        raise NotImplementedError


class WhisperAlignmentProvider(AlignmentProvider):
    def align(self, audio_path: pathlib.Path, transcript: Optional[str] = None, language: str = "vi") -> List[TimedWord]:
        """Aligns audio with text using Whisper API or local audio duration fallback."""
        audio_path = pathlib.Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        words: List[TimedWord] = []
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_TOKEN")
        
        if api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)
                with open(audio_path, "rb") as af:
                    transcription = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=af,
                        response_format="verbose_json",
                        timestamp_granularities=["word"],
                        language=language
                    )
                
                raw_words = getattr(transcription, "words", [])
                if not raw_words and isinstance(transcription, dict):
                    raw_words = transcription.get("words", [])
                    
                for idx, w in enumerate(raw_words):
                    w_dict = w if isinstance(w, dict) else w.__dict__
                    w_text = w_dict.get("word", "").strip()
                    if w_text:
                        words.append(TimedWord(
                            id=f"w_{idx:04d}",
                            text=w_text,
                            start=float(w_dict.get("start", 0.0)),
                            end=float(w_dict.get("end", 0.0)),
                            confidence=0.95
                        ))
                if words:
                    return words
            except Exception as err:
                print(f"[AlignmentProvider] OpenAI Whisper API word alignment skipped: {err}")

        # Fallback 1: Use pydub / moviepy to get total audio duration and linear interpolate transcript words
        duration = 1.0
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_file(str(audio_path))
            duration = len(seg) / 1000.0
        except Exception:
            try:
                from moviepy.editor import AudioFileClip
                clip = AudioFileClip(str(audio_path))
                duration = clip.duration
                clip.close()
            except Exception:
                duration = 10.0

        target_text = transcript.strip() if transcript else ""
        if not target_text:
            target_text = "Nội dung video tự động"

        clean_words = [w for w in re.split(r'\s+', target_text) if w]
        num_w = len(clean_words)
        if num_w == 0:
            return []

        word_dur = duration / num_w
        for i, w in enumerate(clean_words):
            words.append(TimedWord(
                id=f"w_{i:04d}",
                text=w,
                start=round(i * word_dur, 3),
                end=round((i + 1) * word_dur, 3),
                confidence=0.8
            ))

        return words
