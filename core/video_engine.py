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
import gc
import io
import json
import multiprocessing
import os
import pathlib
import re
import shutil
import time

# Configure ImageMagick path for MoviePy
os.environ["IMAGEMAGICK_BINARY"] = "/opt/homebrew/bin/convert"
from datetime import datetime
from typing import (Dict, List, Tuple)

import edge_tts
import openai
import psutil
import requests
from concurrent.futures import ProcessPoolExecutor
from fake_useragent import UserAgent
from functools import lru_cache
from keybert import KeyBERT
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
from nltk.tokenize import sent_tokenize, word_tokenize
import nltk
for nltk_res in ('punkt', 'punkt_tab'):
    try:
        nltk.data.find(f'tokenizers/{nltk_res}')
    except LookupError:
        try:
            nltk.download(nltk_res, quiet=True)
        except Exception:
            pass
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
FRAGMENT_LENGTH: int = int(config["TEXT_FRAGMENT"]["FRAGMENT_LENGTH"])

# AUDIO
TTS_PROVIDER: str = config["AUDIO"]["TTS_PROVIDER"]
ELEVENLABS_VOICE_ID: str = config["AUDIO"]["ELEVENLABS_VOICE_ID"]
KOKORO_VOICE_ID: str = config["AUDIO"]["KOKORO_VOICE_ID"]
KOKORO_URL: str = config["AUDIO"]["KOKORO_URL"]
VOICE: str = config["AUDIO"]["VOICE"]
BG_MUSIC: bool = config["AUDIO"].getboolean("BG_MUSIC")
BG_MUSIC_PATH: pathlib.Path = pathlib.Path(__file__).parent.parent / config["AUDIO"]["BG_MUSIC_PATH"]
MUSIC_VOLUME: float = float(config["AUDIO"]["MUSIC_VOLUME"])

# IMAGE PROMPTS
IMAGE_PROMPT_PROVIDER: str = config["IMAGE_PROMPT"]["IMAGE_PROMPT_PROVIDER"]
OLLAMA_MODEL: str = config["IMAGE_PROMPT"]["OLLAMA_MODEL"]

# STABLE DIFFUSION
POSITIVE_PREFIX: str = config["STABLE_DIFFUSION"]["positive_prompt_prefix"]
POSITIVE_SUFFIX: str = config["STABLE_DIFFUSION"]["positive_prompt_suffix"]
ART_STYLES: Dict[str, str] = {
    "watercolor": "hand-painted watercolor style, soft edges, ink washes, detailed textures, classical literary book illustration of old Vietnam, warm nostalgic colors, featuring traditional Vietnamese clothing, Vietnamese village house, Vietnamese landscape, masterpiece",
    "dong_ho": "traditional Vietnamese Dong Ho folk painting style, woodblock print texture, bold hand-drawn ink outlines, natural pigments on aged textured Dzo paper, depicting traditional Vietnamese rural life, Vietnamese peasants, Vietnamese countryside context, folk art masterpiece",
    "son_mai": "detailed Vietnamese lacquer painting style, gold leaf accents, cinnabar red highlights, polished dark lacquer surface, organic textures, featuring traditional Vietnamese motifs and Vietnamese village scenery, cultural masterpiece",
    "woodblock": "rustic monochrome Vietnamese folk woodblock print style, black ink printing on aged yellowish parchment paper, bold textured carving lines, traditional Vietnamese country life depiction, hand-carved block printing aesthetic",
    "thuy_mac": "classical East Asian ink wash painting style, sumi-e aesthetic, elegant black ink strokes on white Xuan paper, subtle grey washes, misty atmosphere, monochrome, zen art style, poetic nostalgic look, masterpiece",
    "thuy_mac_blackwhite": "strict monochrome black and white ink wash painting, traditional sumi-e style, pure black ink and white paper contrast, no color, minimalist, zen atmosphere, expressive brushstrokes, negative space, dramatic silhouette, masterpiece"
}
NEGATIVE_PROMPT: str = config["STABLE_DIFFUSION"]["negative_prompt"]
USE_SD_API: str = config["STABLE_DIFFUSION"]["USE_SD_VIA_API"]
SD_URL: str = config["STABLE_DIFFUSION"]["SD_URL"]
SEED: int = int(config["STABLE_DIFFUSION"]["seed"])
IMAGE_WIDTH: int = int(config["STABLE_DIFFUSION"]["image_width"])
IMAGE_HEIGHT: int = int(config["STABLE_DIFFUSION"]["image_height"])
POLLINATIONS_MODEL: str = config["STABLE_DIFFUSION"].get("POLLINATIONS_MODEL", fallback="flux")
EFFECT_TYPE: str = config["STABLE_DIFFUSION"].get("EFFECT_TYPE", fallback="none")

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
    # 1. Remove HTML tags like <i>, <b>, <p>, <br> entirely
    text = re.sub(r'<[^>]+>', '', text)
    
    # 2. Clean markdown links: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # 2.5 Normalize website/domain names for correct pronunciation
    text = re.sub(r'\bgog\.zone\b', 'gờ o gờ chấm zôn', text, flags=re.IGNORECASE)
    
    # 3. Standard character mapping
    # NOTE: Do NOT strip Vietnamese tone marks (é, ê, ô, ơ, ư, etc.)
    mapping = {
        ">": "",
        "<": "",
        "=": "",
        "#": "",
        "..": ".",
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
        ".....": ".",
        "....": ".",
        "...": ", ",
        "\u2026": ", ",
        "\n\n\n": "\n",
        "\n\n": "\n",
    }
    for k, v in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
        text = text.replace(k, v)
    return text


