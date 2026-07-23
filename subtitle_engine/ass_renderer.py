import os
import pathlib
import datetime
from typing import List
from subtitle_engine.domain import RenderScene, Caption, StylePreset
from subtitle_engine.font_manager import FontManager


class ASSRenderer:
    def hex_to_ass_color(self, hex_color: str, alpha_hex: str = "00") -> str:
        """Converts #RRGGBB or #RRGGBBAA to ASS color format &HAABBGGRR."""
        hex_clean = hex_color.lstrip('#')
        if len(hex_clean) == 8:
            r, g, b, a = hex_clean[0:2], hex_clean[2:4], hex_clean[4:6], hex_clean[6:8]
            return f"&H{a}{b}{g}{r}".upper()
        elif len(hex_clean) == 6:
            r, g, b = hex_clean[0:2], hex_clean[2:4], hex_clean[4:6]
            return f"&H{alpha_hex}{b}{g}{r}".upper()
        return "&H00FFFFFF"

    def seconds_to_ass_time(self, seconds: float) -> str:
        """Converts float seconds to ASS timestamp format H:MM:SS.cc."""
        td = datetime.timedelta(seconds=max(0.0, seconds))
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        centiseconds = int((seconds - total_seconds) * 100)
        centiseconds = min(99, max(0, centiseconds))
        return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"

    def generate_ass_content(self, scene: RenderScene) -> str:
        """Generates complete ASS script content from RenderScene IR."""
        preset = scene.preset
        canvas = scene.canvas
        font_name = FontManager.get_ass_font_name(preset.font.family)
        
        primary_col = self.hex_to_ass_color(preset.text.color)
        active_col = self.hex_to_ass_color(preset.text.active_color)
        outline_col = self.hex_to_ass_color(preset.outline.color)
        shadow_col = self.hex_to_ass_color(preset.shadow.color, "80")

        header = f"""[Script Info]
Title: Taka Subtitle Engine Generated
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None
PlayResX: {canvas.width}
PlayResY: {canvas.height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{preset.font.size},{primary_col},{active_col},{outline_col},{shadow_col},-1,0,0,0,100,100,0,0,1,{preset.outline.width},{preset.shadow.y},2,80,80,{preset.layout.safe_bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        dialogues = []

        for cap in scene.captions:
            start_str = self.seconds_to_ass_time(cap.start)
            end_str = self.seconds_to_ass_time(cap.end)

            if not cap.words:
                # Fallback to simple line
                text_clean = cap.text.upper()
                dialogues.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text_clean}")
                continue

            # Build ASS Karaoke / Word Highlight tags
            # Format: {\k<cs>}Word 
            line_parts = []
            for w in cap.words:
                cs = int(max(0.05, w.end - w.start) * 100)
                word_text = w.text.upper()
                # Apply word-level karaoke tag with active color highlight tag
                line_parts.append(f"{{\\k{cs}}}{word_text}")

            text_karaoke = " ".join(line_parts)

            # Option: If pop animation preset is enabled, wrap with \\t(0, 150, \\fscx110\\fscy110)
            anim_tag = ""
            if preset.animation.caption_enter.get("type") == "pop":
                anim_tag = r"{\fscx90\fscy90\t(0,120,\fscx100\fscy100)}"

            dialogues.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{anim_tag}{text_karaoke}")

        return header + "\n".join(dialogues) + "\n"

    def render_to_file(self, scene: RenderScene, output_ass_path: pathlib.Path) -> pathlib.Path:
        """Writes ASS content to disk."""
        output_ass_path = pathlib.Path(output_ass_path)
        output_ass_path.parent.mkdir(parents=True, exist_ok=True)
        
        content = self.generate_ass_content(scene)
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(content)

        return output_ass_path
