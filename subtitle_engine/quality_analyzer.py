from typing import List, Dict, Any
from subtitle_engine.domain import RenderScene, Caption, TimedWord


class QualityAnalyzer:
    def analyze(self, scene: RenderScene) -> Dict[str, Any]:
        """
        Analyzes subtitle scene quality.
        Returns quality score (0-100), warnings, and metrics.
        """
        warnings: List[Dict[str, Any]] = []
        score = 100

        if not scene.captions:
            return {
                "score": 0,
                "warnings": [{"type": "NO_CAPTIONS", "message": "Scene has no captions."}],
                "metrics": {"total_captions": 0}
            }

        total_words = 0
        total_duration = max(0.1, scene.duration)

        for cap in scene.captions:
            cap_duration = max(0.1, cap.end - cap.start)
            words = cap.words or []
            total_words += len(words)

            # Check 1: Reading speed (Words Per Second)
            wps = len(words) / cap_duration
            if wps > 4.5:
                score -= 5
                warnings.append({
                    "type": "READING_SPEED_TOO_FAST",
                    "caption_id": cap.id,
                    "text": cap.text,
                    "wps": round(wps, 2),
                    "message": f"Caption '{cap.text}' is read too fast ({wps:.1f} words/sec)."
                })

            # Check 2: Caption too long
            if len(cap.text) > 40:
                score -= 3
                warnings.append({
                    "type": "CAPTION_TOO_LONG",
                    "caption_id": cap.id,
                    "text": cap.text,
                    "message": f"Caption '{cap.text}' exceeds 40 characters."
                })

            # Check 3: Timing validity
            if cap.start < 0 or cap.end <= cap.start:
                score -= 10
                warnings.append({
                    "type": "INVALID_TIMING",
                    "caption_id": cap.id,
                    "message": f"Caption '{cap.id}' has invalid timestamps ({cap.start} -> {cap.end})."
                })

        wps_avg = total_words / total_duration
        score = max(0, min(100, score))

        return {
            "score": score,
            "warnings": warnings,
            "metrics": {
                "total_captions": len(scene.captions),
                "total_words": total_words,
                "avg_words_per_second": round(wps_avg, 2),
                "canvas": f"{scene.canvas.width}x{scene.canvas.height}"
            }
        }
