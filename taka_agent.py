import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
import configparser
import requests
import websockets
from websockets.exceptions import ConnectionClosed

# Resolve base directory (where taka_agent.py is located)
AGENT_DIR = pathlib.Path(__file__).resolve().parent

# Load config
_CONFIG_PATH = AGENT_DIR / "config.ini"
config = configparser.ConfigParser()
config.read(_CONFIG_PATH, encoding="utf-8")

SERVER_URL = config.get("TAKA_AGENT", "SERVER_URL", fallback="http://localhost:8080")
WORKSPACE_ID = config.get("TAKA_AGENT", "WORKSPACE_ID", fallback="default_workspace")

# Resolve tools and checkpoints relative to AGENT_DIR
omnivoice_subpath = config.get("TAKA_AGENT", "OMNIVOICE_PATH", fallback="tools/OmniVoice")
OMNIVOICE_PATH = AGENT_DIR / omnivoice_subpath

omnivoice_model_subpath = config.get("TAKA_AGENT", "OMNIVOICE_MODEL_DIR", fallback="tools/OmniVoice/checkpoints")
OMNIVOICE_MODEL_DIR = AGENT_DIR / omnivoice_model_subpath

OMNIVOICE_REPO = config.get("OMNIVOICE", "REPO_URL", fallback="https://github.com/k2-fsa/OmniVoice")
OMNIVOICE_LANG = config.get("OMNIVOICE", "LANGUAGE", fallback="vi")

# Import the core video/NLP engine
from core import video_engine

# Resolve secure WebSocket URL from Server URL
if "localhost" not in SERVER_URL and "127.0.0.1" not in SERVER_URL:
    ws_base = SERVER_URL.replace("http://", "wss://").replace("https://", "wss://")
else:
    ws_base = SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")
ws_url = f"{ws_base}/v1/system/agent/ws?workspace_id={WORKSPACE_ID}"

active_websocket = None

async def check_environment() -> dict:
    """Check availability of local CUDA/MPS, Ollama, and OmniVoice setup."""
    # 1. Check PyTorch CUDA / MPS
    cuda_available = False
    mps_available = False
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        mps_available = hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    except ImportError:
        pass

    # 2. Check local Ollama
    ollama_active = False
    try:
        res = requests.get("http://localhost:11434/api/tags", timeout=1.0)
        if res.status_code == 200:
            ollama_active = True
    except Exception:
        pass

    # 3. Check OmniVoice directory & entrypoints
    omnivoice_installed = OMNIVOICE_PATH.exists() and (OMNIVOICE_PATH / "requirements.txt").exists()

    return {
        "cuda_available": cuda_available,
        "mps_available": mps_available,
        "ollama_active": ollama_active,
        "omnivoice_installed": omnivoice_installed,
        "agent_version": "0.1.0"
    }

async def setup_omnivoice():
    """Download/Clone and set up OmniVoice repo and checkpoints."""
    if not OMNIVOICE_PATH.exists():
        print(f"[Agent] Cloning OmniVoice from {OMNIVOICE_REPO}...")
        OMNIVOICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Run git clone
        cmd = ["git", "clone", OMNIVOICE_REPO, str(OMNIVOICE_PATH)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to clone OmniVoice: {stderr.decode()}")
        print("[Agent] Cloned OmniVoice successfully.")

    # Install dependencies inside the environment
    req_path = OMNIVOICE_PATH / "requirements.txt"
    if req_path.exists():
        print("[Agent] Installing OmniVoice requirements...")
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_path)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f"[Agent] Warning: requirement installation reported issues: {stderr.decode()}")

    # Setup directories for checkpoints
    OMNIVOICE_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[Agent] OmniVoice environment configured at {OMNIVOICE_PATH}.")

