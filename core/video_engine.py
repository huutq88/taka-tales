"""
core/video_engine.py
~~~~~~~~~~~~~~~~~~~~

Turn a plain-text story into an illustrated, narrated video.
Restructured as a core package for Taka-Tales.
"""

from __future__ import annotations

import asyncio
import base64
import configparser
from functools import lru_cache
import gc
import io

import json
import multiprocessing
import os
import pathlib
import re
import shutil
import time

import requests
try:
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target_limit = min(10240, hard) if hard != resource.RLIM_INFINITY else 10240
    resource.setrlimit(resource.RLIMIT_NOFILE, (target_limit, hard))
except Exception:
    pass

def ensure_ffmpeg_on_path():
    if not shutil.which("ffmpeg"):
        try:
            import imageio_ffmpeg
            ff_exe = imageio_ffmpeg.get_ffmpeg_exe()
            if ff_exe and os.path.exists(ff_exe):
                ff_dir = os.path.dirname(ff_exe)
                os.environ["PATH"] = ff_dir + os.path.pathsep + os.environ.get("PATH", "")
                os.environ["FFMPEG_BINARY"] = ff_exe
                print(f"[FFmpeg Setup] Automatically configured imageio-ffmpeg binary: {ff_exe}")
        except Exception as e:
            print(f"[FFmpeg Warning] Could not auto-load imageio-ffmpeg: {e}")

ensure_ffmpeg_on_path()

# Configure ImageMagick path for MoviePy
os.environ["IMAGEMAGICK_BINARY"] = "/opt/homebrew/bin/convert"
from datetime import datetime
from typing import (Dict, List, Tuple)

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    import openai
except ImportError:
    openai = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    from fake_useragent import UserAgent
except ImportError:
    UserAgent = None

try:
    from keybert import KeyBERT
except ImportError:
    KeyBERT = None

try:
    from moviepy.audio.AudioClip import AudioClip
    from moviepy.audio.fx.all import volumex
    from moviepy.editor import (
        AudioFileClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        TextClip,
        VideoFileClip,
        VideoClip,
        concatenate_audioclips,
        concatenate_videoclips,
    )
except ImportError:
    AudioClip = None
    volumex = None
    AudioFileClip = None
    CompositeAudioClip = None
    CompositeVideoClip = None
    ImageClip = None
    TextClip = None
    VideoFileClip = None
    VideoClip = None
    concatenate_audioclips = None
    concatenate_videoclips = None

try:
    import nltk
    from nltk.tokenize import sent_tokenize, word_tokenize
    for nltk_res in ('punkt', 'punkt_tab'):
        try:
            nltk.data.find(f'tokenizers/{nltk_res}')
        except LookupError:
            try:
                nltk.download(nltk_res, quiet=True)
            except Exception:
                pass
except ImportError:
    try:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "nltk", "--break-system-packages"], check=False)
        import nltk
        from nltk.tokenize import sent_tokenize, word_tokenize
        for nltk_res in ('punkt', 'punkt_tab'):
            try:
                nltk.data.find(f'tokenizers/{nltk_res}')
            except LookupError:
                try:
                    nltk.download(nltk_res, quiet=True)
                except Exception:
                    pass
    except Exception:
        import re
        def sent_tokenize(text: str):
            if not text:
                return []
            return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
        def word_tokenize(text: str):
            if not text:
                return []
            return text.split()
from ollama import ChatResponse, chat
from PIL import Image, PngImagePlugin

# ---------- MoviePy FFMPEG override ----------
import moviepy.config as mpy_cfg

mpy_cfg.change_settings({"FFMPEG_BINARY": "ffmpeg"})

# ---------- Configuration ----------
_CONFIG_PATH = pathlib.Path(__file__).parent.parent / "config.ini"
config = configparser.ConfigParser()
config.read(_CONFIG_PATH, encoding="utf-8")

# GENERAL
DEBUG: bool = config["GENERAL"].getboolean("DEBUG", fallback=False)
SPEED_UP: bool = config["GENERAL"].getboolean("SPEED_UP", fallback=False)
FREE_SWAP_GB: int = int(config["GENERAL"]["FREE_SWAP"])
FPS: int = int(config["GENERAL"]["FPS"])

# TEXT
FRAGMENT_LENGTH: int = int(config.get("TEXT_FRAGMENT", "FRAGMENT_LENGTH", fallback="20"))

# AUDIO
TTS_PROVIDER: str = config.get("AUDIO", "TTS_PROVIDER", fallback="omnivoice")
ELEVENLABS_VOICE_ID: str = config.get("AUDIO", "ELEVENLABS_VOICE_ID", fallback="")
KOKORO_VOICE_ID: str = config.get("AUDIO", "KOKORO_VOICE_ID", fallback="af_heart")
KOKORO_URL: str = config.get("AUDIO", "KOKORO_URL", fallback="http://localhost:8880/v1/audio/speech")
VOICE: str = config.get("AUDIO", "VOICE", fallback="nam-bac-dao-ly")
BG_MUSIC: bool = config.getboolean("AUDIO", "BG_MUSIC", fallback=True)
BG_MUSIC_PATH: pathlib.Path = pathlib.Path(__file__).parent.parent / config.get("AUDIO", "BG_MUSIC_PATH", fallback="downloaded_albums/Hết Buồn Hết Điên Hết Say/01 - Tà Áo Lụa Trắng.mp3")
MUSIC_VOLUME: float = float(config.get("AUDIO", "MUSIC_VOLUME", fallback="0.15"))

# IMAGE PROMPTS
IMAGE_PROMPT_PROVIDER: str = config["IMAGE_PROMPT"]["IMAGE_PROMPT_PROVIDER"]
OLLAMA_MODEL: str = config["IMAGE_PROMPT"]["OLLAMA_MODEL"]

# STABLE DIFFUSION
POSITIVE_PREFIX: str = config["STABLE_DIFFUSION"]["positive_prompt_prefix"]
POSITIVE_SUFFIX: str = config["STABLE_DIFFUSION"]["positive_prompt_suffix"]
ART_STYLES: Dict[str, str] = {
    "2d_stick_figure": "minimalist 2D flat vector explainer illustration, educational infographic animation style, cream background #ECE7D8, stickman character with white circular head, thick 8px black outline, orange shirt #F4A621, black necktie, simple black limbs, clean geometric shapes, flat color fills, high contrast vector art, Adobe Illustrator style presentation",
    "2d-stick-figure-cartoon": "minimalist 2D flat vector explainer illustration, educational infographic animation style, cream background #ECE7D8, stickman character with white circular head, thick 8px black outline, orange shirt #F4A621, black necktie, simple black limbs, clean geometric shapes, flat color fills, high contrast vector art, Adobe Illustrator style presentation",
    "monochromatic_pencil_sketch": "A clean monochromatic graphite pencil concept art sketch, Warhammer 40K codex art style, clean pencil linework, smooth graphite shading, high contrast, dramatic cinematic lighting, focused composition, white and grey pencil tone, no color, dark grimdark atmosphere",
    "watercolor": "hand-painted watercolor style, soft edges, ink washes, detailed textures, classical literary book illustration of old Vietnam, warm nostalgic colors, featuring traditional Vietnamese clothing, Vietnamese village house, Vietnamese landscape, masterpiece",
    "thuy_mac_blackwhite": "strict monochrome traditional East Asian black ink wash brush painting, ancient Chinese sumi-e style, bold black ink calligraphic brush strokes, charcoal grey washes, pure black ink on aged white Xuan paper, minimalist zen ink painting, high contrast black and white, negative space, no colors, monochrome masterpiece",
    "cyber_tech_glassmorphism": "Futuristic Apple Silicon chip micro-architecture with semi-transparent frosted glass UI cards floating in 3D dark space, showing execution plans, memory bandwidth graph, glowing cyan and violet laser data pipelines representing unified memory, sleek dark obsidian background, 8k resolution, cinematic lighting, ultra-high detailed 3D render",
    "cyber-tech-glassmorphism": "Futuristic Apple Silicon chip micro-architecture with semi-transparent frosted glass UI cards floating in 3D dark space, showing execution plans, memory bandwidth graph, glowing cyan and violet laser data pipelines representing unified memory, sleek dark obsidian background, 8k resolution, cinematic lighting, ultra-high detailed 3D render"
}
NEGATIVE_PROMPT: str = config["STABLE_DIFFUSION"]["negative_prompt"]
USE_SD_API: str = config["STABLE_DIFFUSION"]["USE_SD_VIA_API"]
SD_URL: str = config["STABLE_DIFFUSION"]["SD_URL"]
SEED: int = int(config["STABLE_DIFFUSION"]["seed"])
IMAGE_WIDTH: int = int(config["STABLE_DIFFUSION"]["image_width"])
IMAGE_HEIGHT: int = int(config["STABLE_DIFFUSION"]["image_height"])
POLLINATIONS_MODEL: str = config["STABLE_DIFFUSION"].get("POLLINATIONS_MODEL", fallback="flux")
EFFECT_TYPE: str = config["STABLE_DIFFUSION"].get("EFFECT_TYPE", fallback="none")

