import os
import pathlib
import json
import subprocess
from typing import Optional, Union, Dict, Any

from subtitle_engine.domain import (
    RenderScene, StylePreset, Canvas, Caption
)
from subtitle_engine.alignment import WhisperAlignmentProvider
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
    def __init__(self, preset_path_or_id: Optional[Union[str, pathlib.Path]] = None, enable_emoji: bool = False):
        self.preset = self._load_preset(preset_path_or_id)
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
        scene = self.build_render_scene(
            audio_or_video_path=input_video_path,
            transcript=transcript,
            canvas_width=1080,
            canvas_height=1920
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
        preset = scene.preset
        font_path = FontManager.resolve_font_path(preset.font.family) or "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        font_size = int(preset.font.size * (h / 1920.0))
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

        sub_clips = []
        stroke_w = max(3, int(preset.outline.width * (h / 1920.0)))
        safe_bottom_px = int(preset.layout.safe_bottom * (h / 1920.0))
        max_allowed_w = int(w * preset.layout.max_width_ratio)  # 80% screen width limit
        margin_x_min = int(w * 0.10)  # 10% margin on left/right

        def apply_transform(txt: str) -> str:
            if getattr(preset.text, "transform", "none") == "uppercase":
                return txt.upper()
            elif getattr(preset.text, "transform", "none") == "lowercase":
                return txt.lower()
            return txt

        for cap in scene.captions:
            cap_words = cap.words or []
            if not cap_words:
                # Static line rendering
                img = PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                full_text = apply_transform(cap.text)
                bbox = draw.textbbox((0, 0), full_text, font=pil_font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pos_x = max(margin_x_min, (w - tw) // 2)
                pos_y = h - safe_bottom_px - th

                draw.text(
                    (pos_x, pos_y), full_text, font=pil_font,
                    fill=preset.text.color, stroke_width=stroke_w, stroke_fill=preset.outline.color
                )
                sub_clip = ImageClip(np.array(img)).set_duration(max(0.2, cap.end - cap.start)).set_start(cap.start)
                sub_clips.append(sub_clip)
                continue

            # Render Word-by-Word active highlight clips with Word Zoom Pop 115%
            for active_idx, active_word in enumerate(cap_words):
                w_start = active_word.start
                w_end = active_word.end if active_idx < len(cap_words) - 1 else cap.end
                w_dur = max(0.08, w_end - w_start)

                img = PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)

                # Split cap_words into 2 balanced lines if line count > 1
                lines_words = []
                if len(cap.lines) > 1 and len(cap_words) >= 4:
                    mid = len(cap_words) // 2
                    lines_words = [cap_words[:mid], cap_words[mid:]]
                else:
                    lines_words = [cap_words]

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
                        is_active = (word_counter == active_idx)
                        word_str = apply_transform(word_obj.text)
                        f = pil_font_active if is_active else pil_font
                        word_color = preset.text.active_color if is_active else preset.text.color
                        word_w = draw.textbbox((0, 0), word_str, font=f)[2]

                        # Adjust vertical y position so active word scales UP in place without dropping baseline
                        y_offset = (font_size_active - font_size) // 2 if is_active else 0
                        word_y = line_y - y_offset

                        # Draw subtle drop shadow
                        draw.text(
                            (curr_x + 2, word_y + 4), word_str, font=f,
                            fill=preset.shadow.color, stroke_width=stroke_w, stroke_fill="#000000"
                        )
                        # Draw main text with outline
                        draw.text(
                            (curr_x, word_y), word_str, font=f,
                            fill=word_color, stroke_width=stroke_w, stroke_fill=preset.outline.color
                        )
                        curr_x += word_w + space_w
                        word_counter += 1

                    line_y += font_size + 14

                sub_clip = ImageClip(np.array(img)).set_duration(w_dur).set_start(w_start)
                sub_clips.append(sub_clip)

        final_video = CompositeVideoClip([video] + sub_clips)
        final_video.write_videofile(
            str(output_video_path),
            fps=video.fps or 30,
            codec="libx264",
            audio_codec="aac",
            logger=None
        )
        video.close()
        final_video.close()

