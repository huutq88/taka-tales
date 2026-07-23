import os
import pathlib
from typing import List, Dict, Any
from subtitle_engine.domain import RenderScene, Caption, StylePreset


class SVGRenderer:
    """SVG Motion Graphics Subtitle Renderer."""
    
    def generate_caption_svg(self, caption: Caption, preset: StylePreset, canvas_w: int = 1080, canvas_h: int = 1920, active_word_idx: int = -1) -> str:
        """Generates an SVG string representation of a subtitle caption frame."""
        font_family = preset.font.family
        font_size = preset.font.size
        primary_color = preset.text.color
        active_color = preset.text.active_color
        stroke_color = preset.outline.color
        stroke_w = preset.outline.width
        safe_bottom = preset.layout.safe_bottom
        
        pos_y = canvas_h - safe_bottom

        words = caption.words or []
        tspan_elements = []

        if words:
            for idx, w in enumerate(words):
                fill = active_color if idx == active_word_idx else primary_color
                # Scale animation transform tag for active word
                transform = ' transform="scale(1.12)"' if idx == active_word_idx else ''
                tspan_elements.append(f'<tspan fill="{fill}"{transform}>{w.text.upper()}</tspan>')
            full_content = " ".join(tspan_elements)
        else:
            full_content = f'<tspan fill="{primary_color}">{caption.text.upper()}</tspan>'

        svg_template = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg width="{canvas_w}" height="{canvas_h}" viewBox="0 0 {canvas_w} {canvas_h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="6" stdDeviation="4" flood-color="#000000" flood-opacity="0.6"/>
    </filter>
  </defs>
  <style>
    .subtitle-text {{
      font-family: "{font_family}", Arial, sans-serif;
      font-size: {font_size}px;
      font-weight: 800;
      text-anchor: middle;
      paint-order: stroke fill;
      stroke: {stroke_color};
      stroke-width: {stroke_w}px;
      stroke-linejoin: round;
      filter: url(#shadow);
    }}
  </style>
  <text x="{canvas_w // 2}" y="{pos_y}" class="subtitle-text">
    {full_content}
  </text>
</svg>
"""
        return svg_template

    def export_svg_to_file(self, svg_content: str, output_path: pathlib.Path) -> pathlib.Path:
        """Writes SVG content string to disk."""
        output_path = pathlib.Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_content)
        return output_path