def tts_omnivoice(text: str, out: pathlib.Path, voice_config: dict = None) -> None:
    """Generate audio using OmniVoice cloned repo."""
    out.parent.mkdir(parents=True, exist_ok=True)
    
    scripts = (
        list(OMNIVOICE_PATH.glob("**/infer.py")) +
        list(OMNIVOICE_PATH.glob("**/tts.py")) +
        list(OMNIVOICE_PATH.glob("**/generate.py"))
    )
    
    if not scripts:
        print("[Agent] OmniVoice tts/generate/infer script not found. Falling back to edge-tts.")
        asyncio.run(video_engine.tts_edge(text, out))
        return
        
    script_path = scripts[0]
    cmd = [
        sys.executable, str(script_path),
        "--model", "k2-fsa/OmniVoice",
        "--text", text,
        "--language", OMNIVOICE_LANG,
        "--output", str(out)
    ]

    
    # Add voice cloning or voice design flags if voice_config matches
    if voice_config:
        mode = voice_config.get("omnivoice_mode")
        if mode == "clone" and voice_config.get("ref_audio_path"):
            cmd += ["--ref_audio", voice_config["ref_audio_path"]]
            if voice_config.get("ref_text"):
                cmd += ["--ref_text", voice_config["ref_text"]]
        elif mode == "design" and voice_config.get("voice_instruct"):
            cmd += ["--instruct", voice_config["voice_instruct"]]

    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"[Agent] Generated OmniVoice audio at {out}")
        if res.stdout:
            print(f"[Agent] OmniVoice stdout: {res.stdout}")
        if res.stderr:
            print(f"[Agent] OmniVoice stderr: {res.stderr}")
    except subprocess.CalledProcessError as e:
        print(f"[Agent] OmniVoice script failed: {e.stderr}. Falling back to edge-tts.")
        asyncio.run(video_engine.tts_edge(text, out))

async def generate_voiceover(text: str, out: pathlib.Path, voice_config: dict = None) -> None:
    """Routing helper that generates voiceover according to provider settings."""
    default_config = {
        "provider": config.get("AUDIO", "TTS_PROVIDER", fallback="edge"),
        "omnivoice_mode": config.get("AUDIO", "OMNIVOICE_MODE", fallback="auto"),
        "ref_audio_path": config.get("AUDIO", "OMNIVOICE_REF_AUDIO", fallback=None),
        "ref_text": config.get("AUDIO", "OMNIVOICE_REF_TEXT", fallback=None),
        "voice_instruct": config.get("AUDIO", "OMNIVOICE_INSTRUCT", fallback=None),
        "voice_id": config.get("AUDIO", "VOICE", fallback=None)
    }
    
    # Merge custom voice_config over defaults
    merged_config = default_config.copy()
    if voice_config:
        for k, v in voice_config.items():
            if v is not None and v != "":
                merged_config[k] = v
                
    provider = merged_config.get("provider", "edge").lower()
    
    if provider == "omnivoice":
        await asyncio.to_thread(tts_omnivoice, text, out, merged_config)
    elif provider == "kokoro":
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.KOKORO_VOICE_ID
        if custom_voice:
            video_engine.KOKORO_VOICE_ID = custom_voice
        try:
            await asyncio.to_thread(video_engine.tts_kokoro, text, out)
        finally:
            video_engine.KOKORO_VOICE_ID = orig_voice
    elif provider == "elevenlabs":
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.ELEVENLABS_VOICE_ID
        if custom_voice:
            video_engine.ELEVENLABS_VOICE_ID = custom_voice
        try:
            await asyncio.to_thread(video_engine.tts_elevenlabs, text, out)
        finally:
            video_engine.ELEVENLABS_VOICE_ID = orig_voice
    else:  # edge-tts
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.VOICE
        if custom_voice:
            video_engine.VOICE = custom_voice
        try:
            await video_engine.tts_edge(text, out)
        finally:
            video_engine.VOICE = orig_voice

