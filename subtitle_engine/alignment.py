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
    _fw_model = None

    def align(self, audio_path: pathlib.Path, transcript: Optional[str] = None, language: str = "vi") -> List[TimedWord]:
        """Aligns audio with text using Whisper API or local audio duration fallback."""
        audio_path = pathlib.Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        words: List[TimedWord] = []
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_TOKEN")
        if not api_key:
            try:
                import configparser
                cfg = configparser.ConfigParser()
                cfg_path = pathlib.Path(__file__).parent.parent / "config.ini"
                if cfg_path.exists():
                    cfg.read(cfg_path)
                    api_key = cfg.get("OPENAI", "API_KEY", fallback=None) or cfg.get("IMAGE_PROMPT", "OPENAI_TOKEN", fallback=None)
            except Exception:
                pass
        
        # 1. Try local faster_whisper model
        try:
            if WhisperAlignmentProvider._fw_model is None:
                os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
                os.environ["OMP_NUM_THREADS"] = "1"
                from faster_whisper import WhisperModel
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
                compute_type = "float16" if device == "cuda" else "int8"
                print(f"[WhisperAlignmentProvider] Initializing cached faster_whisper model ({device}/{compute_type})...")
                WhisperAlignmentProvider._fw_model = WhisperModel("small", device=device, compute_type=compute_type, cpu_threads=2)

            fw_model = WhisperAlignmentProvider._fw_model
            print(f"[WhisperAlignmentProvider] Running local faster_whisper alignment on {audio_path.name}...")
            initial_prompt = transcript.strip()[:150] if transcript else None
            segments, info = fw_model.transcribe(str(audio_path), word_timestamps=True, initial_prompt=initial_prompt, language=language)
            
            w_idx = 0
            for segment in segments:
                for w in (getattr(segment, "words", None) or []):
                    w_text = w.word.strip()
                    if w_text:
                        words.append(TimedWord(
                            id=f"w_{w_idx:04d}",
                            text=w_text,
                            start=round(float(w.start), 3),
                            end=round(float(w.end), 3),
                            confidence=round(float(getattr(w, "probability", 0.95)), 2)
                        ))
                        w_idx += 1
            if words:
                print(f"[WhisperAlignmentProvider] Extracted {len(words)} word timestamps using local faster_whisper.")
                return words
        except Exception as fw_err:
            print(f"[WhisperAlignmentProvider] Local faster_whisper skipped: {fw_err}")

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
                        language=language,
                        prompt=transcript[:150] if transcript else None
                    )
                
                raw_words = getattr(transcription, "words", [])
                if not raw_words and isinstance(transcription, dict):
                    raw_words = transcription.get("words", [])
                    
                for idx, w in enumerate(raw_words):
                    w_dict = w if isinstance(w, dict) else (w.__dict__ if hasattr(w, "__dict__") else {})
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

        # 3. Fallback: estimate word timestamps from audio duration and transcript words
        if transcript and transcript.strip():
            try:
                import soundfile as sf
                sf_info = sf.info(str(audio_path))
                dur = float(sf_info.duration)
                clean_words = [w.strip() for w in transcript.strip().split() if w.strip()]
                if clean_words and dur > 0:
                    time_per_word = dur / len(clean_words)
                    for i, w in enumerate(clean_words):
                        words.append(TimedWord(
                            id=f"w_{i:04d}",
                            text=w,
                            start=round(i * time_per_word, 3),
                            end=round((i + 1) * time_per_word, 3),
                            confidence=0.85
                        ))
                    if words:
                        print(f"[WhisperAlignmentProvider] Fallback: generated {len(words)} word timestamps from audio duration ({dur:.2f}s).")
                        return words
            except Exception as f_err:
                print(f"[WhisperAlignmentProvider] Duration fallback error: {f_err}")

        raise RuntimeError(f"[WhisperAlignmentProvider] Failed to extract word timestamps using Whisper AI for '{audio_path.name}'.")


class WhisperXAlignmentProvider(AlignmentProvider):
    def align(self, audio_path: pathlib.Path, transcript: Optional[str] = None, language: str = "vi") -> List[TimedWord]:
        """Aligns audio with text using local WhisperX pipeline with Forced Alignment."""
        audio_path = pathlib.Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        try:
            import whisperx
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"

            print(f"[WhisperXAlignmentProvider] Transcribing {audio_path.name} using WhisperX on device: {device}...")
            model = whisperx.load_model("small", device=device, compute_type=compute_type, language=language)
            audio = whisperx.load_audio(str(audio_path))
            result = model.transcribe(audio, batch_size=16)

            lang_code = result.get("language", language)
            print(f"[WhisperXAlignmentProvider] Loading alignment model for language '{lang_code}'...")
            model_a, metadata = whisperx.load_align_model(language_code=lang_code, device=device)
            aligned_result = whisperx.align(
                result["segments"],
                model_a,
                metadata,
                audio,
                device=device,
                return_char_alignments=False
            )

            words: List[TimedWord] = []
            idx = 0
            
            # Map aligned segments and scale word timestamps if Phoneme model compressed timestamps
            for raw_seg, aligned_seg in zip(result.get("segments", []), aligned_result.get("segments", [])):
                orig_start = float(raw_seg.get("start", 0.0))
                orig_end = float(raw_seg.get("end", orig_start + 1.0))
                
                seg_words = aligned_seg.get("words", [])
                if not seg_words:
                    continue
                
                valid_words = [w for w in seg_words if "start" in w and "end" in w]
                if not valid_words:
                    continue
                
                w_start = float(valid_words[0]["start"])
                w_end = float(valid_words[-1]["end"])
                
                w_dur = w_end - w_start
                orig_dur = orig_end - orig_start
                
                # Calculate scale factor if alignment model compressed timestamps
                scale = (orig_dur / w_dur) if (w_dur > 0 and orig_dur > 0 and abs(orig_dur - w_dur) > 0.5) else 1.0
                
                for w in seg_words:
                    w_text = w.get("word", "").strip()
                    if not w_text:
                        continue
                    
                    raw_s = float(w.get("start", w_start))
                    raw_e = float(w.get("end", raw_s + 0.3))
                    
                    if scale != 1.0:
                        start_t = orig_start + (raw_s - w_start) * scale
                        end_t = orig_start + (raw_e - w_start) * scale
                    else:
                        start_t = raw_s
                        end_t = raw_e
                    
                    conf = float(w.get("score", 0.95))
                    words.append(TimedWord(
                        id=f"w_{idx:04d}",
                        text=w_text,
                        start=round(start_t, 3),
                        end=round(end_t, 3),
                        confidence=conf
                    ))
                    idx += 1

            if words:
                expected_count = len(re.split(r'\s+', transcript.strip())) if transcript else 0
                if expected_count > 5 and len(words) < (expected_count * 0.5):
                    print(f"[WhisperXAlignmentProvider Warning] WhisperX extracted only {len(words)}/{expected_count} words. Falling back to faster_whisper Alignment...")
                    fallback_provider = WhisperAlignmentProvider()
                    return fallback_provider.align(audio_path, transcript, language)

                print(f"[WhisperXAlignmentProvider] Extracted {len(words)} word timestamps using WhisperX (scaled & aligned).")
                return words
        except Exception as err:
            print(f"[WhisperXAlignmentProvider] WhisperX failed: {err}. Falling back to Whisper API/Standard...")

        # Fallback to WhisperAlignmentProvider if WhisperX encounters error
        fallback_provider = WhisperAlignmentProvider()
        return fallback_provider.align(audio_path, transcript, language)

