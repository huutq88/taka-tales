import os
import pathlib
from typing import List, Optional

PREFERRED_FONTS = [
    "Montserrat ExtraBold",
    "Montserrat-ExtraBold",
    "Montserrat",
    "Be Vietnam Pro",
    "BeVietnamPro-Bold",
    "Roboto",
    "Roboto-Bold",
    "Outfit",
    "Georgia",
    "Arial Bold",
    "Arial",
    "Helvetica",
    "Trebuchet MS",
    "Noto Sans",
    "DejaVu Sans"
]

FONT_SEARCH_PATHS = [
    pathlib.Path(__file__).parent.parent / "assets/fonts",
    pathlib.Path.home() / "Library/Fonts",
    pathlib.Path("/Library/Fonts"),
    pathlib.Path("/System/Library/Fonts/Supplemental"),
    pathlib.Path("/System/Library/Fonts"),
    pathlib.Path("/usr/share/fonts"),
    pathlib.Path("/usr/local/share/fonts"),
    pathlib.Path("/tmp")
]


class FontManager:
    @staticmethod
    def resolve_font_path(font_name: str) -> Optional[str]:
        """Resolves full path to a font file given its family name."""
        clean_name = font_name.lower().replace("-", "").replace(" ", "")
        
        for search_dir in FONT_SEARCH_PATHS:
            if search_dir.exists() and search_dir.is_dir():
                for ffile in search_dir.glob("*.[tT][tT][fF]"):
                    stem_clean = ffile.stem.lower().replace("-", "").replace(" ", "")
                    if clean_name in stem_clean or stem_clean in clean_name:
                        return str(ffile.resolve())
                for ffile in search_dir.glob("*.[oO][tT][fF]"):
                    stem_clean = ffile.stem.lower().replace("-", "").replace(" ", "")
                    if clean_name in stem_clean or stem_clean in clean_name:
                        return str(ffile.resolve())

        # Fallback to system default fonts
        for fallback in PREFERRED_FONTS:
            clean_fallback = fallback.lower().replace("-", "").replace(" ", "")
            for search_dir in FONT_SEARCH_PATHS:
                if search_dir.exists() and search_dir.is_dir():
                    for ffile in search_dir.glob("*.[tT][tT][fF]"):
                        stem_clean = ffile.stem.lower().replace("-", "").replace(" ", "")
                        if clean_fallback in stem_clean:
                            return str(ffile.resolve())
                            
        return None

    @staticmethod
    def get_ass_font_name(requested_font: str) -> str:
        """Returns the font family name suitable for ASS header font specification."""
        path = FontManager.resolve_font_path(requested_font)
        if path:
            filename = os.path.basename(path)
            if "Montserrat" in filename:
                return "Montserrat"
            if "BeVietnamPro" in filename or "Be Vietnam" in filename:
                return "Be Vietnam Pro"
            if "Roboto" in filename:
                return "Roboto"
            if "Georgia" in filename:
                return "Georgia"
            if "Arial" in filename:
                return "Arial"
            if "Helvetica" in filename:
                return "Helvetica"
            if "Noto" in filename:
                return "Noto Sans"
        return requested_font