async def run_pipeline_task(project_name: str, project_path_str: str, websocket, voice_config: dict = None, art_style: str = None, use_watermark: bool = True, use_subtitles: bool = True):
    """Executes the full Taka-Tales pipeline and reports progress in real time."""
    try:
        # Resolve project folder relative to AGENT_DIR/projects to support remote server
        path_obj = pathlib.Path(project_path_str)
        story_id = path_obj.parent.name
        chapter_id = path_obj.name
        project_dir = AGENT_DIR / "projects" / story_id / chapter_id
        
        # Save project_config.json
        config_data = {
            "use_watermark": use_watermark,
            "use_subtitles": use_subtitles,
            "use_whisper": False
        }
        with open(project_dir / "project_config.json", "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        
        # 1. Setup folders and clean old folders completely to ensure no leftover files
        for sub in ("text", "audio", "images", "videos"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)
            
        for folder in ("text/story_sentences", "text/story_fragments", "text/image_prompts", "videos"):
            fpath = project_dir / folder
            if fpath.exists():
                shutil.rmtree(fpath)
            fpath.mkdir(parents=True, exist_ok=True)

        for folder in ("audio", "images"):
            (project_dir / folder).mkdir(parents=True, exist_ok=True)

        final_video = project_dir / f"{project_name}.mp4"
        server_final = project_dir / "final.mp4"
        if final_video.exists():
            final_video.unlink()
        if server_final.exists():
            server_final.unlink()

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "processing_sentences",
            "current_fragment": 0,
            "total_fragments": 0
        }))

        # 2. Text preprocessing and splitting (Always regenerate to select slice correctly)
        story_file = project_dir / "story.txt"
        num_sentences = video_engine.load_and_split_to_sentences(story_file)
        num_frags = video_engine.sentences_to_fragments(num_sentences, project_dir)

        # 2.5 Slice fragments to match requested Start Index and Limit
        start_frag = 0
        limit_frag = 0
        if voice_config:
            start_frag = int(voice_config.get("start_fragment", 0))
            limit_frag = int(voice_config.get("limit_fragments", 0))

        if start_frag > 0 or limit_frag > 0:
            frag_dir = project_dir / "text/story_fragments"
            all_frags = []
            for i in range(num_frags):
                frag_file = frag_dir / f"story_fragment{i}.txt"
                if frag_file.exists():
                    all_frags.append(video_engine._read_text(frag_file))
            
            start_idx = max(0, min(start_frag, len(all_frags) - 1)) if all_frags else 0
            end_idx = len(all_frags)
            if limit_frag > 0:
                end_idx = min(start_idx + limit_frag, len(all_frags))
            
            selected_frags = all_frags[start_idx:end_idx]
            
            # Clear and rewrite fragments numbered 0 to N-1
            shutil.rmtree(frag_dir)
            frag_dir.mkdir(parents=True, exist_ok=True)
            for idx, frag in enumerate(selected_frags):
                video_engine._write_text(frag_dir / f"story_fragment{idx}.txt", frag)
            
            num_frags = len(selected_frags)

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "generating_prompts",
            "current_fragment": 0,
            "total_fragments": num_frags
        }))

        # 3. Generate prompts (Ollama)
        await asyncio.to_thread(video_engine._unload_sd)
        await asyncio.to_thread(video_engine._reload_ollama)
        prompt_dir = project_dir / "text/image_prompts"
        frag_dir = project_dir / "text/story_fragments"
        for idx in range(num_frags):
            prompt_file = prompt_dir / f"image_prompt{idx}.txt"
            if not prompt_file.exists():
                prompt = await asyncio.to_thread(
                    video_engine.build_image_prompt,
                    video_engine._read_text(frag_dir / f"story_fragment{idx}.txt"),
                    art_style
                )
                video_engine._write_text(prompt_file, prompt)
                
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "generating_prompts",
                "current_fragment": idx + 1,
                "total_fragments": num_frags
            }))

        # 4. Generate audio (OmniVoice)
        for idx in range(num_frags):
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "generating_audio",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "tts"}
            }))
            
            wav = project_dir / f"audio/voiceover{idx}.wav"
            mp3 = project_dir / f"audio/voiceover{idx}.mp3"
            if not (wav.exists() or mp3.exists()):
                frag = video_engine._read_text(frag_dir / f"story_fragment{idx}.txt")
                await generate_voiceover(frag, wav, voice_config)

        # 5. Generate images
        await asyncio.to_thread(video_engine._unload_ollama)
        await asyncio.to_thread(video_engine._reload_sd)
        for idx in range(num_frags):
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "generating_images",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "image"}
            }))
            
            img = project_dir / f"images/image{idx}.jpg"
            if not img.exists():
                await asyncio.to_thread(video_engine.generate_image, idx, project_dir, art_style)

        # 6. Render clips (MoviePy)
        for idx in range(num_frags):
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "compiling_clips",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "clip"}
            }))
            
            out_clip = project_dir / f"videos/video{idx}.mp4"
            if not out_clip.exists():
                await asyncio.to_thread(video_engine.create_video_clip, idx, project_dir)

        # 7. Final Concatenation and music assembly
        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "assembling_final_video",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        }))
        
        final_video = project_dir / f"{project_name}.mp4"
        server_final = project_dir / "final.mp4"
        
        await asyncio.to_thread(video_engine.make_final_video, project_name, project_dir)
        if final_video.exists():
            shutil.copy(str(final_video), str(server_final))

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "completed",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        }))
    except Exception as e:
        print(f"[Agent] Pipeline task failed: {e}")
        try:
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "failed",
                "error": str(e),
                "current_fragment": 0,
                "total_fragments": 0
            }))
        except Exception as send_err:
            print(f"[Agent] Failed to send error status: {send_err}")