def load_and_split_to_sentences(story_path: pathlib.Path) -> int:
    """
    Split *story.txt* into sentences and write into
    ``text/story_sentences/story_sentence{idx}.txt``.
    """
    raw = story_path.read_text(encoding="utf-8")
    raw = clean_text(raw)
    sentences = sent_tokenize(raw)

    punctuation_list = [',', ';', ':']
    new_sentences: List[str] = []
    frag_len = 3*FRAGMENT_LENGTH
    for sent in sentences:
        words = sent.split()
        if len(words) <= FRAGMENT_LENGTH:
            new_sentences.append(sent)
        else:
            part = []
            for word in words:
                part.append(word)
                if word[-1] in punctuation_list and len(part) > frag_len:
                    new_sentences.append(' '.join(part))
                    part = []
            if part:
                new_sentences.append(" ".join(part))

    for idx, sent in enumerate(new_sentences):
        _write_text(story_path.parent / f"text/story_sentences/story_sentence{idx}.txt", sent)

    _log(f"Created {len(new_sentences)} sentence files.")
    return len(new_sentences)


def sentences_to_fragments(num_sentences: int, project_dir: pathlib.Path) -> int:
    """
    Group consecutive sentences into fragments of at least *FRAGMENT_LENGTH* words.
    """
    fragments: List[str] = []
    current_words: List[str] = []

    for i in range(num_sentences):
        sentence = _read_text(project_dir / f"text/story_sentences/story_sentence{i}.txt")
        current_words.extend(sentence.split())
        if len(current_words) > FRAGMENT_LENGTH:
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
    

def build_image_prompt(fragment: str, art_style: str = None) -> str:
    style_suffix = ART_STYLES.get(art_style, POSITIVE_SUFFIX) if art_style else POSITIVE_SUFFIX
    prompt_instruction = (
        "You are an expert prompt writer for Stable-Diffusion-XL. "
        f"Style context: {style_suffix}. "
        "Describe the scene in a single sentence, max 20 words. "
        "Do NOT include any explanations or quotes. "
        "CRITICAL: The output prompt must be in English. Do NOT use any Vietnamese or Chinese characters in the prompt. "
        "Do NOT include any text, words, or letters on the image itself. "
        "Force a rich, traditional Vietnamese context by using keywords like 'non la' (conical hat), 'ao dai', 'bamboo trees', 'rustic Vietnamese village scenery', or 'misty Vietnamese countryside' where appropriate to describe the visual scene."
    )

    if IMAGE_PROMPT_PROVIDER == "chatgpt":
        try:
            response = openai.Completion.create(
                engine="text-davinci-003",
                prompt=f"{prompt_instruction}\n{fragment}",
                max_tokens=40,
                temperature=0.9,
            )
            prompt = response.choices[0].text.strip()
                
        except Exception as e:
            _log(f"ChatGPT failed: {e}. Using KeyBERT fallback.")
            prompt = _keywords_fallback(fragment)

    elif IMAGE_PROMPT_PROVIDER == "ollama":
        try:
            resp: ChatResponse = chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": f"{prompt_instruction}\n{fragment}"}],
            )
            prompt = resp["message"]["content"].strip()
            _log(prompt)
                
        except Exception as e:
            _log(f"Ollama failed: {e}. Using KeyBERT fallback.")
            prompt = _keywords_fallback(fragment)

    else:
        prompt = _keywords_fallback(fragment)

    if any(x in prompt.lower() for x in ("i cannot", "?")):
        prompt = _keywords_fallback(fragment)

    if CHAR_DESC:
        prompt = _find_characters(fragment) + prompt

    return prompt


# ---------- Image Generation ---------- 
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


def _sd_api_payload(prompt: str) -> dict:
    return {
        "prompt": f"{prompt}",
        "negative_prompt": NEGATIVE_PROMPT,
        "steps": 20,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "seed": SEED,
        "guidance_scale": 4.0,
        "sampler_index": "Euler a",
    }


