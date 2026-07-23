from typing import List, Tuple, Optional, Dict
from subtitle_engine.domain import Canvas, SafeArea, LayoutRules, Caption


class LayoutEngine:
    def __init__(self, canvas: Canvas = None, rules: LayoutRules = None):
        self.canvas = canvas or Canvas()
        self.rules = rules or LayoutRules()

    def get_safe_area(self) -> SafeArea:
        """Calculates Safe Area based on canvas dimensions and aspect ratio."""
        if self.canvas.width < self.canvas.height:
            # 9:16 Vertical Video (TikTok / Shorts / Reels)
            return SafeArea(
                left=int(0.08 * self.canvas.width),
                right=int(0.08 * self.canvas.width),
                top=int(0.12 * self.canvas.height),
                bottom=int(self.rules.safe_bottom)
            )
        else:
            # 16:9 Horizontal Video (YouTube / TV)
            return SafeArea(
                left=int(0.10 * self.canvas.width),
                right=int(0.10 * self.canvas.width),
                top=int(0.10 * self.canvas.height),
                bottom=int(0.15 * self.canvas.height)
            )

    def calculate_position(self, caption: Caption, obstacle_boxes: Optional[List[Dict[str, int]]] = None) -> Tuple[int, int]:
        """
        Calculates (X, Y) center position for a caption on the canvas.
        Automatically shifts layout if face/obstacle bounding boxes overlap bottom safe area.
        """
        safe_area = self.get_safe_area()
        center_x = self.canvas.width // 2
        default_y = self.canvas.height - safe_area.bottom - (len(caption.lines) * 45)

        if self.rules.type == "center":
            return (center_x, self.canvas.height // 2)

        if self.rules.type == "top-center":
            return (center_x, safe_area.top + 40)

        # Check if obstacle boxes (e.g. face boxes [y_min, y_max]) overlap bottom area
        if obstacle_boxes:
            caption_box_y_min = default_y - 30
            caption_box_y_max = default_y + (len(caption.lines) * 45) + 30

            for box in obstacle_boxes:
                obs_y_min = box.get("y", 0)
                obs_y_max = obs_y_min + box.get("height", 0)

                # Overlap check
                if not (caption_box_y_max < obs_y_min or caption_box_y_min > obs_y_max):
                    # Overlap detected: auto shift to mid-upper safe area
                    return (center_x, int(0.35 * self.canvas.height))

        return (center_x, default_y)