async def run_music_pipeline_task(project_name: str, project_path_str: str, websocket, voice_config: dict = None, art_style: str = None, use_watermark: bool = False, use_subtitles: bool = False, use_whisper: bool = False):
    """Executes the music-to-video pipeline by transcribing audio and generating images/subtitles."""
    try:
        # Resolve project folder relative to AGENT_DIR/projects to support remote server
        path_obj = pathlib.Path(project_path_str)
        story_id = path_obj.parent.name
        chapter_id = path_obj.name
        project_dir = AGENT_DIR / "projects" / story_id / chapter_id
        
        # Save project_config.json
        config_data = {
            "use_watermark": use_watermark,
            "use_subtitles": use_subtitles,
            "use_whisper": use_whisper
        }
        with open(project_dir / "project_config.json", "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        
        # 1. Setup folders and clean old folders completely
        for sub in ("text", "audio", "images", "videos"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)
            
        for folder in ("text/story_fragments", "text/image_prompts", "videos"):
            fpath = project_dir / folder
            if fpath.exists():
                shutil.rmtree(fpath)
            fpath.mkdir(parents=True, exist_ok=True)

        final_video = project_dir / f"{project_name}.mp4"
        server_final = project_dir / "final.mp4"
        if final_video.exists():
            final_video.unlink()
        if server_final.exists():
            server_final.unlink()

        # Find the uploaded music file in project directory
        music_files = list(project_dir.glob("music.*"))
        if not music_files:
            raise FileNotFoundError("No music file found (expecting music.*) in project directory.")
        music_file_path = music_files[0]

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "transcribing_audio",
            "current_fragment": 0,
            "total_fragments": 0
        }))

        # 2. Try to find synced lyrics (LRC) first
        import syncedlyrics
        import re
        from moviepy.editor import AudioFileClip
        
        music_clip = AudioFileClip(str(music_file_path))
        total_duration = music_clip.duration
        music_clip.close()
        
        segments = []
        if use_whisper:
            print("[Agent] Whisper mode enabled. Transcribing audio...")
            raw_segments = await asyncio.to_thread(video_engine.transcribe_audio_file, music_file_path)
            
            story_source_file = project_dir / "story.txt"
            lyrics_lines = []
            if story_source_file.exists():
                try:
                    with open(story_source_file, "r", encoding="utf-8") as f:
                        lyrics_lines = [l.strip() for l in f.readlines() if l.strip()]
                except Exception:
                    pass
            if not lyrics_lines:
                lyrics_lines = ["a beautiful traditional Vietnamese countryside scene"]
            
            L = len(lyrics_lines)
            S = len(raw_segments)
            if S > 0:
                for idx in range(S):
                    start_idx = int(idx * L / S)
                    end_idx = int((idx + 1) * L / S)
                    segment_text = " / ".join(lyrics_lines[start_idx:end_idx])
                    raw_segments[idx]["text"] = segment_text
                
                # Fill timeline gaps to prevent black screens
                for i in range(len(raw_segments) - 1):
                    raw_segments[i]["end"] = raw_segments[i+1]["start"]
                raw_segments[-1]["end"] = total_duration
                segments = raw_segments
                print(f"[Agent] Whisper segmentation complete: {len(segments)} segments.")

        lrc_text = None
        if not segments:
            search_query = project_name.replace("-", " ").replace("_", " ")
            print(f"[Agent] Searching synced lyrics for query: '{search_query}'")
            try:
                lrc_text = syncedlyrics.search(search_query)
            except Exception as e:
                print(f"[Agent] syncedlyrics search failed: {e}")

        if not segments and lrc_text:
            print("[Agent] Synced lyrics (LRC) found! Parsing...")
            # Parse LRC text
            lines = lrc_text.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$", line)
                if match:
                    mins = int(match.group(1))
                    secs = float(match.group(2))
                    text = match.group(3).strip()
                    # Skip metadata headers
                    if not text and not segments:
                        continue
                    start_time = mins * 60 + secs
                    segments.append({"start": start_time, "text": text})
            
            # Clean empty segments
            segments = [s for s in segments if s["text"]]
            
            # Set end times
            for i in range(len(segments) - 1):
                segments[i]["end"] = segments[i+1]["start"]
            if segments:
                segments[-1]["end"] = total_duration
        
        if segments:
            print(f"[Agent] Successfully parsed {len(segments)} synced lyrics segments.")
        else:
            print("[Agent] No synced lyrics (LRC) found. Subtitles will be omitted.")
            story_source_file = project_dir / "story.txt"
            lyrics_lines = []
            if story_source_file.exists():
                try:
                    with open(story_source_file, "r", encoding="utf-8") as f:
                        lyrics_lines = [l.strip() for l in f.readlines() if l.strip()]
                except Exception:
                    pass
            
            if not lyrics_lines:
                lyrics_lines = [f"Slide {i+1}" for i in range(20)]
                
            num_slides = len(lyrics_lines)
            slide_duration = total_duration / num_slides
            for idx, line in enumerate(lyrics_lines):
                segments.append({
                    "start": idx * slide_duration,
                    "end": (idx + 1) * slide_duration,
                    "text": "" # Empty text to omit subtitle
                })
                
        num_frags = len(segments)

        # Write story.txt (the full lyrics)
        full_lyrics = "\n".join([seg["text"] for seg in segments])
        video_engine._write_text(project_dir / "story.txt", full_lyrics)

        # Save segments metadata to JSON file for final video alignment
        import json
        segments_json_path = project_dir / "segments.json"
        with open(segments_json_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

        # Write fragments
        frag_dir = project_dir / "text/story_fragments"
        for idx, seg in enumerate(segments):
            video_engine._write_text(frag_dir / f"story_fragment{idx}.txt", seg["text"])

        # Slice the audio file into fragment-level audio files
        await asyncio.to_thread(
            video_engine.slice_music_file,
            music_file_path,
            segments,
            project_dir / "audio"
        )

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "generating_prompts",
            "current_fragment": 0,
            "total_fragments": num_frags
        }))

        # 3. Generate prompts (Ollama)
        await asyncio.to_thread(video_engine._unload_sd)
        await asyncio.to_thread(video_engine._reload_ollama)
        prompt_dir = project_dir / "text/image_prompts"
        for idx in range(num_frags):
            prompt_file = prompt_dir / f"image_prompt{idx}.txt"
            if not prompt_file.exists():
                prompt = await asyncio.to_thread(
                    video_engine.build_image_prompt,
                    video_engine._read_text(frag_dir / f"story_fragment{idx}.txt"),
                    art_style
                )
                video_engine._write_text(prompt_file, prompt)
                
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "generating_prompts",
                "current_fragment": idx + 1,
                "total_fragments": num_frags
            }))

        # 4. Generate images
        await asyncio.to_thread(video_engine._unload_ollama)
        await asyncio.to_thread(video_engine._reload_sd)
        for idx in range(num_frags):
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "generating_images",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "image"}
            }))
            
            img = project_dir / f"images/image{idx}.jpg"
            if not img.exists():
                await asyncio.to_thread(video_engine.generate_image, idx, project_dir, art_style)

        # 5. Render clips (MoviePy)
        for idx in range(num_frags):
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "compiling_clips",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "clip"}
            }))
            
            out_clip = project_dir / f"videos/video{idx}.mp4"
            if not out_clip.exists():
                await asyncio.to_thread(video_engine.create_video_clip, idx, project_dir)

        # 6. Final Concatenation and music assembly
        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "assembling_final_video",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        }))
        
        await asyncio.to_thread(video_engine.make_final_music_video, project_name, project_dir, music_file_path, segments)
        if final_video.exists():
            shutil.copy(str(final_video), str(server_final))

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "completed",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        }))
    except Exception as e:
        print(f"[Agent] Music pipeline task failed: {e}")
        try:
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "failed",
                "error": str(e),
                "current_fragment": 0,
                "total_fragments": 0
            }))
        except Exception as send_err:
            print(f"[Agent] Failed to send error status: {send_err}")


