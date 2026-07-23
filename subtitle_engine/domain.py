from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class TimedWord(BaseModel):
    id: str
    text: str
    spoken_text: Optional[str] = None
    start: float  # seconds
    end: float    # seconds
    confidence: Optional[float] = 1.0


class CaptionLine(BaseModel):
    text: str
    words: List[TimedWord] = Field(default_factory=list)


class Caption(BaseModel):
    id: str
    start: float  # seconds
    end: float    # seconds
    text: str
    lines: List[str] = Field(default_factory=list)
    words: List[TimedWord] = Field(default_factory=list)


class Canvas(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30


class SafeArea(BaseModel):
    left: int = 80
    right: int = 80
    top: int = 160
    bottom: int = 320


class FontStyle(BaseModel):
    family: str = "Montserrat ExtraBold"
    size: int = 62
    weight: int = 800


class TextStyle(BaseModel):
    color: str = "#FFFFFF"
    active_color: str = "#FFD400"
    inactive_opacity: float = 1.0
    transform: str = "none"  # "uppercase", "lowercase", or "none"


class OutlineStyle(BaseModel):
    enabled: bool = True
    color: str = "#000000"
    width: int = 7


class ShadowStyle(BaseModel):
    enabled: bool = True
    color: str = "#000000AA"
    x: int = 0
    y: int = 5
    blur: int = 5


class SegmentationRules(BaseModel):
    min_words: int = 2
    max_words: int = 6
    min_duration_ms: int = 450
    max_duration_ms: int = 2500
    max_chars_per_line: int = 20
    max_lines: int = 2
    pause_threshold_ms: int = 320


class LayoutRules(BaseModel):
    type: str = "bottom-center"
    safe_bottom: int = 440
    max_width_ratio: float = 0.80
    line_spacing: int = 14


class AnimationRules(BaseModel):
    caption_enter: Dict[str, Any] = Field(default_factory=lambda: {"type": "pop", "duration_ms": 150})
    word_active: Dict[str, Any] = Field(default_factory=lambda: {"type": "scale-highlight", "scale": 1.12, "duration_ms": 100})
    caption_exit: Dict[str, Any] = Field(default_factory=lambda: {"type": "fade", "duration_ms": 100})


class StylePreset(BaseModel):
    id: str = "viral-bold-yellow"
    renderer_hint: str = "ass"
    segmentation: SegmentationRules = Field(default_factory=SegmentationRules)
    layout: LayoutRules = Field(default_factory=LayoutRules)
    font: FontStyle = Field(default_factory=FontStyle)
    text: TextStyle = Field(default_factory=TextStyle)
    outline: OutlineStyle = Field(default_factory=OutlineStyle)
    shadow: ShadowStyle = Field(default_factory=ShadowStyle)
    animation: AnimationRules = Field(default_factory=AnimationRules)


class RenderScene(BaseModel):
    version: str = "1.0"
    canvas: Canvas = Field(default_factory=Canvas)
    duration: float = 0.0
    captions: List[Caption] = Field(default_factory=list)
    preset: StylePreset = Field(default_factory=StylePreset)