def configure_project_resolution(project_dir: pathlib.Path = None, aspect_ratio: str = None) -> None:
    global IMAGE_WIDTH, IMAGE_HEIGHT
    is_horizontal = None
    if aspect_ratio:
        is_horizontal = (aspect_ratio in ("16:9", "horizontal", "landscape"))
    elif project_dir:
        dirs_to_check = [project_dir, project_dir.parent]
        if project_dir.parent and project_dir.parent.parent:
            dirs_to_check.append(project_dir.parent.parent)
        for check_d in dirs_to_check:
            if not check_d or not check_d.exists():
                continue
            ar_file = check_d / "aspect_ratio.txt"
            if ar_file.exists():
                try:
                    ar_val = ar_file.read_text(encoding="utf-8").strip()
                    if ar_val in ("16:9", "horizontal", "landscape"):
                        is_horizontal = True
                        break
                    elif ar_val in ("9:16", "vertical", "portrait"):
                        is_horizontal = False
                        break
                except Exception:
                    pass
            if (check_d / "project_config.json").exists():
                try:
                    with open(check_d / "project_config.json", "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    aspect_val = cfg.get("aspect_ratio") or cfg.get("aspect")
                    if aspect_val in ("16:9", "horizontal", "landscape"):
                        is_horizontal = True
                        break
                    elif aspect_val in ("9:16", "vertical", "portrait"):
                        is_horizontal = False
                        break
                except Exception:
                    pass
            if (check_d / "item.json").exists():
                try:
                    with open(check_d / "item.json", "r", encoding="utf-8") as f:
                        ij = json.load(f)
                    aspect_val = ij.get("aspect_ratio")
                    if aspect_val in ("16:9", "horizontal", "landscape"):
                        is_horizontal = True
                        break
                    elif aspect_val in ("9:16", "vertical", "portrait"):
                        is_horizontal = False
                        break
                except Exception:
                    pass
            if (check_d / "content.json").exists():
                try:
                    with open(check_d / "content.json", "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                    if isinstance(cdata, dict):
                        aspect_val = cdata.get("aspect_ratio")
                        if aspect_val in ("16:9", "horizontal", "landscape"):
                            is_horizontal = True
                            break
                        elif aspect_val in ("9:16", "vertical", "portrait"):
                            is_horizontal = False
                            break
                except Exception:
                    pass

    if is_horizontal is None:
        is_horizontal = True

    if is_horizontal:
        IMAGE_WIDTH = 1824
        IMAGE_HEIGHT = 1024
        _log(f"[VideoEngine] Resolution set to 16:9 Horizontal (Long-Form): {IMAGE_WIDTH}x{IMAGE_HEIGHT}")
    else:
        IMAGE_WIDTH = 1024
        IMAGE_HEIGHT = 1824
        _log(f"[VideoEngine] Resolution set to 9:16 Vertical (Reels): {IMAGE_WIDTH}x{IMAGE_HEIGHT}")

USE_CHAR_DESC: bool = config["STABLE_DIFFUSION"].getboolean("USE_CHARACTERS_DESCRIPTIONS")
SHOW_WATERMARK: bool = config["STABLE_DIFFUSION"].getboolean("SHOW_WATERMARK", fallback=True)
CHAR_DESC: Dict[str, str] = {}
if USE_CHAR_DESC:
    _CHAR_DESC_PATH = pathlib.Path(__file__).parent / "characters_descriptions.ini"
    if _CHAR_DESC_PATH.exists():
        _cd = configparser.ConfigParser()
        _cd.read(_CHAR_DESC_PATH, encoding="utf-8")
        CHAR_DESC = dict(_cd["CHARACTERS_DESCRIPTIONS"])

# API keys from environment
if TTS_PROVIDER == "elevenlabs":
    openai.api_key = os.environ["ELEVENLABS_API_KEY"]

if IMAGE_PROMPT_PROVIDER == "chatgpt":
    openai.api_key = os.environ["OPENAI_TOKEN"]

# ---------- Utilities ----------
_TIMESTAMP_FMT = "[%Y-%m-%d %H:%M:%S UTC]"


def _log(msg: str) -> None:
    """Print timestamped message when DEBUG=True."""
    if DEBUG:
        print(f"{datetime.utcnow().strftime(_TIMESTAMP_FMT)}  {msg}")


def _write_text(path: pathlib.Path, text: str) -> None:
    """Atomic write with UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text(path: pathlib.Path) -> str:
    """Read UTF-8 file."""
    return path.read_text(encoding="utf-8")


# ---------- Text Processing ----------
def clean_text(text: str) -> str:
    """Normalize punctuation, quotes, dashes, HTML tags and Markdown."""
    # 0. Convert escaped literal '\n' sequences to real linebreaks
    text = text.replace("\\n", "\n")

    # 1. Remove HTML tags like <i>, <b>, <p>, <br> entirely
    text = re.sub(r'<[^>]+>', '', text)
    
    # 2. Clean markdown links: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # 2.5 Normalize website/domain names for correct pronunciation
    text = re.sub(r'\bgog\.zone\b', 'gờ o gờ chấm zôn', text, flags=re.IGNORECASE)
    
    # 3. Standard character mapping
    # NOTE: Do NOT strip Vietnamese tone marks (é, ê, ô, ơ, ư, etc.)
    # NOTE: Preserve \n\n and ... for proper paragraph breaks & pauses
    mapping = {
        ">": "",
        "<": "",
        "=": "",
        "#": "",
        "\u201c": "",
        "\u201d": "",
        "-": " ",
        "\u2013": " ",
        "\u2014": " ",
        "*": "",
        "_": "",
        "~": "",
        "XXXXXX": "",
        "xxxxx": "",
        ".....": "...",
        "....": "...",
        "\u2026": "...",
    }
    for k, v in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
        text = text.replace(k, v)
    return text


def load_and_split_to_sentences(story_path: pathlib.Path) -> int:
    """
    Split *story.txt* into sentences and write into
    ``text/story_sentences/story_sentence{idx}.txt``.
    Respects paragraph breaks (\n\n) as hard sentence/fragment boundaries.
    """
    raw = story_path.read_text(encoding="utf-8")
    raw = clean_text(raw)

    # Split by paragraph breaks (\n\n or multi-newlines)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', raw) if p.strip()]

    punctuation_list = [',', ';', ':', '...']
    new_sentences: List[str] = []
    frag_len = 3 * FRAGMENT_LENGTH

    for p in paragraphs:
        # Split paragraph into sentences via sent_tokenize
        sentences = sent_tokenize(p)
        for sent in sentences:
            sent_str = sent.strip()
            if not sent_str:
                continue
            words = sent_str.split()
            if len(words) <= FRAGMENT_LENGTH:
                new_sentences.append(sent_str)
            else:
                part = []
                for word in words:
                    part.append(word)
                    if (word[-1] in punctuation_list or word.endswith("...")) and len(part) >= frag_len:
                        new_sentences.append(' '.join(part))
                        part = []
                if part:
                    new_sentences.append(" ".join(part))

        # Insert paragraph break sentinel
        new_sentences.append("<PARA_BREAK>")

    if new_sentences and new_sentences[-1] == "<PARA_BREAK>":
        new_sentences.pop()

    for idx, sent in enumerate(new_sentences):
        _write_text(story_path.parent / f"text/story_sentences/story_sentence{idx}.txt", sent)

    _log(f"Created {len(new_sentences)} sentence files across {len(paragraphs)} paragraphs.")
    return len(new_sentences)


def sentences_to_fragments(num_sentences: int, project_dir: pathlib.Path) -> int:
    """
    Group consecutive sentences into fragments of at least *FRAGMENT_LENGTH* words,
    flushing fragments immediately on paragraph boundaries (<PARA_BREAK>).
    """
    fragments: List[str] = []
    current_words: List[str] = []

    for i in range(num_sentences):
        sentence = _read_text(project_dir / f"text/story_sentences/story_sentence{i}.txt")
        if sentence == "<PARA_BREAK>":
            if current_words:
                fragments.append(" ".join(current_words))
                current_words = []
            continue

        current_words.extend(sentence.split())
        if len(current_words) >= FRAGMENT_LENGTH:
            fragments.append(" ".join(current_words))
            current_words = []

    if current_words:
        fragments.append(" ".join(current_words))

    for idx, frag in enumerate(fragments):
        _write_text(project_dir / f"text/story_fragments/story_fragment{idx}.txt", frag)

    _log(f"Created {len(fragments)} fragment files.")
    return len(fragments)


# ---------- Image Prompt Generation ----------
def _unload_ollama() -> None:    
    if IMAGE_PROMPT_PROVIDER == "ollama":
        try:
            url = 'http://localhost:11434/api/generate'
            data = {'model': OLLAMA_MODEL, 'keep_alive': 0}
            response = requests.post(url, json=data, timeout=5)
            print(response.text)
            time.sleep(3)
        except Exception as e:
            print(f"[Engine] Warning: Failed to unload Ollama: {e}")
        
        
def _reload_ollama() -> None:    
    if IMAGE_PROMPT_PROVIDER == "ollama":
        try:
            url = 'http://localhost:11434/api/generate'
            data = {'model': OLLAMA_MODEL, 'keep_alive': 1}
            response = requests.post(url, json=data, timeout=5)
            print(response.text)
            time.sleep(3)
        except Exception as e:
            print(f"[Engine] Warning: Failed to reload Ollama: {e}")


def _find_characters(fragment: str) -> str:
    for name, desc in CHAR_DESC.items():
        if re.search(rf"\b{name}\b", fragment, flags=re.IGNORECASE):
            return f"[[[ {desc} ]]], "
    return ""


@lru_cache(maxsize=1)
def _get_kw_model() -> KeyBERT:
    return KeyBERT("all-mpnet-base-v2")


def _keywords_fallback(fragment: str) -> str:
    kw_model = _get_kw_model()
    ngram_range = (1, 8)
    keywords = kw_model.extract_keywords(
        fragment,
        keyphrase_ngram_range=ngram_range, 
        stop_words='english', 
        highlight=False,
        top_n=1
    )
    keywords_list = list(dict(keywords).keys())
    del kw_model
    del keywords
    gc.collect()
    image_prompt = ', '.join(keywords_list)
    return image_prompt
    

VIETNAMESE_CONCEPT_MAP: Dict[str, str] = {
    "túi tiền": "an ornate vintage leather coin pouch resting on an ancient carved wooden desk in a quiet study",
    "tài chính": "golden ancient coins spread on an aged wooden table beside an old inkwell",
    "tâm hồn": "a serene scholar meditating peacefully beside a tranquil lotus pond with floating petals",
    "bản lĩnh": "a solitary figure standing firm on a cliff overlooking a misty valley during sunrise",
    "cơn giận": "dark storm clouds gathering over a mountain ridge with wild wind",
    "buông bỏ": "gentle autumn leaves drifting down on an ancient mossy stone path leading to a pagoda",
    "quá khứ": "an old hand-drawn scroll unrolled on a wooden table beside a burning incense burner",
    "nhìn thấu": "an elderly wise scholar gazing quietly into a clear still water reflection",
    "lòng người": "a quiet traditional tea set placed on a bamboo table near a sunlit window",
    "im lặng": "a peaceful bamboo grove shrouded in soft morning mist with sunbeams filtering through",
    "trưởng thành": "a tall majestic pine tree standing resiliently against mountain breezes",
    "nội tâm": "a candle flame burning softly in a quiet room, serene peaceful atmosphere",
    "bản chất": "a calm clear stream flowing over smooth polished stones in a forest",
    "bình yên": "a sleepy Vietnamese countryside village at dusk with soft glowing lanterns",
    "trí tuệ": "an old sage writing calligraphy on Xuan paper with a traditional brush",
    "học hỏi": "an ancient open book resting beside an oil lamp in a traditional library",
    "thành công": "a sunburst breaking through clouds above a high mountain peak",
    "thất bại": "falling rain drops on a calm lake surface, reflective mood",
    "gia đình": "a cozy traditional Vietnamese wooden house surrounded by green gardens",
    "bạn bè": "two travelers sipping tea under an old banyan tree in a tranquil courtyard"
}


def _smart_vietnamese_prompt(fragment: str, art_style: str = None) -> str:
    frag_lower = fragment.lower()
    for kw, visual in VIETNAMESE_CONCEPT_MAP.items():
        if kw in frag_lower:
            if art_style not in ("dong_ho", "son_mai", "watercolor", "woodblock"):
                visual = visual.replace("Vietnamese ", "").replace("Vietnamese", "")
            return visual
    
    try:
        kw = _keywords_fallback(fragment)
        if kw and len(kw) > 3 and not any(x in kw.lower() for x in ("không", "là", "của", "với")):
            if art_style in ("dong_ho", "son_mai", "watercolor", "woodblock"):
                return f"a traditional Vietnamese visual scene depicting {kw}"
            return f"a detailed visual scene depicting {kw}"
    except Exception:
        pass
    return "a peaceful landscape with misty mountains and trees"


def build_image_prompt(fragment: str, art_style: str = None) -> str:
    style_suffix = ART_STYLES.get(art_style, POSITIVE_SUFFIX) if art_style else POSITIVE_SUFFIX
    color_rule = ""
    if art_style in ("2d_stick_figure", "2d-stick-figure-cartoon"):
        color_rule = " CRITICAL: The image MUST be a minimalist 2D flat vector stickman character with white circular head, thick 8px black outline, orange shirt (#F4A621), and black necktie on a cream background (#ECE7D8)."
    elif art_style == "thuy_mac_blackwhite":
        color_rule = " CRITICAL: The image MUST be strict monochrome black and white ink wash brush drawing (sumi-e style). DO NOT mention any colors (such as warm, red, blue, green, yellow, watercolor)."

    no_text_rule = ""
    if art_style in ("watercolor", "thuy_mac_blackwhite", "monochromatic_pencil_sketch"):
        no_text_rule = " CRITICAL: Do NOT include any written text, letters, words, chinese calligraphy, watermark, signature, or typography inside the generated image scene."

    prompt_instruction = (
        "Translate the specific visual subject, object, and scene of the following sentence into a vivid single-sentence English image prompt for Stable Diffusion. "
        f"Style context: {style_suffix}.{color_rule}{no_text_rule} "
        "Describe ONLY the specific objects, figures, or actions present in the input text. Max 20 words. "
        "DO NOT add unmentioned cultural or religious tropes (such as Buddha, Vietnamese temple, bamboo, or lanterns) unless explicitly present in the input text. "
        "Strictly align with the theme, tone, and atmosphere of the style context. Output ONLY the English prompt."
    )

    prompt = ""
    if IMAGE_PROMPT_PROVIDER == "chatgpt":
        try:
            api_key = os.environ.get("OPENAI_TOKEN") or os.environ.get("OPENAI_API_KEY")
            if api_key:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": f"{prompt_instruction}\n{fragment}"}],
                    max_tokens=50
                )
                prompt = response.choices[0].message.content.strip()
        except Exception as e:
            _log(f"ChatGPT API failed: {e}. Using Smart Concept Fallback.")

    elif IMAGE_PROMPT_PROVIDER == "ollama":
        try:
            resp: ChatResponse = chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": f"{prompt_instruction}\n{fragment}"}],
            )
            prompt = resp["message"]["content"].strip()
            _log(prompt)
        except Exception as e:
            _log(f"Ollama failed: {e}. Using Smart Concept Fallback.")

    if not prompt or any(x in prompt.lower() for x in ("i cannot", "?", "failed")):
        prompt = _smart_vietnamese_prompt(fragment, art_style)

    if CHAR_DESC:
        prompt = _find_characters(fragment) + prompt

    return prompt


def _unload_sd() -> None:
    if USE_SD_API == "yes":
        try:
            response = requests.post(url=f"{SD_URL}/sdapi/v1/unload-checkpoint", json={}, timeout=5)
            print(response.text)
            time.sleep(3)
        except Exception as e:
            print(f"[Engine] Warning: Failed to unload SD: {e}")
    

def _reload_sd() -> None:
    if USE_SD_API == "yes":
        try:
            response = requests.post(url=f"{SD_URL}/sdapi/v1/reload-checkpoint", json={}, timeout=5)
            print(response.text)
            time.sleep(3)
        except Exception as e:
            print(f"[Engine] Warning: Failed to reload SD: {e}")


def generate_sd_payload(prompt: str, negative_prompt: str) -> Dict[str, Any]:
    return {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "steps": 20,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "seed": SEED,
        "guidance_scale": 4.0,
        "sampler_index": "Euler a",
    }


def generate_image(idx: int, project_dir: pathlib.Path, art_style: str = None, force: bool = False, aspect_ratio: str = None) -> None:
    configure_project_resolution(project_dir, aspect_ratio=aspect_ratio)
    prompt_path = project_dir / f"text/image_prompts/image_prompt{idx}.txt"
    image_path = project_dir / f"images/image{idx}.jpg"
    if image_path.exists() and not force:
        return

    prompt_raw = _read_text(prompt_path)
    
    if art_style in ("2d_stick_figure", "2d-stick-figure-cartoon"):
        preset_file = pathlib.Path(__file__).parent.parent / "presets/2d-stick-figure-cartoon.json"
        prefix = "minimalist 2D flat vector explainer illustration, educational infographic animation style, cream background #ECE7D8, stickman character with white circular head, thick 8px black outline, orange shirt #F4A621, black necktie, simple black limbs"
        style_suffix = "clean geometric shapes, flat color fills, no gradients, no photorealism, no texture, high contrast vector art, Adobe Illustrator style presentation"
        negative_str = "photorealistic, 3d render, complex gradient, dark shadows, noise, grainy texture, busy detailed background, glossy"
        if preset_file.exists():
            try:
                with open(preset_file, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                ip = pdata.get("image_prompt", {})
                prefix = ip.get("prefix", prefix)
                style_suffix = ip.get("suffix", style_suffix)
                negative_str = ip.get("negative", negative_str)
            except Exception:
                pass
        p_raw = prompt_raw.strip()
        if "minimalist 2D flat vector stickman character" in p_raw or "white circular head" in p_raw:
            prompt = f"{p_raw}, {style_suffix}"
        else:
            prompt = f"{prefix}, {p_raw}, {style_suffix}"
    elif art_style in ("monochromatic_pencil_sketch", "pencil_sketch", "sketch"):
        prefix = "A clean monochromatic graphite pencil concept art sketch of"
        style_suffix = "Warhammer 40K codex art style, clean pencil linework, smooth graphite shading, high contrast, dramatic cinematic lighting, focused composition, white and grey pencil tone, no color"
        negative_str = "color, colorful, red, blue, green, yellow, warm colors, watercolor, oil painting, 3d, cgi, photorealistic, 2d stick figure, cartoon, nsfw, text, letters, words, signature, watermark"
        prompt = f"{prefix} {prompt_raw}, {style_suffix}"
    elif art_style == "thuy_mac_blackwhite":
        prefix = "traditional monochrome black and white Chinese ink brush painting, sumi-e style, masterwork black ink brushwork on white Xuan paper,"
        style_suffix = "pure black ink, charcoal grey wash gradients, expressive calligraphic brush strokes, negative space, dramatic silhouette, zen art aesthetic, no color, black and white masterpiece"
        negative_str = "color, colorful, red, green, blue, yellow, warm colors, watercolor, oil painting, 3d, cgi, photorealistic, digital painting, nsfw, text, letters, words, signature, watermark, typography, chinese characters"
        prompt = f"{prefix} {prompt_raw}, {style_suffix}"
    elif art_style in ("cyber_tech_glassmorphism", "cyber-tech-glassmorphism"):
        prefix = "Futuristic 3D cyber-tech glassmorphism scene depicting"
        style_suffix = "semi-transparent frosted glass UI cards floating in 3D dark space, showing system execution plans, glowing cyan and violet laser data pipelines, sleek dark obsidian background, 8k resolution, cinematic lighting, ultra-high detailed 3D render"
        negative_str = "flat 2d, stick figure, watercolor, hand drawn, sketchy, low quality, blurry, bright daylight, oversaturated, text errors, watermark"
        prompt = f"{prefix} {prompt_raw}, {style_suffix}"
    elif art_style == "watercolor":
        prefix = "traditional Vietnamese watercolor illustration showing"
        style_suffix = "hand-painted watercolor style, soft edges, ink washes, detailed textures, classical literary book illustration of old Vietnam, warm nostalgic colors, featuring traditional Vietnamese clothing, Vietnamese village house, Vietnamese landscape, masterpiece"
        negative_str = "low quality, worst quality, watermark, logo, text, letters, words, signature, calligraphy, typography, chinese characters, asian text, extra limbs, bad anatomy, deformed, cgi, 3d render"
        prompt = f"{prefix} {prompt_raw}, {style_suffix}"
    else:
        style_suffix = ART_STYLES.get(art_style, POSITIVE_SUFFIX) if art_style else POSITIVE_SUFFIX
        prompt = f"{POSITIVE_PREFIX} {prompt_raw} {style_suffix}"
        negative_str = NEGATIVE_PROMPT
    
    _log(f"{idx} Loaded Prompt: {prompt}")
    attempts = 0
    max_retries = 2
    wait_time = 2
    
    while attempts < max_retries:
        attempts += 1
        try:
            if USE_SD_API == "yes":
                payload = {
                    "prompt": prompt,
                    "negative_prompt": negative_str,
                    "steps": 20,
                    "cfg_scale": 7,
                    "width": IMAGE_WIDTH,
                    "height": IMAGE_HEIGHT,
                    "seed": SEED,
                }
                url = SD_URL.rstrip('/')
                option_payload = {
                    "sd_model_checkpoint": "sd_xl_base_1.0.safetensors [31e35c80cf]",
                    "sd_vae": "sdxl_vae.safetensors",
                }
                requests.post(f"{url}/sdapi/v1/options", json=option_payload)
                r = requests.post(f"{url}/sdapi/v1/txt2img", json=payload).json()

                for b64 in r["images"]:
                    img = Image.open(io.BytesIO(base64.b64decode(b64.split(",", 1)[0])))
                    info = PngImagePlugin.PngInfo()
                    info.add_text("parameters", r.get("info", ""))
                    img.save(image_path, pnginfo=info)

            elif USE_SD_API == "pollinations":
                import urllib.parse
                ua = UserAgent()
                encoded_prompt = urllib.parse.quote(prompt)
                encoded_negative = urllib.parse.quote(negative_str)
                url = (
                    f"https://image.pollinations.ai/prompt/{encoded_prompt}"
                    f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&nologo=true&model={POLLINATIONS_MODEL}&enhance=false"
                    f"&seed={time.time()}&negative={encoded_negative}"
                )
                response = requests.get(url, headers={"User-Agent": ua.random}, timeout=60)
                if response.status_code == 200:
                    image = io.BytesIO(response.content)
                    img = Image.open(image)
                    img.save(image_path)
                else:
                    raise requests.exceptions.HTTPError(f'Failed to download. Status: {response.status_code}')

            elif USE_SD_API == "ima2":
                import subprocess
                # Map size to OpenAI ima2 exact aspect ratio sizes: 1824x1024 (16:9 Landscape), 1024x1824 (9:16 Portrait), 1024x1024 (1:1 Square)
                if (aspect_ratio and aspect_ratio in ("9:16", "vertical", "portrait")) or IMAGE_HEIGHT > IMAGE_WIDTH:
                    ima2_size = "1152x2048"
                    final_prompt = f"Vertical 9:16 portrait orientation, tall vertical mobile frame format, {prompt}"
                elif (aspect_ratio and aspect_ratio in ("16:9", "horizontal", "landscape")) or IMAGE_WIDTH > IMAGE_HEIGHT:
                    ima2_size = "1824x1024"
                    final_prompt = f"Horizontal 16:9 widescreen landscape orientation, wide cinematic format, {prompt}"
                else:
                    if IMAGE_HEIGHT >= IMAGE_WIDTH:
                        ima2_size = "1152x2048"
                        final_prompt = f"Vertical 9:16 portrait orientation, tall vertical mobile frame format, {prompt}"
                    else:
                        ima2_size = "1824x1024"
                        final_prompt = f"Horizontal 16:9 widescreen landscape orientation, wide cinematic format, {prompt}"

                _log(f"[VideoEngine] Executing ima2 gen with size: {ima2_size} (-s {ima2_size}) for image {idx}...")
                cmd = [
                    "ima2", "gen", final_prompt,
                    "--mode", "direct",
                    "--quality", "low",
                    "--model", "oauth/gpt-5.6-luna",
                    "-s", ima2_size,
                    "-o", str(image_path)
                ]
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                except subprocess.TimeoutExpired:
                    _log(f"[Agent] ima2-gen timed out after 300s for image {idx}. Retrying...")
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if res.returncode != 0:
                    _log(f"[Agent] ima2-gen error: {res.stderr.strip()}. Tự động xoay token Codex auth từ pool...")
                    try:
                        from core.rotate_ima2_auth import rotate_auth
                        rotate_auth()
                    except Exception as e:
                        _log(f"[Agent] Lỗi khi xoay auth: {e}")
                    res_retry = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if res_retry.returncode != 0:
                        raise RuntimeError(f"ima2-gen error sau khi re-auth: {res_retry.stderr}")
                
                # Post-process generated image to ensure clean 24-bit RGB JPEG
                png_counterpart = image_path.with_suffix('.png')
                webp_counterpart = image_path.with_suffix('.webp')
                target_file = image_path if image_path.exists() else (png_counterpart if png_counterpart.exists() else (webp_counterpart if webp_counterpart.exists() else None))

                if target_file and target_file.exists():
                    try:
                        with Image.open(target_file) as im:
                            rgb_im = im.convert("RGB")
                            target_w, target_h = IMAGE_WIDTH, IMAGE_HEIGHT
                            orig_w, orig_h = rgb_im.size
                            target_ratio = target_w / target_h
                            orig_ratio = orig_w / orig_h

                            # If both target and source have matching orientation (both vertical or both horizontal), use direct LANCZOS resize to preserve 100% of image content (0% cropped)
                            if abs(orig_ratio - target_ratio) < 0.25 or (target_h > target_w and orig_h > orig_w) or (target_w > target_h and orig_w > orig_h):
                                final_im = rgb_im.resize((target_w, target_h), Image.Resampling.LANCZOS)
                            else:
                                # Center-crop aspect-fill only if source image has a completely different aspect ratio
                                scale = max(target_w / orig_w, target_h / orig_h)
                                new_w, new_h = round(orig_w * scale), round(orig_h * scale)
                                scaled_im = rgb_im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                                left = (new_w - target_w) // 2
                                top = (new_h - target_h) // 2
                                final_im = scaled_im.crop((left, top, left + target_w, top + target_h))
                            final_im.save(image_path, "JPEG", quality=95, optimize=True)
                        if png_counterpart.exists() and png_counterpart != image_path:
                            png_counterpart.unlink()
                        if webp_counterpart.exists() and webp_counterpart != image_path:
                            webp_counterpart.unlink()
                    except Exception as ex:
                        _log(f"[Engine] Warning: Failed to convert {target_file} to JPEG: {ex}")
                
            break
            
        except Exception as e:   
            _log(f"Exception!!! {idx} (attempt {attempts}/{max_retries})\n{e}")
            if attempts >= max_retries:
                break
            time.sleep(wait_time)


# ---------- TTS ----------
EDGE_VOICE_MAP = {
    "nam-dao-ly": "vi-VN-NamMinhNeural",
    "nam_dao_ly": "vi-VN-NamMinhNeural",
    "nam-bac-dao-ly": "vi-VN-NamMinhNeural",
    "nam_bac_dao_ly": "vi-VN-NamMinhNeural",
    "nam-doc-truyen": "vi-VN-NamMinhNeural",
    "nam_doc_truyen": "vi-VN-NamMinhNeural",
    "nu-doc-truyen": "vi-VN-HoaiMyNeural",
    "nu_doc_truyen": "vi-VN-HoaiMyNeural",
    "nu-appota": "vi-VN-HoaiMyNeural",
    "nu_appota": "vi-VN-HoaiMyNeural",
}

async def tts_edge(text: str, out: pathlib.Path, voice: str = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    target_voice = voice or VOICE
    edge_voice = EDGE_VOICE_MAP.get(target_voice, target_voice)
    if not edge_voice or not (edge_voice.startswith("vi-") or ("-" in edge_voice and len(edge_voice) > 10)):
        edge_voice = "vi-VN-NamMinhNeural"
    try:
        com = edge_tts.Communicate(text, edge_voice)
        await com.save(str(out))
    except Exception as e:
        print(f"[VideoEngine] Edge-TTS error with voice '{edge_voice}': {e}. Retrying with vi-VN-NamMinhNeural...")
        com = edge_tts.Communicate(text, "vi-VN-NamMinhNeural")
        await com.save(str(out))


def tts_elevenlabs(text: str, out: pathlib.Path) -> None:
    url = "https://api.elevenlabs.io/v1/user/subscription"
    headers = {
          "Accept": "audio/mpeg",
          "Content-Type": "application/json",
          "xi-api-key": ELEVENLABS_API_KEY
    }
    usage = requests.get(url, headers=headers).json()
    if usage["character_limit"] - usage["character_count"] < len(text)+1:
        raise RuntimeError("ElevenLabs character limit almost exceeded!")

    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    resp = requests.post(tts_url, json=payload, headers=headers)
    resp.raise_for_status()
    with open(out, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024):
            f.write(chunk)


def tts_kokoro(text: str, out: pathlib.Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        KOKORO_URL,
        json={
            "model": "kokoro",
            "input": text.lower(),
            "voice": KOKORO_VOICE_ID,
            "speed": 1.0,
            "response_format": "wav",
            "stream": True,
        },
        stream=True,
    )
    resp.raise_for_status()
    with open(out, "wb") as f:
        shutil.copyfileobj(resp.raw, f)


def split_text_to_subtitles(text: str, max_words: int = 8) -> List[str]:
    import re
    parts = re.split(r'([,,;;\.\!\?\:\-\n]+)', text)
    
    phrases = []
    current_phrase = ""
    for part in parts:
        if not part.strip():
            continue
        if re.match(r'^[,,;;\.\!\?\:\-\n]+$', part):
            current_phrase += part
            phrases.append(current_phrase.strip())
            current_phrase = ""
        else:
            if current_phrase:
                phrases.append(current_phrase.strip())
            current_phrase = part
            
    if current_phrase:
        phrases.append(current_phrase.strip())
        
    phrases = [p for p in phrases if p]
    
    final_subtitles = []
    for phrase in phrases:
        words = phrase.split()
        if len(words) <= max_words:
            final_subtitles.append(phrase)
        else:
            for i in range(0, len(words), max_words):
                chunk = " ".join(words[i:i+max_words])
                final_subtitles.append(chunk)
                
    return final_subtitles


# ---------- Video Assembly ----------
def get_processed_watermark() -> str | None:
    watermark_src = "watermark_white.png"
    if not os.path.exists(watermark_src):
        return None
    
    # Processed watermark path will be cached based on target dimensions
    watermark_processed = f"watermark_{IMAGE_WIDTH}x{IMAGE_HEIGHT}.png"
    
    # Check if already processed and up-to-date
    if os.path.exists(watermark_processed):
        return watermark_processed
        
    try:
        from PIL import Image
        img = Image.open(watermark_src)
        w, h = img.size
        
        # Bounding boxes for top and bottom parts
        top_h = 620
        bottom_h = 620
        
        top_box = img.crop((0, 0, w, top_h))
        bottom_left_box = img.crop((0, h - bottom_h, 750, h))
        bottom_right_box = img.crop((2900, h - bottom_h, w, h))
        
        # Isotropic scale factor based on target_height (old scale)
        scale = IMAGE_HEIGHT / h
        
        # Resize top logo
        new_top_w = int(w * scale)
        new_top_h = int(top_h * scale)
        resized_top = top_box.resize((new_top_w, new_top_h), Image.Resampling.LANCZOS)
        
        # Resize bottom left icon
        new_bl_w = int(750 * scale)
        new_bl_h = int(bottom_h * scale)
        resized_bl = bottom_left_box.resize((new_bl_w, new_bl_h), Image.Resampling.LANCZOS)
        
        # Resize bottom right icon
        new_br_w = int((w - 2900) * scale)
        new_br_h = int(bottom_h * scale)
        resized_br = bottom_right_box.resize((new_br_w, new_br_h), Image.Resampling.LANCZOS)
        
        # Create canvas and paste elements at their correct positions
        canvas = Image.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT), (0, 0, 0, 0))
        canvas.paste(resized_top, (0, 0), resized_top)
        # canvas.paste(resized_bl, (0, IMAGE_HEIGHT - new_bl_h), resized_bl)
        canvas.paste(resized_br, (IMAGE_WIDTH - new_br_w, IMAGE_HEIGHT - new_br_h), resized_br)
        
        canvas.save(watermark_processed, "PNG")
        print(f"Processed watermark saved at {watermark_processed}")
        return watermark_processed
    except Exception as e:
        print(f"Error processing watermark: {e}")
        return None
def generate_procedural_waveform_frames(target_w: int, target_h: int) -> list:
    from PIL import Image as PILImage, ImageDraw
    import math
    frames = []
    num_frames = 100
    num_bars = 40
    bar_width = max(2, int(target_w / (num_bars * 1.5)))
    spacing = max(1, int(bar_width * 0.3))
    total_bars_width = num_bars * bar_width + (num_bars - 1) * spacing
    start_x = (target_w - total_bars_width) // 2
    
    for f in range(num_frames):
        img = PILImage.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        for i in range(num_bars):
            dist_from_center = abs(i - (num_bars / 2)) / (num_bars / 2)
            bell = math.exp(-3.0 * (dist_from_center ** 2)) # gaussian bell curve
            
            # Oscillating waves
            w1 = math.sin(f * 0.15 + i * 0.2)
            w2 = math.cos(f * 0.1 - i * 0.1)
            oscillation = 0.15 + 0.85 * abs(0.6 * w1 + 0.4 * w2)
            
            height_factor = bell * oscillation * 0.8
            bar_h = max(2, int(target_h * height_factor))
            
            x0 = start_x + i * (bar_width + spacing)
            y0 = (target_h - bar_h) // 2
            x1 = x0 + bar_width
            y1 = y0 + bar_h
            
            # Clean semi-transparent white bars as requested
            draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 220))
            
        frames.append(img)
    return frames

def generate_audio_waveform_frames(audio_path: str, target_w: int, target_h: int, fps: int = 12, num_frames: int = 100) -> list:
    from PIL import Image as PILImage, ImageDraw
    import numpy as np
    from pydub import AudioSegment
    import math
    
    try:
        audio = AudioSegment.from_file(audio_path)
        audio = audio.set_channels(1)
        duration_ms = len(audio)
        frame_duration_ms = duration_ms / num_frames
        
        num_bars = 40
        bar_width = max(2, int(target_w / (num_bars * 1.5)))
        spacing = max(1, int(bar_width * 0.3))
        total_bars_width = num_bars * bar_width + (num_bars - 1) * spacing
        start_x = (target_w - total_bars_width) // 2
        
        samples = np.array(audio.get_array_of_samples(), dtype=float)
        sample_rate = audio.frame_rate
        total_samples = len(samples)
        
        frames = []
        for f in range(num_frames):
            img = PILImage.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            frame_start_ms = f * frame_duration_ms
            frame_end_ms = (f + 1) * frame_duration_ms
            
            start_idx = int((frame_start_ms / 1000.0) * sample_rate)
            end_idx = int((frame_end_ms / 1000.0) * sample_rate)
            
            start_idx = max(0, min(total_samples - 1, start_idx))
            end_idx = max(start_idx + 1, min(total_samples, end_idx))
            
            frame_samples = samples[start_idx:end_idx]
            if len(frame_samples) == 0:
                frame_samples = np.array([0.0])
                
            frame_rms = np.sqrt(np.mean(frame_samples ** 2)) if len(frame_samples) > 0 else 0.0
            max_possible_val = 24000.0 # visual ceiling for dynamic range
            normalized_rms = min(1.0, frame_rms / max_possible_val)
            
            for i in range(num_bars):
                chunk_len = len(frame_samples) // num_bars
                if chunk_len > 4:
                    chunk = frame_samples[i * chunk_len : (i + 1) * chunk_len]
                    bar_rms = np.sqrt(np.mean(chunk ** 2))
                    bar_norm = min(1.0, bar_rms / max_possible_val)
                else:
                    bar_norm = normalized_rms
                
                bar_norm = 0.03 + 0.97 * bar_norm
                dist_from_center = abs(i - (num_bars / 2)) / (num_bars / 2)
                bell = math.exp(-3.0 * (dist_from_center ** 2))
                
                height_factor = bar_norm * bell * 0.9
                bar_h = max(2, int(target_h * height_factor))
                
                x0 = start_x + i * (bar_width + spacing)
                y0 = (target_h - bar_h) // 2
                x1 = x0 + bar_width
                y1 = y0 + bar_h
                
                draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 220))
                
            frames.append(img)
        print(f"[Video Engine] Generated {len(frames)} audio waveform frames successfully (white)")
        return frames
    except Exception as e:
        print(f"[Video Engine] Error generating real audio waveform: {e}. Falling back...")
        return generate_procedural_waveform_frames(target_w, target_h)

def apply_ken_burns_effect(clip: ImageClip, idx: int, is_music: bool = False, audio_path: pathlib.Path = None, effect_override: str = None, show_waveform: bool = True) -> ImageClip:
    duration = clip.duration
    w, h = clip.size
    import numpy as np
    from PIL import Image as PILImage
    import math

    effect = (effect_override or EFFECT_TYPE or "none").lower()

    # Load waveform.gif or generate dynamic audio waveform frames
    waveform_frames = []
    if show_waveform:
        if audio_path and audio_path.exists():
            target_w = int(0.70 * w)
            target_h = int(180 * (target_w / 1367))
            waveform_frames = generate_audio_waveform_frames(str(audio_path), target_w, target_h, fps=FPS, num_frames=int(duration * FPS))
        else:
            gif_path = pathlib.Path(__file__).parent.parent / "waveform.gif"
            if gif_path.exists():
                try:
                    gif = PILImage.open(str(gif_path))
                    bbox = (293, 384, 1660, 704) # Pre-measured bbox
                    target_w = int(0.70 * w)
                    target_h = int(320 * (target_w / 1367))
                    
                    for frame_idx in range(getattr(gif, "n_frames", 1)):
                        gif.seek(frame_idx)
                        frame_img = gif.convert("RGBA")
                        cropped = frame_img.crop(bbox)
                        resized = cropped.resize((target_w, target_h), PILImage.Resampling.LANCZOS)
                        waveform_frames.append(resized)
                except Exception as e:
                    print(f"[Video Engine] Error loading waveform.gif in Ken Burns: {e}")
            else:
                target_w = int(0.70 * w)
                target_h = int(180 * (target_w / 1367))
                waveform_frames = generate_procedural_waveform_frames(target_w, target_h)

    # Initialize particle simulation based on selected effect
    particles = []
    if effect in ["leaves", "leaf", "snow", "rain", "wind"]:
        import random
        if effect in ["leaves", "leaf"]:
            num_particles = 32
            for _ in range(num_particles):
                particles.append({
                    "x": random.uniform(-40, w),
                    "y": random.uniform(-h, 0),
                    "speed_y": random.uniform(40, 110),
                    "speed_x": random.uniform(15, 60),
                    "sway_freq1": random.uniform(1.2, 2.8),
                    "sway_freq2": random.uniform(2.5, 4.5),
                    "sway_amp1": random.uniform(20, 45),
                    "sway_amp2": random.uniform(8, 20),
                    "rot_start": random.uniform(0, 360),
                    "rot_speed": random.uniform(-120, 120),
                    "size_w": random.uniform(7, 16),
                    "size_h": random.uniform(12, 24),
                    "leaf_type": random.choice(["maple", "oval", "willow"]),
                    "color": random.choice([
                        (238, 160, 34, 180),  # Bright golden amber
                        (212, 110, 24, 180),  # Warm autumn orange
                        (180, 80, 20, 170),   # Deep russet red-brown
                        (245, 185, 45, 190),  # Radiant yellow
                        (160, 95, 30, 165),   # Muted rustic brown
                    ])
                })
        elif effect == "snow":
            num_particles = 55
            for _ in range(num_particles):
                particles.append({
                    "x": random.uniform(0, w),
                    "y": random.uniform(-h, 0),
                    "speed_y": random.uniform(60, 160),
                    "speed_x": random.uniform(-15, 15),
                    "sway_freq": random.uniform(1.0, 2.5),
                    "sway_amp": random.uniform(15, 30),
                    "size": random.uniform(2, 6),
                    "color": (255, 255, 255, random.randint(140, 220))
                })
        elif effect == "rain":
            num_particles = 70
            for _ in range(num_particles):
                particles.append({
                    "x": random.uniform(0, w + 200),
                    "y": random.uniform(-h, 0),
                    "speed_y": random.uniform(800, 1400),
                    "speed_x": random.uniform(-100, -40),
                    "size": random.uniform(1.2, 2.5),
                    "length": random.uniform(25, 55),
                    "color": (200, 220, 235, random.randint(90, 160))
                })
        elif effect == "wind":
            num_particles = 40
            for _ in range(num_particles):
                particles.append({
                    "x": random.uniform(-w, 0),
                    "y": random.uniform(0, h),
                    "speed_x": random.uniform(500, 950),
                    "speed_y": random.uniform(-30, 30),
                    "length": random.uniform(50, 120),
                    "size": random.uniform(1.2, 2.5),
                    "color": (255, 255, 255, random.randint(70, 140))
                })

    def make_frame(get_frame, t):
        frame = get_frame(t)
        img = PILImage.fromarray(frame)
        w, h = img.size
        
        effect_type = idx % 3
        
        if effect in ["static", "none_static"]:
            img_cropped = img
        elif effect_type == 0:
            # Zoom In: 1.0 to 1.15
            factor = 1.0 + 0.15 * (t / duration)
            crop_w = w / factor
            crop_h = h / factor
            left = (w - crop_w) / 2.0
            top = (h - crop_h) / 2.0
            img_cropped = img.resize((w, h), box=(left, top, left + crop_w, top + crop_h), resample=PILImage.Resampling.LANCZOS)
        elif effect_type == 1:
            # Zoom Out: 1.15 down to 1.0
            factor = 1.15 - 0.15 * (t / duration)
            crop_w = w / factor
            crop_h = h / factor
            left = (w - crop_w) / 2.0
            top = (h - crop_h) / 2.0
            img_cropped = img.resize((w, h), box=(left, top, left + crop_w, top + crop_h), resample=PILImage.Resampling.LANCZOS)
        else:
            # Pan left-to-right: zoom to 1.12, then shift x over time
            factor = 1.12
            crop_w = w / factor
            crop_h = h / factor
            max_shift = w - crop_w
            shift_x = max_shift * (t / duration)
            top = (h - crop_h) / 2.0
            img_cropped = img.resize((w, h), box=(shift_x, top, shift_x + crop_w, top + crop_h), resample=PILImage.Resampling.LANCZOS)
            
        # Draw dynamic particle effects
        if particles:
            from PIL import ImageDraw
            img_rgba = img_cropped.convert("RGBA")
            overlay = PILImage.new("RGBA", img_rgba.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            
            for p in particles:
                if effect in ["leaves", "leaf"]:
                    # Multi-harmonic natural wind sway math
                    sway = math.sin(t * p["sway_freq1"]) * p["sway_amp1"] + math.cos(t * p["sway_freq2"]) * p["sway_amp2"]
                    curr_y = (p["y"] + p["speed_y"] * t) % (h + 80) - 40
                    curr_x = (p["x"] + p["speed_x"] * t + sway) % (w + 80) - 40
                    
                    # Tumbling 2D rotation math
                    angle_deg = p["rot_start"] + p["rot_speed"] * t + 30.0 * math.sin(t * 2.0)
                    rad = math.radians(angle_deg)
                    cos_a, sin_a = math.cos(rad), math.sin(rad)
                    
                    sw, sh = p["size_w"], p["size_h"]
                    
                    # Organic leaf contour relative coordinates
                    if p["leaf_type"] == "maple":
                        base_pts = [
                            (0, -sh * 0.6),
                            (sw * 0.3, -sh * 0.2),
                            (sw * 0.6, -sh * 0.35),
                            (sw * 0.4, 0.1 * sh),
                            (sw * 0.5, 0.5 * sh),
                            (0, sh * 0.4),
                            (-sw * 0.5, 0.5 * sh),
                            (-sw * 0.4, 0.1 * sh),
                            (-sw * 0.6, -sh * 0.35),
                            (-sw * 0.3, -sh * 0.2),
                        ]
                    else:
                        base_pts = [
                            (0, -sh * 0.65),
                            (sw * 0.45, -sh * 0.2),
                            (sw * 0.55, sh * 0.25),
                            (sw * 0.15, sh * 0.6),
                            (0, sh * 0.75),
                            (-sw * 0.15, sh * 0.6),
                            (-sw * 0.55, sh * 0.25),
                            (-sw * 0.45, -sh * 0.2),
                        ]
                    
                    # Apply 2D rotation matrix & translation
                    pts = []
                    for dx, dy in base_pts:
                        rx = dx * cos_a - dy * sin_a
                        ry = dx * sin_a + dy * cos_a
                        pts.append((curr_x + rx, curr_y + ry))
                        
                    draw.polygon(pts, fill=p["color"])
                elif effect == "snow":
                    sway = math.sin(t * p["sway_freq"]) * p["sway_amp"]
                    curr_y = (p["y"] + p["speed_y"] * t) % (h + 20) - 10
                    curr_x = (p["x"] + p["speed_x"] * t + sway) % w
                    size = p["size"]
                    draw.ellipse([curr_x, curr_y, curr_x + size, curr_y + size], fill=p["color"])
                elif effect == "rain":
                    curr_y = (p["y"] + p["speed_y"] * t) % (h + 80) - 40
                    curr_x = (p["x"] + p["speed_x"] * t) % (w + 200) - 100
                    draw.line([curr_x, curr_y, curr_x + p["speed_x"] * 0.03, curr_y + p["length"]], fill=p["color"], width=int(p["size"]))
                elif effect == "wind":
                    curr_x = (p["x"] + p["speed_x"] * t) % (w + 300) - 150
                    curr_y = (p["y"] + p["speed_y"] * t) % h
                    draw.line([curr_x, curr_y, curr_x + p["length"], curr_y + p["speed_y"] * 0.1], fill=p["color"], width=int(p["size"]))
                    
            img_cropped = PILImage.alpha_composite(img_rgba, overlay).convert("RGB")
            
        # Draw waveform
        if waveform_frames:
            wave_idx = min(len(waveform_frames) - 1, int(t * FPS))
            wave_img = waveform_frames[wave_idx]
            x_pos = (w - wave_img.width) // 2
            y_pos = (h * 6.6) // 8
            img_rgba = img_cropped.convert("RGBA")
            img_rgba.paste(wave_img, (int(x_pos), int(y_pos)), wave_img)
            img_cropped = img_rgba.convert("RGB")
            
        return np.array(img_cropped)
        
    return clip.fl(make_frame)


def create_video_clip(idx: int, project_dir: pathlib.Path) -> None:
    configure_project_resolution(project_dir)
    frag_path = project_dir / f"text/story_fragments/story_fragment{idx}.txt"
    img_path = project_dir / f"images/image{idx}.jpg"
    audio_wav = project_dir / f"audio/voiceover{idx}.wav"
    audio_mp3 = project_dir / f"audio/voiceover{idx}.mp3"

    is_music = "projects/music" in str(project_dir)
    audio_path = audio_mp3 if audio_mp3.exists() else audio_wav

    if not audio_path.exists():
        try:
            from pydub import AudioSegment
            silence = AudioSegment.silent(duration=3000, frame_rate=44100)
            audio_wav.parent.mkdir(parents=True, exist_ok=True)
            silence.export(str(audio_wav), format="wav")
            audio_path = audio_wav
        except Exception as e:
            print(f"[Engine] Warning: Failed to create silent audio fallback: {e}")
    
    if not is_music and audio_path.exists():
        from pydub import AudioSegment
        audio_seg = AudioSegment.from_file(str(audio_path))
        # Fade in 25ms, fade out 25ms for smooth zero-crossing transitions
        audio_seg = audio_seg.fade_in(25).fade_out(25)
        # Create 120ms silence with matching frame_rate, sample_width, and channels
        silence = AudioSegment.silent(duration=120, frame_rate=audio_seg.frame_rate)
        silence = silence.set_sample_width(audio_seg.sample_width).set_channels(audio_seg.channels)
        padded_audio = silence + audio_seg + silence
        
        temp_audio_path = project_dir / f"audio/processed_voiceover{idx}.wav"
        padded_audio.export(str(temp_audio_path), format="wav")
        audio_clip = AudioFileClip(str(temp_audio_path))
    else:
        audio_clip = AudioFileClip(str(audio_path))

    # Load project_config.json if it exists
    config_path = project_dir / "project_config.json"
    use_watermark = SHOW_WATERMARK
    use_subtitles = True
    use_waveform = True
    effect_override = None
    if config_path.exists():
        try:
            import json
            with open(config_path, "r", encoding="utf-8") as f:
                p_cfg = json.load(f)
                use_watermark = p_cfg.get("use_watermark", use_watermark)
                use_subtitles = p_cfg.get("use_subtitles", use_subtitles)
                use_waveform = p_cfg.get("use_waveform", use_waveform)
                effect_override = p_cfg.get("effect_type") or p_cfg.get("effect")
        except Exception as e:
            print(f"Error loading project_config.json: {e}")

    # Ensure image size matches configured IMAGE_WIDTH and IMAGE_HEIGHT
    if not img_path.exists():
        img_path.parent.mkdir(parents=True, exist_ok=True)
        existing_imgs = sorted(list(img_path.parent.glob("image*.jpg")))
        if existing_imgs:
            img_path = existing_imgs[0]
        else:
            blank = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), color=(20, 20, 20))
            blank.save(str(img_path))

    img = Image.open(str(img_path))
    if img.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
        rgb_im = img.convert("RGB")
        target_w, target_h = IMAGE_WIDTH, IMAGE_HEIGHT
        orig_w, orig_h = rgb_im.size
        target_ratio = target_w / target_h
        orig_ratio = orig_w / orig_h

        if orig_ratio > target_ratio:
            crop_w = int(orig_h * target_ratio)
            crop_h = orig_h
            left = (orig_w - crop_w) // 2
            top = 0
        else:
            crop_w = orig_w
            crop_h = int(orig_w / target_ratio)
            left = 0
            top = (orig_h - crop_h) // 2

        cropped_im = rgb_im.crop((left, top, left + crop_w, top + crop_h))
        final_im = cropped_im.resize((target_w, target_h), Image.Resampling.LANCZOS)
        final_im.save(str(img_path), "JPEG", quality=95, optimize=True)

    image_clip = ImageClip(str(img_path)).set_duration(audio_clip.duration)
    image_clip = apply_ken_burns_effect(image_clip, idx, is_music=True, audio_path=audio_path, effect_override=effect_override, show_waveform=use_waveform)

    # Pick the best available font with Vietnamese support
    font_path = "Arial"
    for possible_font in [
        os.path.expanduser("~/Library/Fonts/NotoSans.ttf"),
        "/tmp/NotoSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]:
        if os.path.exists(possible_font):
            font_path = possible_font
            break

    # Subtitles are now handled 100% by modern Subtitle Engine (subtitle_engine) on final concatenated video.
    # Legacy MoviePy subtitle overlay in fragment video creation is disabled to prevent double subtitle overlays.
    subtitles = []
    sub_word_counts = [len(sub.split()) for sub in subtitles]
    total_words = sum(sub_word_counts)
    total_duration = audio_clip.duration

    sub_fontsize = int(0.027 * IMAGE_HEIGHT)
    PAD = 10                                   # padding around text box (px)
    text_bottom_pad = int(0.05 * IMAGE_HEIGHT) # gap from very bottom edge

    def _make_subtitle_frame(text_line: str, highlight_word_idx: int = -1):
        """Render one subtitle frame: uppercase bold white text with thick black outline, no background box."""
        from PIL import Image as PILImage, ImageDraw, ImageFont as PILFont

        text_line = text_line.upper()

        try:
            # Let's use Arial Bold if available on Mac, otherwise fallback
            bold_font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
            if not os.path.exists(bold_font_path):
                bold_font_path = font_path
            pil_font = PILFont.truetype(bold_font_path, sub_fontsize)
        except Exception:
            pil_font = PILFont.load_default()

        # Build dummy canvas to measure text wrapping
        probe = PILImage.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT))
        dd = ImageDraw.Draw(probe)

        # Word wrap text_line to fit within max_w = int(0.85 * IMAGE_WIDTH)
        max_w = int(0.85 * IMAGE_WIDTH)
        words = text_line.split()
        lines = []
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = dd.textbbox((0, 0), test_line, font=pil_font)
            test_w = bbox[2] - bbox[0]
            if test_w <= max_w:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                    current_line = [word]
                else:
                    lines.append(word)
                    current_line = []
        if current_line:
            lines.append(" ".join(current_line))

        # Measure sizes of all lines and find max width
        line_w_hs = []
        total_text_h = 0
        line_spacing = int(0.015 * IMAGE_HEIGHT)

        for line in lines:
            bbox = dd.textbbox((0, 0), line, font=pil_font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            line_w_hs.append((w, h, bbox[0], bbox[1]))
            total_text_h += h + line_spacing

        total_text_h -= line_spacing

        # Build full-frame transparent canvas
        frame = PILImage.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        # Stroke thickness dynamically based on screen height (thick outline)
        stroke_w = max(2, round(0.003 * IMAGE_HEIGHT))

        # Draw lines centered horizontally, positioned at the center of the bottom half of the screen
        curr_y = (IMAGE_HEIGHT * 3) // 4 - (total_text_h // 2)

        global_word_counter = 0

        for idx_line, line in enumerate(lines):
            lw, lh, offset_x, offset_y = line_w_hs[idx_line]
            line_x = (IMAGE_WIDTH - lw) // 2 - offset_x
            line_y = curr_y - offset_y

            words_in_line = line.split()
            x_offsets = []
            for i in range(len(words_in_line)):
                prefix = " ".join(words_in_line[:i])
                if prefix:
                    prefix_bbox = dd.textbbox((0, 0), prefix + " ", font=pil_font)
                    prefix_w = prefix_bbox[2] - prefix_bbox[0]
                else:
                    prefix_w = 0
                x_offsets.append(prefix_w)

            for i, word in enumerate(words_in_line):
                word_x = line_x + x_offsets[i]
                
                # Check if this word should be highlighted in gold
                if highlight_word_idx == global_word_counter:
                    # Draw a gold glow first
                    glow_w = stroke_w + 3
                    draw.text(
                        (word_x, line_y), 
                        word, 
                        font=pil_font, 
                        fill=(255, 215, 0, 100),
                        stroke_width=glow_w,
                        stroke_fill=(255, 140, 0, 100)
                    )
                    # Draw the gold word on top
                    draw.text(
                        (word_x, line_y), 
                        word, 
                        font=pil_font, 
                        fill=(255, 215, 0, 255),
                        stroke_width=stroke_w,
                        stroke_fill=(0, 0, 0, 255)
                    )
                else:
                    # Draw normal white word
                    draw.text(
                        (word_x, line_y), 
                        word, 
                        font=pil_font, 
                        fill=(255, 255, 255, 255),
                        stroke_width=stroke_w,
                        stroke_fill=(0, 0, 0, 255)
                    )
                
                global_word_counter += 1

            curr_y += lh + line_spacing

        return frame

    @lru_cache(maxsize=1)
    def load_waveform_frames(target_width, target_height):
        frames = []
        gif_path = "waveform.gif"
        if os.path.exists(gif_path):
            try:
                from PIL import Image as PILImage
                gif = PILImage.open(gif_path)
                bbox = (293, 384, 1660, 704) # Pre-measured bbox
                target_w = int(0.70 * target_width)
                target_h = int(320 * (target_w / 1367))
                
                for frame_idx in range(getattr(gif, "n_frames", 1)):
                    gif.seek(frame_idx)
                    frame_img = gif.convert("RGBA")
                    cropped = frame_img.crop(bbox)
                    resized = cropped.resize((target_w, target_h), PILImage.Resampling.LANCZOS)
                    frames.append(resized)
                print(f"[Video Engine] Loaded {len(frames)} frames from waveform.gif (resized to {target_w}x{target_h})")
            except Exception as e:
                print(f"[Video Engine] Error loading waveform.gif: {e}")
        else:
            target_w = int(0.70 * target_width)
            target_h = int(180 * (target_w / 1367))
            frames = generate_procedural_waveform_frames(target_w, target_h)
        return frames

    txt_clips = []
    start_offset = 0.0 if is_music else 0.5
    active_speech_duration = total_duration if is_music else (total_duration - 1.0)
    current_time = start_offset
    waveform_frames = load_waveform_frames(IMAGE_WIDTH, IMAGE_HEIGHT)

    for i, sub in enumerate(subtitles):
        if total_words > 0:
            sub_duration = (sub_word_counts[i] / total_words) * active_speech_duration
        else:
            sub_duration = active_speech_duration

        sub_duration = max(0.5, sub_duration)
        if i == len(subtitles) - 1:
            sub_duration = max(sub_duration, (start_offset + active_speech_duration) - current_time)

        words_list = sub.split()
        num_words = len(words_list)

        # Render static subtitle frame once (no karaoke highlighting)
        img_frame = _make_subtitle_frame(sub, highlight_word_idx=-1)
        import numpy as np
        img_array = np.array(img_frame)
        sub_clip = (ImageClip(img_array)
                    .set_duration(sub_duration)
                    .set_start(current_time))

        txt_clips.append(sub_clip)
        current_time += sub_duration

    watermark_path = get_processed_watermark()
    extra_clips = []
    if watermark_path and use_watermark:
        watermark_clip = ImageClip(watermark_path).set_duration(audio_clip.duration)
        extra_clips.append(watermark_clip)

    video = CompositeVideoClip([image_clip.set_audio(audio_clip)] + extra_clips + txt_clips)
    out = project_dir / f"videos/video{idx}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    video.write_videofile(str(out), fps=FPS, codec="libx264", audio_codec="pcm_s16le", logger=None)


def concat_clips(project_dir: pathlib.Path, start_idx: int = None, end_idx: int = None) -> List[VideoFileClip]:
    files = sorted(project_dir.glob("videos/video*.mp4"), key=lambda p: int(p.stem[5:]))
    if start_idx is not None and end_idx is not None:
        files = [f for f in files if start_idx <= int(f.stem[5:]) <= end_idx]
    return [VideoFileClip(str(f)) for f in files]


def make_final_video(project_name: str, project_dir: pathlib.Path, start_idx: int = None, end_idx: int = None) -> None:
    configure_project_resolution(project_dir)
    clips = concat_clips(project_dir, start_idx, end_idx)
    is_music = "projects/music" in str(project_dir)
    if is_music:
        clips = [c.crossfadein(1.0).crossfadeout(1.0) for c in clips]
        final = concatenate_videoclips(clips, padding=-1, method="compose")
    else:
        final = concatenate_videoclips(clips, method="compose")
        # Build seamless master voiceover audio using Pydub to prevent any inter-clip demuxing click/pop sounds
        try:
            from pydub import AudioSegment
            files = sorted(project_dir.glob("videos/video*.mp4"), key=lambda p: int(p.stem[5:]))
            if start_idx is not None and end_idx is not None:
                files = [f for f in files if start_idx <= int(f.stem[5:]) <= end_idx]
            
            clean_voice_seg = AudioSegment.empty()
            for f in files:
                idx = int(f.stem[5:])
                proc_wav = project_dir / f"audio/processed_voiceover{idx}.wav"
                raw_wav = project_dir / f"audio/voiceover{idx}.wav"
                raw_mp3 = project_dir / f"audio/voiceover{idx}.mp3"
                target_a = proc_wav if proc_wav.exists() else (raw_wav if raw_wav.exists() else raw_mp3)
                if target_a.exists():
                    seg = AudioSegment.from_file(str(target_a))
                    clean_voice_seg += seg
            
            if len(clean_voice_seg) > 0:
                clean_voice_path = project_dir / "audio/full_voiceover_concat.wav"
                clean_voice_seg.export(str(clean_voice_path), format="wav")
                voice_audio_clip = AudioFileClip(str(clean_voice_path))
                
                if BG_MUSIC:
                    bg_path = BG_MUSIC_PATH
                    if "chuong-1" in str(project_dir):
                        bg_path = BG_MUSIC_PATH.parent / "01 - Tà Áo Lụa Trắng.mp3"
                    elif "chuong-2" in str(project_dir):
                        bg_path = BG_MUSIC_PATH.parent / "02 - Ly Cà Phê Vỉa Hè.mp3"
                    elif "chuong-3" in str(project_dir):
                        bg_path = BG_MUSIC_PATH.parent / "03 - Truyện Kiều Bìa Rách.mp3"

                    if bg_path.exists():
                        bg = AudioFileClip(str(bg_path)).audio_loop(duration=voice_audio_clip.duration)
                        bg = volumex(bg, MUSIC_VOLUME)
                        final = final.set_audio(CompositeAudioClip([voice_audio_clip, bg]))
                    else:
                        final = final.set_audio(voice_audio_clip)
                else:
                    final = final.set_audio(voice_audio_clip)
                print(f"[VideoEngine] Successfully built clean seamless audio master ({clean_voice_path.name})")
        except Exception as ex:
            print(f"[VideoEngine Warning] Failed to generate clean Pydub master audio: {ex}")

    clean_name = pathlib.Path(project_name).name
    out = project_dir / f"{clean_name}.mp4"
    temp_audio = project_dir / "temp_audio.m4a"
    final.write_videofile(
        str(out), fps=FPS, codec="libx264", audio_codec="aac",
        audio_bitrate="192k", temp_audiofile=str(temp_audio), remove_temp=True
    )
    try:
        final.close()
        for c in clips:
            c.close()
    except Exception:
        pass

    # Check if subtitle engine burn is enabled in project_config.json
    config_file = project_dir / "project_config.json"
    use_sub = True
    preset_id = "viral-bold-yellow"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                use_sub = cfg.get("use_subtitles", True)
                preset_id = cfg.get("subtitle_preset", "viral-bold-yellow")
        except Exception:
            pass

    if use_sub:
        try:
            from subtitle_engine.processor import SubtitleProcessor
            story_file = project_dir / "story.txt"
            story_text = story_file.read_text(encoding="utf-8") if story_file.exists() else None
            sp = SubtitleProcessor(preset_path_or_id=preset_id)
            temp_out = project_dir / f"{project_name}_subtitled.mp4"
            frag_rng = (start_idx, end_idx) if (start_idx is not None and end_idx is not None) else None
            sp.burn_subtitles_to_video(input_video_path=out, output_video_path=temp_out, transcript=story_text, fragment_range=frag_rng)
            if temp_out.exists():
                shutil.move(str(temp_out), str(out))
        except Exception as err:
            print(f"[VideoEngine] Subtitle Engine burn notice: {err}")

    # Generate Thumbnail Cover Image & Embed Metadata + Cover Art into MP4
    generate_thumbnail_and_embed_metadata(project_dir, project_name)

    server_final = project_dir / "final.mp4"
    if out.exists():
        try:
            shutil.copy(str(out), str(server_final))
            print(f"[VideoEngine] Copied final video to {server_final.name}")
        except Exception as ex:
            print(f"[VideoEngine] Error copying to final.mp4: {ex}")


def generate_thumbnail_and_embed_metadata(project_dir: pathlib.Path, project_name: str) -> None:
    """Generates a high-quality video cover thumbnail and embeds metadata + cover art into the final MP4 file."""
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont as PILFont
        import subprocess

        clean_name = pathlib.Path(project_name).name
        video_path = project_dir / f"{clean_name}.mp4"
        if not video_path.exists():
            video_path = project_dir / "final.mp4"
        if not video_path.exists():
            video_path = project_dir / f"{project_dir.parent.name}_{project_dir.name}.mp4"

        # 1. Pick base image
        img_candidates = list((project_dir / "images").glob("*.jpg")) + list((project_dir / "images").glob("*.png"))
        if not img_candidates:
            return
        
        base_img_path = sorted(img_candidates)[0]
        base_img = PILImage.open(base_img_path).convert("RGBA").resize((IMAGE_WIDTH, IMAGE_HEIGHT), PILImage.Resampling.LANCZOS)
        
        # 2. Add full-canvas dark gradient overlay
        overlay = PILImage.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT), (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        for y in range(IMAGE_HEIGHT):
            alpha = int(130 + (y / IMAGE_HEIGHT) * 50)
            draw_ov.line([(0, y), (IMAGE_WIDTH, y)], fill=(0, 0, 0, alpha))
            
        thumb_img = PILImage.alpha_composite(base_img, overlay).convert("RGB")
        draw = ImageDraw.Draw(thumb_img)

        # 3. Typography & Styling according to 3-line requirement:
        # Line 1: short_title (Yellow Pill Badge)
        # Line 2: title (Main bold title, stripping episode_label)
        # Line 3: first sentence of content
        short_title = ""
        title_text = ""
        content_text = ""
        episode_label = ""

        item_file = project_dir / "item.json"
        if item_file.exists():
            try:
                with open(item_file, "r", encoding="utf-8") as f:
                    idata = json.load(f)
                short_title = str(idata.get("short_title", "") or "").strip()
                title_text = str(idata.get("title", "") or "").strip()
                episode_label = str(idata.get("episode_label", "") or "").strip()
                content_text = str(idata.get("content", "") or "").strip()
            except Exception:
                pass

        config_file = project_dir / "project_config.json"
        if config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not short_title: short_title = str(cfg.get("short_title", "") or "").strip()
                if not title_text: title_text = str(cfg.get("title", "") or "").strip()
                if not episode_label: episode_label = str(cfg.get("episode_label", "") or "").strip()
                if not content_text: content_text = str(cfg.get("content", "") or "").strip()
            except Exception:
                pass

        if not short_title or not title_text or not content_text:
            story_id = project_dir.parent.name
            chap_id = project_dir.name
            data_dir = pathlib.Path(__file__).parent.parent / "data"
            for jpath in data_dir.rglob("*.json"):
                try:
                    with open(jpath, "r", encoding="utf-8") as f:
                        jdata = json.load(f)
                    if jdata.get("slug") in (chap_id, story_id, project_name):
                        if not short_title: short_title = str(jdata.get("short_title", "") or "").strip()
                        if not title_text: title_text = str(jdata.get("title", "") or "").strip()
                        if not episode_label: episode_label = str(jdata.get("episode_label", "") or "").strip()
                        if not content_text: content_text = str(jdata.get("content", "") or "").strip()
                        break
                except Exception:
                    pass

        # Clean Line 1: Short title badge text (strip episode label, replace hyphens/underscores)
        if episode_label:
            short_title = re.sub(r'^' + re.escape(episode_label) + r'[\s:\-]*', '', short_title, flags=re.IGNORECASE).strip()
        short_title = re.sub(r'^(?:Tập\s*\d+|Ep\.?\s*\d+|Chapter\s*\d+|Phần\s*\d+)[\s:\-]*', '', short_title, flags=re.IGNORECASE).strip()
        if ('-' in short_title or '_' in short_title) and ' ' not in short_title:
            short_title = short_title.replace("-", " ").replace("_", " ").strip()
        if not short_title:
            short_title = project_name.replace("dao_ly_", "").replace("dao-ly-", "").replace("-", " ").replace("_", " ").strip().title()

        # Clean Line 2: Strip episode_label (e.g. "Tập 01:", "Tập 1 -", "Ep 01:", etc.) from title_text
        if episode_label:
            title_text = re.sub(r'^' + re.escape(episode_label) + r'[\s:\-]*', '', title_text, flags=re.IGNORECASE).strip()
        title_text = re.sub(r'^(?:Tập\s*\d+|Ep\.?\s*\d+|Chapter\s*\d+|Phần\s*\d+|Cơ-sở\s*\d+)[\s:\-]*', '', title_text, flags=re.IGNORECASE).strip()
        if ('-' in title_text or '_' in title_text) and ' ' not in title_text:
            title_text = title_text.replace("-", " ").replace("_", " ").strip()
        if not title_text:
            title_text = short_title

        # Line 3: Extract first sentence of content
        first_sentence = ""
        raw_story = content_text.strip()
        if not raw_story:
            story_file = project_dir / "story.txt"
            if story_file.exists():
                try:
                    raw_story = story_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

        if raw_story:
            try:
                clean_story = re.sub(r'\[.*?\]', '', raw_story).strip()
                paragraphs = [p.strip() for p in clean_story.splitlines() if p.strip()]
                for p in paragraphs:
                    p_clean = p.replace("...", "___ELLIPSIS___")
                    sents = [s.replace("___ELLIPSIS___", "...").strip() for s in re.split(r'[.!?\n]', p_clean) if s.strip()]
                    if sents:
                        first_sentence = sents[0]
                        if len(sents) > 1 and len(first_sentence) < 30:
                            first_sentence += " " + sents[1]
                        break
            except Exception:
                pass
                
        if not first_sentence:
            first_sentence = "Bài học cuộc sống sâu sắc và ý nghĩa"

        font_path_title = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        font_path_sub = "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"
        font_path_tag = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        
        try:
            font_size_title = int(0.032 * IMAGE_HEIGHT) if IMAGE_WIDTH > IMAGE_HEIGHT else int(0.024 * IMAGE_HEIGHT)
            font_size_sub = int(0.020 * IMAGE_HEIGHT) if IMAGE_WIDTH > IMAGE_HEIGHT else int(0.016 * IMAGE_HEIGHT)
            font_size_tag = int(0.020 * IMAGE_HEIGHT) if IMAGE_WIDTH > IMAGE_HEIGHT else int(0.015 * IMAGE_HEIGHT)
            pil_font_title = PILFont.truetype(font_path_title, font_size_title)
            pil_font_sub = PILFont.truetype(font_path_sub, font_size_sub)
            pil_font_tag = PILFont.truetype(font_path_tag, font_size_tag)
        except Exception:
            pil_font_title = PILFont.load_default()
            pil_font_sub = pil_font_title
            pil_font_tag = pil_font_title

        # Line 1: Short Title Pill Badge (Truncate/wrap if too long to prevent horizontal spill)
        disp_short = short_title.upper()
        if len(disp_short) > 30:
            disp_short = disp_short[:27] + "..."
        tag_text = f"  {disp_short}  "
        tag_bbox = draw.textbbox((0, 0), tag_text, font=pil_font_tag)
        tag_w = tag_bbox[2] - tag_bbox[0]
        tag_h = tag_bbox[3] - tag_bbox[1]

        # Clamp tag width to max 85% of image width
        max_tag_w = int(0.85 * IMAGE_WIDTH)
        if tag_w > max_tag_w:
            tag_w = max_tag_w

        tag_x = (IMAGE_WIDTH - tag_w) // 2
        tag_y = int(IMAGE_HEIGHT * 0.18)

        draw.rounded_rectangle([tag_x - 18, tag_y - 10, tag_x + tag_w + 18, tag_y + tag_h + 10], radius=22, fill=(245, 158, 11, 240))
        draw.text((tag_x, tag_y), tag_text, font=pil_font_tag, fill=(15, 23, 42))

        # Line 2: Title (Wrap lines & cap at max 3 lines to prevent vertical spill)
        max_w = int(0.85 * IMAGE_WIDTH)
        words = title_text.split()
        lines, curr = [], []
        for w in words:
            bbox = draw.textbbox((0, 0), " ".join(curr + [w]), font=pil_font_title)
            if bbox[2] - bbox[0] <= max_w:
                curr.append(w)
            else:
                if curr: lines.append(" ".join(curr))
                curr = [w]
        if curr: lines.append(" ".join(curr))

        if len(lines) > 3:
            lines = lines[:3]
            if not lines[-1].endswith("..."):
                lines[-1] = lines[-1].rstrip(".!?") + "..."

        curr_y = tag_y + tag_h + 35
        stroke_w = max(4, int(0.004 * IMAGE_HEIGHT))
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=pil_font_title)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
            lx = (IMAGE_WIDTH - lw) // 2
            
            draw.text((lx, curr_y), line, font=pil_font_title, fill=(255, 255, 255), stroke_width=stroke_w, stroke_fill=(15, 23, 42))
            curr_y += lh + 14

        # Line 3: Full content text (Wrap lines & cap at max 3 lines)
        quote_text = f"“{first_sentence}”"

        q_words = quote_text.split()
        q_lines, curr_q = [], []
        max_q_w = int(0.82 * IMAGE_WIDTH)
        for w in q_words:
            bbox = draw.textbbox((0, 0), " ".join(curr_q + [w]), font=pil_font_sub)
            if bbox[2] - bbox[0] <= max_q_w:
                curr_q.append(w)
            else:
                if curr_q: q_lines.append(" ".join(curr_q))
                curr_q = [w]
        if curr_q: q_lines.append(" ".join(curr_q))

        if len(q_lines) > 3:
            q_lines = q_lines[:3]
            if not q_lines[-1].endswith("..."):
                q_lines[-1] = q_lines[-1].rstrip(".!?") + "..."

        curr_y += 18
        max_bottom = int(IMAGE_HEIGHT * 0.90)
        for qline in q_lines:
            bbox = draw.textbbox((0, 0), qline, font=pil_font_sub)
            qw = bbox[2] - bbox[0]
            qh = bbox[3] - bbox[1]
            if curr_y + qh > max_bottom:
                break
            qx = (IMAGE_WIDTH - qw) // 2
            draw.text((qx, curr_y), qline, font=pil_font_sub, fill=(252, 211, 77), stroke_width=2, stroke_fill=(0, 0, 0))
            curr_y += qh + 10

        thumb_jpg = project_dir / "thumbnail.jpg"
        thumb_img.save(thumb_jpg, "JPEG", quality=95)
        print(f"[VideoEngine] Saved thumbnail cover image at {thumb_jpg}")

        # 4. Dynamic Metadata Extraction & Embedding + Cover Art Image into MP4 via FFmpeg
        channel_name = ""
        item_file = project_dir / "item.json"
        
        if item_file.exists():
            try:
                with open(item_file, "r", encoding="utf-8") as f:
                    idata = json.load(f)
                channel_name = idata.get("channel", "")
                if not title_text or title_text == short_title:
                    title_text = idata.get("title", title_text)
            except Exception:
                pass

        if not channel_name and config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                channel_name = cfg.get("channel", "")
            except Exception:
                pass

        if not channel_name:
            p_str = str(project_dir).lower()
            if "playnet" in p_str or "en" in p_str or "knowledge" in p_str:
                channel_name = "@playnet.zone-en"
            else:
                channel_name = "@tramgactrithuc"

        meta_title = title_text or project_name
        meta_artist = channel_name
        
        clean_slug = project_dir.name.replace("-", "").replace("_", "")
        if "en" in channel_name or "playnet" in str(project_dir).lower():
            meta_comment = f"#playnet #knowledge #{clean_slug} #educational #science"
            meta_copyright = f"© 2026 {channel_name.lstrip('@')}"
        else:
            meta_comment = f"#tramgactrithuc #trithuc #{clean_slug} #khampha #tuduy #cuocsong #khoahoc"
            meta_copyright = f"© 2026 {channel_name.lstrip('@')}"
            
        meta_encoder = "Taka Media Engine v1.0"
        
        tagged_mp4 = project_dir / f"{project_name}_tagged.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(thumb_jpg),
            "-map", "0",
            "-map", "1",
            "-c", "copy",
            "-disposition:v:1", "attached_pic",
            "-metadata", f"title={meta_title}",
            "-metadata", f"artist={meta_artist}",
            "-metadata", f"comment={meta_comment}",
            "-metadata", f"copyright={meta_copyright}",
            "-metadata", f"encoder={meta_encoder}",
            str(tagged_mp4)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and tagged_mp4.exists() and tagged_mp4.stat().st_size > 0:
            shutil.move(str(tagged_mp4), str(video_path))
            print(f"[VideoEngine] Successfully embedded Metadata & Thumbnail Cover Art into {video_path}")

    except Exception as e:
        print(f"[VideoEngine] Warning: Metadata & Thumbnail embedding failed: {e}")


def transcribe_audio_file(audio_path: pathlib.Path) -> List[Dict[str, any]]:
    """
    Transcribe audio/music file. First tries WhisperX local pipeline,
    then OpenAI Whisper API, and falls back to transformers pipeline.
    """
    # 1. Try WhisperX local transcription
    try:
        import whisperx
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"

        print(f"[Transcribe] Using local WhisperX model (small) on device: {device}...")
        model = whisperx.load_model("small", device=device, compute_type=compute_type, language="vi")
        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(audio, batch_size=16)

        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": seg.get("text", "").strip()
            })

        if segments:
            print(f"[Transcribe] WhisperX returned {len(segments)} segments.")
            return group_whisper_chunks(segments)
    except Exception as wx_err:
        print(f"[Transcribe] Local WhisperX skipped/failed: {wx_err}. Trying OpenAI API...")

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_TOKEN")
    if not api_key:
        api_key = config.get("OPENAI", "API_KEY", fallback=None) or config.get("IMAGE_PROMPT", "OPENAI_TOKEN", fallback=None)
        
    if api_key:
        try:
            print(f"[Transcribe] Using OpenAI Whisper API for {audio_path.name}...")
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            with open(audio_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json",
                    language="vi"
                )
            segments = []
            if hasattr(transcript, "segments") and transcript.segments:
                for seg in transcript.segments:
                    segments.append({
                        "start": seg.get("start") if isinstance(seg, dict) else seg.start,
                        "end": seg.get("end") if isinstance(seg, dict) else seg.end,
                        "text": seg.get("text") if isinstance(seg, dict) else seg.text
                    })
            elif isinstance(transcript, dict) and "segments" in transcript:
                for seg in transcript["segments"]:
                    segments.append({
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg["text"]
                    })
            else:
                text = transcript.text if hasattr(transcript, "text") else transcript.get("text", "")
                segments = [{"start": 0.0, "end": 10.0, "text": text}]
            
            print(f"[Transcribe] OpenAI API returned {len(segments)} segments.")
            return group_whisper_chunks(segments)
        except Exception as e:
            print(f"[Transcribe] OpenAI Whisper API failed: {e}. Falling back to local...")

    # Local Whisper fallback
    try:
        print(f"[Transcribe] Initializing local transformers Whisper pipeline...")
        import torch
        from transformers import pipeline
        
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
            
        whisper_model = config.get("AUDIO", "LOCAL_WHISPER_MODEL", fallback="openai/whisper-tiny")
        print(f"[Transcribe] Using local model: {whisper_model} on device: {device}")
        
        pipe = pipeline(
            "automatic-speech-recognition",
            model=whisper_model,
            device=device
        )
        
        print(f"[Transcribe] Transcribing local file {audio_path}...")
        result = pipe(str(audio_path), return_timestamps=True, generate_kwargs={"language": "vietnamese", "task": "transcribe"})
        
        segments = []
        chunks = result.get("chunks", [])
        for chunk in chunks:
            ts = chunk.get("timestamp")
            if ts and len(ts) == 2:
                start, end = ts
                if start is None: start = 0.0
                if end is None: end = start + 5.0
                segments.append({
                    "start": float(start),
                    "end": float(end),
                    "text": chunk.get("text", "").strip()
                })
            else:
                segments.append({
                    "start": 0.0,
                    "end": 5.0,
                    "text": chunk.get("text", "").strip()
                })
        
        if not segments and result.get("text"):
            segments = [{"start": 0.0, "end": 10.0, "text": result["text"]}]
            
        print(f"[Transcribe] Local Whisper returned {len(segments)} segments.")
        return group_whisper_chunks(segments)
    except Exception as e:
        print(f"[Transcribe] Local transcription failed: {e}")
        raise RuntimeError(f"Transcription failed: {e}")


def group_whisper_chunks(chunks: List[dict], min_duration: float = 4.0, max_duration: float = 12.0) -> List[dict]:
    """Group short transcribed segments together to form longer scenes."""
    merged = []
    curr_text = []
    curr_start = None
    curr_end = None
    
    for chunk in chunks:
        start = chunk.get("start", 0.0)
        end = chunk.get("end", start + 3.0)
        text = chunk.get("text", "").strip()
        if not text or text == "[music]":
            continue
            
        if curr_start is None:
            curr_start = start
            
        curr_text.append(text)
        curr_end = end
        
        duration = curr_end - curr_start
        if duration >= min_duration:
            merged.append({
                "start": curr_start,
                "end": curr_end,
                "text": " ".join(curr_text)
            })
            curr_start = None
            curr_text = []
            curr_end = None
            
    if curr_text and curr_start is not None:
        merged.append({
            "start": curr_start,
            "end": curr_end if curr_end is not None else curr_start + 4.0,
            "text": " ".join(curr_text)
        })
        
    # If merged is completely empty (e.g. no voice, just music), provide at least one segment
    if not merged:
        merged.append({
            "start": 0.0,
            "end": 10.0,
            "text": "Beautiful music visualization"
        })
        
    return merged


def slice_music_file(audio_path: pathlib.Path, segments: List[dict], output_dir: pathlib.Path) -> None:
    """Slice the original audio file into segments to guide MoviePy clip durations."""
    from pydub import AudioSegment
    print(f"[Slice] Loading original audio {audio_path}...")
    audio = AudioSegment.from_file(str(audio_path))
    audio_duration_ms = len(audio)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    for idx, seg in enumerate(segments):
        start_ms = int(seg["start"] * 1000)
        end_ms = int(seg["end"] * 1000)
        
        start_ms = max(0, start_ms)
        end_ms = min(audio_duration_ms, end_ms)
        if end_ms <= start_ms:
            end_ms = start_ms + 1000
            
        seg_audio = audio[start_ms:end_ms]
        out_path = output_dir / f"voiceover{idx}.mp3"
        seg_audio.export(str(out_path), format="mp3")
        print(f"[Slice] Exported fragment {idx} audio: {start_ms}ms to {end_ms}ms -> {out_path.name}")


def make_final_music_video(project_name: str, project_dir: pathlib.Path, original_audio_path: pathlib.Path, segments: List[dict] = None) -> None:
    """Concatenate video clips at their exact start times matching segments to prevent drift, then overlay original audio."""
    import json
    
    # Load segments from JSON if not provided
    if segments is None:
        segments_json_path = project_dir / "segments.json"
        if segments_json_path.exists():
            try:
                with open(segments_json_path, "r", encoding="utf-8") as f:
                    segments = json.load(f)
            except Exception as e:
                print(f"[Final Video] Error loading segments.json: {e}")

    music_clip = AudioFileClip(str(original_audio_path))
    total_duration = music_clip.duration
    
    if segments:
        print(f"[Final Video] Assembling video using {len(segments)} segments for exact timing alignment...")
        composed_clips = []
        for idx, seg in enumerate(segments):
            clip_path = project_dir / f"videos/video{idx}.mp4"
            if clip_path.exists():
                try:
                    clip = VideoFileClip(str(clip_path))
                    # Position clip on timeline
                    clip = clip.set_start(seg["start"])
                    # Limit duration to prevent overlapping
                    duration = seg["end"] - seg["start"]
                    clip = clip.set_duration(duration)
                    composed_clips.append(clip)
                    print(f"[Final Video] Positioned clip {idx} at {seg['start']:.2f}s for {duration:.2f}s")
                except Exception as e:
                    print(f"[Final Video] Error processing clip {idx}: {e}")
        
        # Create final composite video clip with black background of total_duration
        final = CompositeVideoClip(composed_clips, size=(IMAGE_WIDTH, IMAGE_HEIGHT)).set_duration(total_duration)
    else:
        print("[Final Video] Warning: No segments found. Falling back to simple concatenation...")
        clips = concat_clips(project_dir)
        final = concatenate_videoclips(clips, method="compose")
        
    final = final.set_audio(music_clip)
    
    clean_name = pathlib.Path(project_name).name
    out = project_dir / f"{clean_name}.mp4"
    final.write_videofile(str(out), fps=FPS, codec="libx264")

    server_final = project_dir / "final.mp4"
    if out.exists():
        try:
            shutil.copy(str(out), str(server_final))
            print(f"[VideoEngine] Copied final music video to {server_final.name}")
        except Exception as ex:
            print(f"[VideoEngine] Error copying to final.mp4: {ex}")


def dub_video_with_voice(input_video_path: pathlib.Path, voice_audio_path: pathlib.Path, output_video_path: pathlib.Path, mix_mode: str = "replace", bg_volume: float = 0.15) -> pathlib.Path:
    """
    Dubs a video clip with a generated voiceover audio track.
    mix_mode: 'replace' (replaces original audio completely) or 'mix' (ducks original audio and overlays voiceover).
    """
    import subprocess
    input_video_path = pathlib.Path(input_video_path)
    voice_audio_path = pathlib.Path(voice_audio_path)
    output_video_path = pathlib.Path(output_video_path)
    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    if mix_mode == "mix":
        filter_complex = f"[0:a]volume={bg_volume}[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first[aout]"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video_path),
            "-i", str(voice_audio_path),
            "-filter_complex", filter_complex,
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(output_video_path)
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video_path),
            "-i", str(voice_audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_video_path)
        ]
    
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not output_video_path.exists() or output_video_path.stat().st_size == 0:
        try:
            from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
            video_clip = VideoFileClip(str(input_video_path))
            voice_clip = AudioFileClip(str(voice_audio_path))
            if mix_mode == "mix" and video_clip.audio is not None:
                bg_clip = video_clip.audio.volumex(bg_volume)
                final_audio = CompositeAudioClip([bg_clip, voice_clip])
            else:
                final_audio = voice_clip
            
            final_video = video_clip.set_audio(final_audio)
            final_video.write_videofile(str(output_video_path), fps=video_clip.fps or 30, codec="libx264", audio_codec="aac")
            try:
                video_clip.close()
                voice_clip.close()
            except Exception:
                pass
        except ImportError:
            err_log = res.stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(f"FFmpeg dubbing failed (returncode {res.returncode}): {err_log}")

    return output_video_path


