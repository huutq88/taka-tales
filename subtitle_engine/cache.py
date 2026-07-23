import os
import pathlib
import hashlib
import json
from typing import Optional, Any, Dict


class SubtitleCache:
    def __init__(self, cache_dir: Optional[pathlib.Path] = None):
        if cache_dir:
            self.cache_dir = pathlib.Path(cache_dir)
        else:
            self.cache_dir = pathlib.Path.home() / ".taka-agent/cache/subtitles"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def compute_hash(self, audio_or_video_path: pathlib.Path, transcript: Optional[str] = None, preset_id: str = "") -> str:
        """Computes a unique MD5 hash for given media and text input."""
        hasher = hashlib.md5()
        
        # Audio file stat & name hash
        path_obj = pathlib.Path(audio_or_video_path)
        if path_obj.exists():
            hasher.update(path_obj.name.encode("utf-8"))
            hasher.update(str(path_obj.stat().st_size).encode("utf-8"))
            hasher.update(str(path_obj.stat().st_mtime).encode("utf-8"))

        if transcript:
            hasher.update(transcript.encode("utf-8"))

        if preset_id:
            hasher.update(preset_id.encode("utf-8"))

        return hasher.hexdigest()

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached JSON payload if present."""
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def set(self, cache_key: str, data: Dict[str, Any]):
        """Saves data dict to cache JSON file."""
        cache_file = self.cache_dir / f"{cache_key}.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SubtitleCache] Error writing cache: {e}")
