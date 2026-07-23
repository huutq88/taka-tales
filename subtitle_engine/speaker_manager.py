from typing import Dict, List, Optional
from subtitle_engine.domain import Caption, TextStyle


class SpeakerConfig:
    def __init__(self, speaker_id: str, name: str, color: str, active_color: str):
        self.speaker_id = speaker_id
        self.name = name
        self.color = color
        self.active_color = active_color


DEFAULT_SPEAKER_PALETTES = [
    SpeakerConfig("speaker_0", "Người Kể", "#FFFFFF", "#FFD400"),  # White & Yellow
    SpeakerConfig("speaker_1", "Nhân Vật 1", "#F8FAFC", "#38BDF8"), # White & Cyan
    SpeakerConfig("speaker_2", "Nhân Vật 2", "#FDF4FF", "#F472B6"), # White & Pink
    SpeakerConfig("speaker_3", "Nhân Vật 3", "#F0FDF4", "#34D399"), # White & Emerald
]


class SpeakerManager:
    def __init__(self, custom_configs: Optional[List[SpeakerConfig]] = None):
        configs = custom_configs or DEFAULT_SPEAKER_PALETTES
        self.speakers: Dict[str, SpeakerConfig] = {c.speaker_id: c for c in configs}

    def get_speaker(self, speaker_id: str) -> SpeakerConfig:
        """Returns SpeakerConfig for given ID or default fallback."""
        return self.speakers.get(speaker_id, DEFAULT_SPEAKER_PALETTES[0])

    def apply_speaker_style(self, caption: Caption, speaker_id: str) -> Caption:
        """Applies speaker name prefix and color styling to a caption."""
        spk = self.get_speaker(speaker_id)
        
        # Prepend speaker badge if defined
        prefix = f"[{spk.name}]: " if spk.name and spk.speaker_id != "speaker_0" else ""
        new_text = f"{prefix}{caption.text}"

        return Caption(
            id=caption.id,
            start=caption.start,
            end=caption.end,
            text=new_text,
            lines=[new_text],
            words=caption.words
        )
