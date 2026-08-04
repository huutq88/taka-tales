import os
import pathlib
import json
import subprocess
from typing import Optional, Union, Dict, Any

from subtitle_engine.domain import (
    RenderScene, StylePreset, Canvas, Caption
)
from subtitle_engine.alignment import WhisperAlignmentProvider, WhisperXAlignmentProvider
from subtitle_engine.transcript_resolver import TranscriptResolver
from subtitle_engine.caption_segmenter import CaptionSegmenter
from subtitle_engine.layout_engine import LayoutEngine
from subtitle_engine.ass_renderer import ASSRenderer
from subtitle_engine.emoji_engine import EmojiEngine
from subtitle_engine.speaker_manager import SpeakerManager
from subtitle_engine.quality_analyzer import QualityAnalyzer
from subtitle_engine.cache import SubtitleCache
from subtitle_engine.svg_renderer import SVGRenderer


class SubtitleProcessor:
    def __init__(self, preset_path_or_id: Optional[Union[str, pathlib.Path]] = None, enable_emoji: bool = False, use_whisperx: bool = False):
        self.preset = self._load_preset(preset_path_or_id)
        if use_whisperx:
            self.alignment_provider = WhisperXAlignmentProvider()
        else:
            self.alignment_provider = WhisperAlignmentProvider()
        self.transcript_resolver = TranscriptResolver()
        self.caption_segmenter = CaptionSegmenter(rules=self.preset.segmentation)
        self.ass_renderer = ASSRenderer()
        self.emoji_engine = EmojiEngine() if enable_emoji else None
        self.speaker_manager = SpeakerManager()
        self.quality_analyzer = QualityAnalyzer()
        self.cache = SubtitleCache()
        self.svg_renderer = SVGRenderer()

    def _load_preset(self, preset_ref: Optional[Union[str, pathlib.Path]]) -> StylePreset:
        if not preset_ref:
            return StylePreset()

        # Check if it's a file path
        p_path = pathlib.Path(preset_ref)
        if p_path.exists() and p_path.is_file():
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return StylePreset(**data)
            except Exception as e:
                print(f"[SubtitleProcessor] Failed to load preset file '{preset_ref}': {e}")
                return StylePreset()

        # Check in presets directory
        preset_dir = pathlib.Path(__file__).parent.parent / "presets"
        preset_file = preset_dir / f"{preset_ref}.json"
        if preset_file.exists():
            try:
                with open(preset_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return StylePreset(**data)
            except Exception as e:
                print(f"[SubtitleProcessor] Failed to load preset '{preset_ref}': {e}")

        return StylePreset()

    def build_render_scene(
        self,
        audio_or_video_path: pathlib.Path,
        transcript: Optional[str] = None,
        canvas_width: int = 1080,
        canvas_height: int = 1920,
        fps: int = 30,
        language: str = "vi",
        speaker_id: Optional[str] = None
    ) -> RenderScene:
        """Executes alignment, transcript resolving, emoji enhancement, segmentation, and IR scene assembly."""
        audio_or_video_path = pathlib.Path(audio_or_video_path).resolve()
        
        # 1. Alignment
        aligned_words = self.alignment_provider.align(
            audio_path=audio_or_video_path,
            transcript=transcript,
            language=language
        )

        # 2. Resolve transcript
        resolved_words = self.transcript_resolver.resolve(
            original_transcript=transcript,
            aligned_words=aligned_words
        )

        # 3. Caption segmentation
        captions = self.caption_segmenter.segment(resolved_words)

        # 4. Emoji Enhancement if enabled
        if self.emoji_engine:
            captions = self.emoji_engine.enhance_captions(captions)

        # 5. Multi-Speaker Styling if speaker_id defined
        if speaker_id and speaker_id != "speaker_0":
            captions = [self.speaker_manager.apply_speaker_style(c, speaker_id) for c in captions]

        # 6. Canvas & Scene IR
        canvas = Canvas(width=canvas_width, height=canvas_height, fps=fps)
        duration = aligned_words[-1].end if aligned_words else 0.0

        scene = RenderScene(
            canvas=canvas,
            duration=duration,
            captions=captions,
            preset=self.preset
        )

        # Quality check report
        report = self.quality_analyzer.analyze(scene)
        if report.get("score", 100) < 85:
            print(f"[SubtitleProcessor Warning] Quality Score: {report.get('score')} | Warnings: {report.get('warnings')}")

        return scene

    def build_render_scene_from_fragments(
        self,
        project_dir: pathlib.Path,
        transcript: Optional[str] = None,
        canvas_width: int = 1080,
        canvas_height: int = 1920,
        fps: int = 30,
        language: str = "vi"
    ) -> RenderScene:
        """Builds RenderScene by accurately calculating per-fragment timestamps (accounting for 0.5s silence padding per fragment)."""
        import glob
        import re
        from pydub import AudioSegment
        from subtitle_engine.domain import TimedWord, Canvas, RenderScene

        # Detect project language if available
        lang = language
        try:
            cfg_file = project_dir / "project_config.json"
            if not cfg_file.exists():
                cfg_file = project_dir / "config.json"
            if cfg_file.exists():
                with open(cfg_file, "r", encoding="utf-8") as f:
                    c_data = json.load(f)
                    lang = c_data.get("language") or c_data.get("voice_config", {}).get("language") or lang
        except Exception:
            pass

        def get_frag_num(p_str):
            m = re.search(r"story_fragment(\d+)\.txt$", str(p_str))
            return int(m.group(1)) if m else 0

        frag_files = sorted(
            glob.glob(str(project_dir / "text/story_fragments/story_fragment*.txt")),
            key=get_frag_num
        )

        all_words: List[TimedWord] = []
        clip_offset = 0.0
        word_idx = 0

        for ff in frag_files:
            i = get_frag_num(ff)
            txt = pathlib.Path(ff).read_text(encoding="utf-8").strip()
            proc_wav = project_dir / f"audio/processed_voiceover{i}.wav"
            raw_wav = project_dir / f"audio/voiceover{i}.wav"
            raw_mp3 = project_dir / f"audio/voiceover{i}.mp3"
            
            target_a = proc_wav if proc_wav.exists() else (raw_wav if raw_wav.exists() else raw_mp3)

            if not target_a.exists():
                continue

            try:
                audio_seg = AudioSegment.from_file(str(target_a))
                total_dur = len(audio_seg) / 1000.0
            except Exception:
                total_dur = 3.0

            if target_a == proc_wav:
                pad_start = 0.12
                pad_end = 0.12
                speech_dur = max(0.1, total_dur - pad_start - pad_end)
            else:
                pad_start = 0.0
                pad_end = 0.0
                speech_dur = total_dur

            clean_words = [w for w in re.split(r"\s+", txt) if w]
            if not clean_words:
                clip_offset += total_dur
                continue

            # Run Whisper / WhisperX Alignment Provider for exact word-level AI timestamps (Strictly required)
            raw_frag_words = self.alignment_provider.align(target_a, transcript=txt, language=lang)
            if not raw_frag_words:
                raw_frag_words = []

            frag_words = self.transcript_resolver.resolve(txt, raw_frag_words)
            if not frag_words:
                frag_words = raw_frag_words

            max_word_end = max(w.end for w in frag_words) if frag_words else 0.0
            
            # Check if alignment is incomplete / compressed (< 60% of total audio duration)
            if max_word_end < (speech_dur * 0.6) or len(frag_words) < (len(clean_words) * 0.5):
                # Fallback to duration-based even spacing across speech_dur
                step = speech_dur / max(1, len(clean_words))
                scaled_words = []
                for idx_w, cw in enumerate(clean_words):
                    w_st = pad_start + (idx_w * step)
                    w_et = pad_start + ((idx_w + 1) * step)
                    scaled_words.append(TimedWord(
                        id=f"w_fb_{idx_w:04d}",
                        text=cw,
                        start=round(w_st, 3),
                        end=round(w_et, 3),
                        confidence=0.9
                    ))
            else:
                # Proper proportional scaling to fit speech_dur exactly
                scale = (speech_dur / max_word_end) if max_word_end > 0 else 1.0
                scaled_words = []
                for w in frag_words:
                    w_s = pad_start + (w.start * scale)
                    w_e = pad_start + (w.end * scale)
                    w_s = max(0.0, min(w_s, total_dur - 0.05))
                    w_e = max(w_s + 0.05, min(w_e, total_dur))
                    scaled_words.append(TimedWord(
                        id=w.id,
                        text=w.text,
                        start=round(w_s, 3),
                        end=round(w_e, 3),
                        confidence=w.confidence
                    ))

            for w in scaled_words:
                all_words.append(TimedWord(
                    id=f"w_{word_idx:04d}",
                    text=w.text,
                    start=round(clip_offset + w.start, 3),
                    end=round(clip_offset + w.end, 3),
                    confidence=w.confidence
                ))
                word_idx += 1

            clip_offset += total_dur

        captions = self.caption_segmenter.segment(all_words)
        if self.emoji_engine:
            captions = self.emoji_engine.enhance_captions(captions)

        canvas = Canvas(width=canvas_width, height=canvas_height, fps=fps)
        return RenderScene(
            canvas=canvas,
            duration=clip_offset,
            captions=captions,
            preset=self.preset
        )

    def process_and_render_ass(
        self,
        audio_or_video_path: pathlib.Path,
        transcript: Optional[str] = None,
        output_ass_path: Optional[pathlib.Path] = None,
        canvas_width: int = 1080,
        canvas_height: int = 1920
    ) -> pathlib.Path:
        """Generates subtitle ASS file for given video or audio."""
        audio_or_video_path = pathlib.Path(audio_or_video_path)
        if not output_ass_path:
            output_ass_path = audio_or_video_path.parent / f"{audio_or_video_path.stem}.ass"

        scene = self.build_render_scene(
            audio_or_video_path=audio_or_video_path,
            transcript=transcript,
            canvas_width=canvas_width,
            canvas_height=canvas_height
        )

        return self.ass_renderer.render_to_file(scene, output_ass_path)

    def burn_subtitles_to_video(
        self,
        input_video_path: pathlib.Path,
        output_video_path: pathlib.Path,
        transcript: Optional[str] = None,
        preset_name: Optional[str] = None
    ) -> pathlib.Path:
        """Burns subtitles directly onto the output video using FFmpeg (or MoviePy fallback)."""
        input_video_path = pathlib.Path(input_video_path).resolve()
        output_video_path = pathlib.Path(output_video_path).resolve()
        output_video_path.parent.mkdir(parents=True, exist_ok=True)

        ass_path = input_video_path.parent / f"{input_video_path.stem}_subs.ass"
        
        project_dir = input_video_path.parent
        canvas_w, canvas_h = 1080, 1920
        aspect_file = project_dir / "aspect_ratio.txt"
        config_file = project_dir / "project_config.json"
        is_horiz = "longform" in str(project_dir).lower()
        if aspect_file.exists():
            asp_txt = aspect_file.read_text(encoding="utf-8").strip()
            if asp_txt in ("16:9", "horizontal", "landscape"):
                is_horiz = True
            elif asp_txt in ("9:16", "vertical", "portrait"):
                is_horiz = False
        elif config_file.exists():
            try:
                import json
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                asp_val = cfg.get("aspect_ratio") or cfg.get("aspect")
                if asp_val in ("16:9", "horizontal", "landscape"):
                    is_horiz = True
                elif asp_val in ("9:16", "vertical", "portrait"):
                    is_horiz = False
            except Exception:
                pass
        
        if is_horiz:
            canvas_w, canvas_h = 1920, 1080

        if (project_dir / "text/story_fragments").exists():
            scene = self.build_render_scene_from_fragments(project_dir=project_dir, transcript=transcript, canvas_width=canvas_w, canvas_height=canvas_h)
        else:
            scene = self.build_render_scene(
                audio_or_video_path=input_video_path,
                transcript=transcript,
                canvas_width=canvas_w,
                canvas_height=canvas_h
            )
        self.ass_renderer.render_to_file(scene, ass_path)

        # 1. Try FFmpeg libass filter
        ass_path_str = str(ass_path).replace("\\", "/").replace("'", "'\\''")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video_path),
            "-vf", f"subtitles='{ass_path_str}'",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "copy",
            str(output_video_path)
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and output_video_path.exists() and output_video_path.stat().st_size > 0:
            print(f"[SubtitleProcessor] FFmpeg burn-in succeeded: {output_video_path}")
            return output_video_path

        # 2. Fallback: Pure Python PIL + MoviePy overlay renderer
        print("[SubtitleProcessor] FFmpeg libass unavailable. Using MoviePy + PIL Subtitle Overlay Fallback...")
        self._burn_with_moviepy(input_video_path, output_video_path, scene)
        return output_video_path

    def _burn_with_moviepy(self, input_video_path: pathlib.Path, output_video_path: pathlib.Path, scene: RenderScene):
        """Pure Python PIL + MoviePy subtitle frame renderer with Word-by-Word Active Karaoke Highlight."""
        import numpy as np
        from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip
        from PIL import Image as PILImage, ImageDraw, ImageFont as PILFont
        from subtitle_engine.font_manager import FontManager

        video = VideoFileClip(str(input_video_path))
        w, h = video.w, video.h
        is_horiz = (w > h)
        preset = scene.preset
        font_path = FontManager.resolve_font_path(preset.font.family) or "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if is_horiz:
            font_size = int(preset.font.size * (h / 1080.0))
            safe_bottom_px = int(h * 0.12)
            stroke_w = max(3, int(preset.outline.width * (h / 1080.0)))
        else:
            font_size = int(preset.font.size * (h / 1920.0))
            safe_bottom_px = int(preset.layout.safe_bottom * (h / 1920.0))
            stroke_w = max(3, int(preset.outline.width * (h / 1920.0)))

        try:
            pil_font = PILFont.truetype(font_path, font_size)
        except Exception:
            pil_font = PILFont.load_default()

        # Active font for Word Zoom Pop effect (115% size)
        font_size_active = int(font_size * 1.15)
        try:
            pil_font_active = PILFont.truetype(font_path, font_size_active)
        except Exception:
            pil_font_active = pil_font

        margin_x_min = int(w * 0.10)
        captions = scene.captions

        def apply_transform(txt: str) -> str:
            if getattr(preset.text, "transform", "none") == "uppercase":
                return txt.upper()
            elif getattr(preset.text, "transform", "none") == "lowercase":
                return txt.lower()
            return txt

        def make_frame(t):
            base_np = video.get_frame(t)
            img = PILImage.fromarray(base_np).convert("RGBA")

            active_cap = None
            for cap in captions:
                if cap.start <= t <= cap.end:
                    active_cap = cap
                    break
            
            if not active_cap:
                return np.array(img.convert("RGB"))

            draw = ImageDraw.Draw(img)
            cap_words = active_cap.words or []

            if not cap_words:
                full_text = apply_transform(active_cap.text)
                bbox = draw.textbbox((0, 0), full_text, font=pil_font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pos_x = max(margin_x_min, (w - tw) // 2)
                pos_y = h - safe_bottom_px - th
                draw.text((pos_x + 2, pos_y + 4), full_text, font=pil_font, fill=preset.shadow.color, stroke_width=stroke_w, stroke_fill="#000000")
                draw.text((pos_x, pos_y), full_text, font=pil_font, fill=preset.text.color, stroke_width=stroke_w, stroke_fill=preset.outline.color)
                return np.array(img.convert("RGB"))

            active_word_idx = -1
            for idx_w, w_obj in enumerate(cap_words):
                if w_obj.start <= t <= (w_obj.end + 0.1):
                    active_word_idx = idx_w
                    break
            if active_word_idx == -1 and t >= cap_words[0].start:
                for idx_w in range(len(cap_words) - 1, -1, -1):
                    if t >= cap_words[idx_w].start:
                        active_word_idx = idx_w
                        break

            lines_words = [cap_words[:len(cap_words)//2], cap_words[len(cap_words)//2:]] if len(active_cap.lines) > 1 and len(cap_words) >= 4 else [cap_words]
            line_y = h - safe_bottom_px - (len(lines_words) * (font_size + 14))

            word_counter = 0
            for line_idx, l_words in enumerate(lines_words):
                full_line_text = " ".join([apply_transform(word.text) for word in l_words])
                bbox = draw.textbbox((0, 0), full_line_text, font=pil_font)
                tw = bbox[2] - bbox[0]
                start_x = max(margin_x_min, (w - tw) // 2)

                curr_x = start_x
                space_w = draw.textbbox((0, 0), " ", font=pil_font)[2]

                for word_obj in l_words:
                    is_active = (word_counter == active_word_idx)
                    word_str = apply_transform(word_obj.text)
                    base_bbox = draw.textbbox((0, 0), word_str, font=pil_font)
                    word_w = base_bbox[2] - base_bbox[0]

                    word_color = preset.text.active_color if is_active else preset.text.color
                    sw = stroke_w + 1 if is_active else stroke_w

                    draw.text((curr_x + 2, line_y + 4), word_str, font=pil_font, fill=preset.shadow.color, stroke_width=sw, stroke_fill="#000000")
                    draw.text((curr_x, line_y), word_str, font=pil_font, fill=word_color, stroke_width=sw, stroke_fill=preset.outline.color)

                    curr_x += word_w + space_w
                    word_counter += 1

                line_y += font_size + 14

            return np.array(img.convert("RGB"))

        from moviepy.editor import VideoClip
        final_video = VideoClip(make_frame, duration=video.duration)
        if video.audio:
            final_video = final_video.set_audio(video.audio)
        final_video.write_videofile(
            str(output_video_path),
            fps=video.fps or 30,
            codec="libx264",
            audio_codec="aac",
            logger=None
        )
        video.close()
        final_video.close()