async def main():
    global active_websocket
    print(f"[Agent] Starting Taka-Agent. Connecting to server {ws_url}...")
    
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=60, ping_timeout=60) as websocket:
                print("[Agent] Connected to Taka Server successfully.")
                active_websocket = websocket
                
                # Check environment
                status = await check_environment()
                
                # Auto setup OmniVoice if not present
                if not status["omnivoice_installed"]:
                    print("[Agent] OmniVoice not installed. Commencing automatic setup...")
                    try:
                        await setup_omnivoice()
                        status = await check_environment()
                    except Exception as se:
                        print(f"[Agent] Failed to automatically setup OmniVoice: {se}")
                
                # Send status update
                await websocket.send(json.dumps({
                    "type": "status_update",
                    "payload": status
                }))

                async for message_str in websocket:
                    try:
                        message = json.loads(message_str)
                    except json.JSONDecodeError:
                        continue

                    msg_type = message.get("type")
                    payload = message.get("payload", {})

                    if msg_type == "run_pipeline":
                        project_name = payload.get("project_name")
                        project_path_str = payload.get("project_path")
                        voice_config = payload.get("voice_config")
                        pipeline_type = payload.get("pipeline_type", "story")
                        art_style = payload.get("art_style")
                        use_watermark = payload.get("use_watermark", True)
                        use_subtitles = payload.get("use_subtitles", True)
                        use_whisper = payload.get("use_whisper", False)
                        
                        # Process project asynchronously in the background
                        if pipeline_type == "music":
                            asyncio.create_task(run_music_pipeline_task(project_name, project_path_str, websocket, voice_config, art_style, use_watermark, use_subtitles, use_whisper))
                        else:
                            asyncio.create_task(run_pipeline_task(project_name, project_path_str, websocket, voice_config, art_style, use_watermark, use_subtitles))
                        
        except ConnectionClosed:
            print("[Agent] Connection to server closed. Retrying in 5 seconds...")
            active_websocket = None
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[Agent] Connection error: {e}. Retrying in 5 seconds...")
            active_websocket = None
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Agent] Stopped by user.")