def generate_image(idx: int, project_dir: pathlib.Path, art_style: str = None) -> None:
    prompt_path = project_dir / f"text/image_prompts/image_prompt{idx}.txt"
    image_path = project_dir / f"images/image{idx}.jpg"
    if image_path.exists():
        return

    prompt = _read_text(prompt_path)
    style_suffix = ART_STYLES.get(art_style, POSITIVE_SUFFIX) if art_style else POSITIVE_SUFFIX
    prompt = f"{POSITIVE_PREFIX} {prompt} {style_suffix}"
    
    _log(f"{idx} Loaded Prompt: {prompt}")
    do_it = True
    wait_time = 10
    
    while(do_it):
        try:
            if USE_SD_API == "yes":
                url = SD_URL
                payload = _sd_api_payload(prompt)
                option_payload = {
                    "sd_model_checkpoint": "aamXLAnimeMix_v10.safetensors",
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
                url = (
                    f"https://image.pollinations.ai/prompt/{encoded_prompt}"
                    f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&nologo=true&model={POLLINATIONS_MODEL}&enhance=false"
                    f"&seed={time.time()}&negative=nsfw"
                )
                response = requests.get(url, headers={"User-Agent": ua.random}, timeout=60)
                if response.status_code == 200:
                    image = io.BytesIO(response.content)
                    img = Image.open(image)
                    img.save(image_path)
                else:
                    raise requests.exceptions.HTTPError(f'Failed to download. Status: {response.status_code}')    
                
            do_it = False
            
        except Exception as e:   
            _log(f"Exception!!! {idx} \n{e} \nWaiting {wait_time}s...")
            time.sleep(wait_time)


# ---------- TTS ----------
async def tts_edge(text: str, out: pathlib.Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    com = edge_tts.Communicate(text, VOICE)
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

def apply_ken_burns_effect(clip: ImageClip, idx: int, is_music: bool = False, audio_path: pathlib.Path = None) -> ImageClip:
    duration = clip.duration
    w, h = clip.size
    import numpy as np
    from PIL import Image as PILImage

    # Load waveform.gif or generate dynamic audio waveform frames
    waveform_frames = []
    if is_music or True:
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
                # Fallback to dynamic white waveform frames
                target_w = int(0.70 * w)
                target_h = int(180 * (target_w / 1367))
                waveform_frames = generate_procedural_waveform_frames(target_w, target_h)

    # Initialize falling particles if EFFECT_TYPE is enabled
    particles = []
    if EFFECT_TYPE in ["leaves", "snow", "rain"]:
        import random
        num_particles = 15 if EFFECT_TYPE == "leaves" else (40 if EFFECT_TYPE == "snow" else 60)
        for _ in range(num_particles):
            particles.append({
                "x": random.uniform(0, w),
                "y": random.uniform(-h, 0),
                "speed_y": random.uniform(60, 150) if EFFECT_TYPE == "leaves" else (random.uniform(90, 220) if EFFECT_TYPE == "snow" else random.uniform(700, 1300)),
                "speed_x": random.uniform(-30, 30) if EFFECT_TYPE != "rain" else random.uniform(-60, -20),
                "size": random.uniform(6, 16) if EFFECT_TYPE == "leaves" else (random.uniform(2, 5) if EFFECT_TYPE == "snow" else random.uniform(1.5, 2.5)),
                "length": random.uniform(20, 40) if EFFECT_TYPE == "rain" else 0,
                "color": random.choice([
                    (210, 105, 30, 140),  # Chocolate brown leaf
                    (244, 164, 96, 140),  # Sandy brown leaf
                    (205, 133, 63, 140),  # Peru orange-brown leaf
                    (139, 69, 19, 140),   # Saddle brown leaf
                ]) if EFFECT_TYPE == "leaves" else ((255, 255, 255, 200) if EFFECT_TYPE == "snow" else (180, 200, 215, 120))
            })

    def make_frame(get_frame, t):
        frame = get_frame(t)
        img = PILImage.fromarray(frame)
        w, h = img.size
        
        effect_type = idx % 3
        
        if effect_type == 0:
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
            
        # Draw falling particles
        if particles:
            from PIL import ImageDraw
            img_rgba = img_cropped.convert("RGBA")
            overlay = PILImage.new("RGBA", img_rgba.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            
            for p in particles:
                curr_y = (p["y"] + p["speed_y"] * t) % (h + 50) - 25
                curr_x = (p["x"] + p["speed_x"] * t) % w
                
                if EFFECT_TYPE == "leaves":
                    size = p["size"]
                    pts = [
                        (curr_x, curr_y - size),
                        (curr_x + size/2, curr_y),
                        (curr_x, curr_y + size),
                        (curr_x - size/2, curr_y)
                    ]
                    draw.polygon(pts, fill=p["color"])
                elif EFFECT_TYPE == "snow":
                    size = p["size"]
                    draw.ellipse([curr_x, curr_y, curr_x + size, curr_y + size], fill=p["color"])
                elif EFFECT_TYPE == "rain":
                    draw.line([curr_x, curr_y, curr_x + p["speed_x"] * 0.02, curr_y + p["length"]], fill=p["color"], width=int(p["size"]))
                    
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
    frag_path = project_dir / f"text/story_fragments/story_fragment{idx}.txt"
    img_path = project_dir / f"images/image{idx}.jpg"
    audio_wav = project_dir / f"audio/voiceover{idx}.wav"
    audio_mp3 = project_dir / f"audio/voiceover{idx}.mp3"

    is_music = "projects/music" in str(project_dir)
    audio_path = audio_mp3 if audio_mp3.exists() else audio_wav
    
    if not is_music:
        from pydub import AudioSegment
        audio_seg = AudioSegment.from_file(str(audio_path))
        # Fade in 50ms, fade out 50ms
        audio_seg = audio_seg.fade_in(50).fade_out(50)
        # Create 0.5s silence segment matching same parameters
        silence = AudioSegment.silent(duration=500, frame_rate=audio_seg.frame_rate)
        # Concatenate silence + audio + silence
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
    if config_path.exists():
        try:
            import json
            with open(config_path, "r", encoding="utf-8") as f:
                p_cfg = json.load(f)
                use_watermark = p_cfg.get("use_watermark", use_watermark)
                use_subtitles = p_cfg.get("use_subtitles", use_subtitles)
        except Exception as e:
            print(f"Error loading project_config.json: {e}")

    # Ensure image size matches configured IMAGE_WIDTH and IMAGE_HEIGHT
    img = Image.open(str(img_path))
    if img.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
        img_resized = img.resize((IMAGE_WIDTH, IMAGE_HEIGHT), Image.Resampling.LANCZOS)
        img_resized.save(str(img_path))

    image_clip = ImageClip(str(img_path)).set_duration(audio_clip.duration)
    # Always apply waveform (is_music=True) with correct audio_path to display actual audio levels
    image_clip = apply_ken_burns_effect(image_clip, idx, is_music=True, audio_path=audio_path)

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

    subtitles = split_text_to_subtitles(_read_text(frag_path)) if use_subtitles else []
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
    video.write_videofile(str(out), fps=FPS, codec="libx264", audio_codec="aac", logger=None)


def concat_clips(project_dir: pathlib.Path, start_idx: int = None, end_idx: int = None) -> List[VideoFileClip]:
    files = sorted(project_dir.glob("videos/video*.mp4"), key=lambda p: int(p.stem[5:]))
    if start_idx is not None and end_idx is not None:
        files = [f for f in files if start_idx <= int(f.stem[5:]) < end_idx]
    return [VideoFileClip(str(f)) for f in files]


def make_final_video(project_name: str, project_dir: pathlib.Path, start_idx: int = None, end_idx: int = None) -> None:
    clips = concat_clips(project_dir, start_idx, end_idx)
    clips = [c.crossfadein(1.0).crossfadeout(1.0) for c in clips]
    final = concatenate_videoclips(clips, padding=-1, method="compose")

    if BG_MUSIC:
        bg_path = BG_MUSIC_PATH
        if "chuong-1" in str(project_dir):
            bg_path = BG_MUSIC_PATH.parent / "01 - Tà Áo Lụa Trắng.mp3"
        elif "chuong-2" in str(project_dir):
            bg_path = BG_MUSIC_PATH.parent / "02 - Ly Cà Phê Vỉa Hè.mp3"
        elif "chuong-3" in str(project_dir):
            bg_path = BG_MUSIC_PATH.parent / "03 - Truyện Kiều Bìa Rách.mp3"

        if bg_path.exists():
            bg = AudioFileClip(str(bg_path)).audio_loop(duration=final.duration)
            bg = volumex(bg, MUSIC_VOLUME)
            final = final.set_audio(CompositeAudioClip([final.audio, bg]))
        else:
            print(f"[Warning] Background music path not found: {bg_path}")

    out = project_dir / f"{project_name}.mp4"
    final.write_videofile(str(out), fps=FPS, codec="libx264", audio_codec="aac")


def transcribe_audio_file(audio_path: pathlib.Path) -> List[Dict[str, any]]:
    """
    Transcribe audio/music file. First tries OpenAI Whisper API,
    then falls back to Hugging Face transformers pipeline.
    """
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
    
    out = project_dir / f"{project_name}.mp4"
    final.write_videofile(str(out), fps=FPS, codec="libx264")
