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

from typing import Dict, Optional, List

import resource
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target_limit = min(10240, hard) if hard != resource.RLIM_INFINITY else 10240
    resource.setrlimit(resource.RLIMIT_NOFILE, (target_limit, hard))
    print(f"[Agent] Raised file descriptor limit: {resource.getrlimit(resource.RLIMIT_NOFILE)}")
except Exception as e:
    print(f"[Agent] Warning: Could not raise ulimit: {e}")

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

# Resolve base directory (where taka_agent.py is located)
AGENT_DIR = pathlib.Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
AGENT_DATA_DIR = pathlib.Path.home() / ".taka-agent"
AGENT_PROJECTS_DIR = AGENT_DATA_DIR / "projects"
AGENT_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
AGENT_VOICES_DIR = AGENT_DATA_DIR / "voices"
AGENT_VOICES_DIR.mkdir(parents=True, exist_ok=True)

def migrate_projects_structure(projects_dir: pathlib.Path):
    if not projects_dir or not projects_dir.exists():
        return
    dao_ly_dir = projects_dir / "dao-ly"
    
    for item in list(projects_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(".") or item.name in ("music", "dao-ly", "affiliate", "test_project_1"):
            continue
            
        if item.name.startswith("dao_ly_") or item.name.startswith("dao-ly-"):
            dao_ly_dir.mkdir(parents=True, exist_ok=True)
            sub_story = item / "story"
            target_dir = dao_ly_dir / item.name
            if sub_story.exists() and sub_story.is_dir():
                target_dir.mkdir(parents=True, exist_ok=True)
                for f in sub_story.iterdir():
                    shutil.move(str(f), str(target_dir / f.name))
                print(f"[Agent Migration] Moved legacy sub_story {item / 'story'} -> {target_dir}")
            elif item != target_dir and not target_dir.exists():
                shutil.move(str(item), str(target_dir))
                print(f"[Agent Migration] Moved legacy project {item} -> {target_dir}")

    # Remove empty dao-ly directory if created previously with no items
    if dao_ly_dir.exists() and not any(p for p in dao_ly_dir.iterdir() if not p.name.startswith(".")):
        try:
            shutil.rmtree(dao_ly_dir, ignore_errors=True)
        except Exception:
            pass

def resolve_local_media_file(bdir: pathlib.Path, file_path: str) -> Optional[pathlib.Path]:
    if not bdir or not bdir.exists():
        return None
    
    tf = (bdir / file_path).resolve()
    if tf.exists() and tf.is_file():
        return tf
    
    p = pathlib.Path(file_path)
    parent = (bdir / p.parent).resolve() if p.parent else bdir
    stem = p.stem
    
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".wav", ".mp3", ".m4a", ".mp4", ".mov", ".webm"]:
        alt = parent / f"{stem}{ext}"
        if alt.exists() and alt.is_file():
            return alt
            
    m = re.search(r'\d+', stem)
    if m and parent.exists() and parent.is_dir():
        num = m.group()
        num_int = int(num)
        
        if "audio" in str(p).lower():
            for astem in [f"processed_voiceover{num_int}", f"processed_voiceover_{num_int}", f"voiceover{num_int}", f"voiceover_{num_int}", f"voice{num_int}", f"voice_{num_int}", f"audio{num_int}", f"audio_{num_int}", str(num_int)]:
                for ext in [".wav", ".mp3", ".m4a"]:
                    alt = parent / f"{astem}{ext}"
                    if alt.exists() and alt.is_file():
                        return alt
                        
        elif "image" in str(p).lower() or "frame" in str(p).lower():
            for istem in [f"image{num_int}", f"image_{num_int}", f"frame{num_int}", f"frame_{num_int}", f"img{num_int}", f"img_{num_int}", str(num_int)]:
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    alt = parent / f"{istem}{ext}"
                    if alt.exists() and alt.is_file():
                        return alt
                        
        elif "video" in str(p).lower() or "clip" in str(p).lower() or p.suffix == ".mp4":
            for vstem in [f"clip{num_int}", f"clip_{num_int}", f"video{num_int}", f"video_{num_int}", f"final", str(num_int)]:
                for ext in [".mp4", ".mov", ".webm"]:
                    alt = parent / f"{vstem}{ext}"
                    if alt.exists() and alt.is_file():
                        return alt
                        
        matches = [f for f in parent.iterdir() if f.is_file() and not f.name.startswith(".") and re.search(r'\b' + num + r'\b', f.name)]
        if matches:
            return matches[0]

    if file_path == "final.mp4" or file_path.endswith(".mp4"):
        for cand_name in ["final.mp4", f"{bdir.name}.mp4"]:
            cand = bdir / cand_name
            if cand.exists() and cand.is_file():
                return cand
        mp4s = [f for f in bdir.glob("*.mp4") if not f.name.startswith(".")]
        if mp4s:
            return mp4s[0]
            
    return None

migrate_projects_structure(AGENT_PROJECTS_DIR)

agent_active_tasks: Dict[str, asyncio.Task] = {}
agent_queued_jobs: Dict[str, dict] = {}
pipeline_queue: asyncio.Queue = asyncio.Queue()

async def safe_send_ws(ws, payload: dict):
    if not ws:
        return
    try:
        if isinstance(payload, dict) and payload.get("type") == "pipeline_progress" and payload.get("project_name"):
            pname = payload["project_name"]
            if "story_id" not in payload and "_" in pname:
                sp, cp = pname.rsplit("_", 1)
                payload["story_id"] = sp
                payload["chapter_id"] = cp
        await ws.send(json.dumps(payload))
    except Exception as e:
        print(f"[Agent] Warning: WS send progress skipped ({e})")

def reorder_queue_positions():
    pos = 1
    for p_name, q_info in list(agent_queued_jobs.items()):
        q_info["position"] = pos
        ws = q_info.get("websocket")
        if ws:
            asyncio.create_task(safe_send_ws(ws, {
                "type": "pipeline_progress",
                "project_name": p_name,
                "status": "queued",
                "queue_position": pos,
                "total_queued": len(agent_queued_jobs)
            }))
        pos += 1

def remove_from_queue_and_active(story_id: str, chapter_id: str = None):
    def is_matching(k: str) -> bool:
        if chapter_id and chapter_id != "story":
            return k in (f"{story_id}_{chapter_id}", f"{story_id}/{chapter_id}", chapter_id)
        return k == story_id or k.startswith(f"{story_id}_") or k.startswith(f"{story_id}/")

    # 1. Cancel active running task if matching
    active_keys = [k for k in list(agent_active_tasks.keys()) if is_matching(k)]
    for k in active_keys:
        t = agent_active_tasks.get(k)
        if t and not t.done():
            print(f"[Queue Manager] Cancelling running task for deleted project: {k}")
            t.cancel()
        agent_active_tasks.pop(k, None)

    # 2. Remove from queued jobs dictionary
    queued_keys = [k for k in list(agent_queued_jobs.keys()) if is_matching(k)]
    for k in queued_keys:
        print(f"[Queue Manager] Removing deleted project '{k}' from queue.")
        agent_queued_jobs.pop(k, None)

    # 3. Recalculate queue positions for all remaining queued items
    reorder_queue_positions()
    
    # 4. If agent has no active tasks, trigger next queued job
    if not agent_active_tasks:
        asyncio.create_task(process_next_queued_job())

def agent_prepare_chapter_structure(story_id: str, chapter_id: str, content: str = "", chapter_dir: pathlib.Path = None) -> pathlib.Path:
    if chapter_dir is None:
        chapter_dir = AGENT_PROJECTS_DIR / story_id / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)

    text_dir = chapter_dir / "text"
    frag_dir = text_dir / "story_fragments"
    sent_dir = text_dir / "story_sentences"
    prompts_dir = text_dir / "image_prompts"

    frag_dir.mkdir(parents=True, exist_ok=True)
    sent_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)

    story_file = chapter_dir / "story.txt"
    
    if not content and story_file.exists():
        try:
            content = story_file.read_text(encoding="utf-8")
        except Exception:
            pass

    if content and content.strip():
        story_file.write_text(content.strip(), encoding="utf-8")
        
        frag_dir = text_dir / "story_fragments"
        existing_frags = list(frag_dir.glob("story_fragment*.txt")) if frag_dir.exists() else []
        if not existing_frags:
            try:
                from core import video_engine
                num_sentences = video_engine.load_and_split_to_sentences(story_file)
                video_engine.sentences_to_fragments(num_sentences, chapter_dir)
            except Exception as err:
                print(f"[Agent] Failed to tokenize via video_engine: {err}")

    return chapter_dir

async def enqueue_or_run_job(
    project_name: str,
    project_path_str: str,
    websocket,
    voice_config: dict = None,
    art_style: str = None,
    use_watermark: bool = True,
    use_waveform: bool = True,
    use_subtitles: bool = True,
    subtitle_preset: str = "viral-bold-yellow",
    use_whisper: bool = False,
    story_text: str = None,
    force_rerun: bool = False,
    effect_type: str = "leaves",
    image_generator: str = "ima2",
    pipeline_type: str = "story",
    music_b64: str = None,
    music_filename: str = None,
    music_local_path: str = None,
    rerun_mode: str = "all",
    aspect_ratio: str = None
):
    payload = {
        "project_name": project_name,
        "project_path": project_path_str,
        "voice_config": voice_config,
        "art_style": art_style,
        "image_generator": image_generator,
        "use_watermark": use_watermark,
        "use_waveform": use_waveform,
        "use_subtitles": use_subtitles,
        "subtitle_preset": subtitle_preset,
        "use_whisper": use_whisper,
        "story_text": story_text,
        "force_rerun": force_rerun,
        "effect_type": effect_type,
        "pipeline_type": pipeline_type,
        "music_b64": music_b64,
        "music_filename": music_filename,
        "music_local_path": music_local_path,
        "rerun_mode": rerun_mode,
        "aspect_ratio": aspect_ratio
    }
    
    if agent_active_tasks or not pipeline_queue.empty():
        q_pos = len(agent_queued_jobs) + 1
        agent_queued_jobs[project_name] = {
            "position": q_pos,
            "status": "queued",
            "websocket": websocket,
            "payload": payload
        }
        await pipeline_queue.put({
            "project_name": project_name,
            "websocket": websocket,
            "payload": payload,
            "pipeline_type": pipeline_type
        })
        print(f"[Queue Manager] Queued project '{project_name}' at position #{q_pos}. Active tasks: {len(agent_active_tasks)}")
        await safe_send_ws(websocket, {
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "queued",
            "queue_position": q_pos,
            "total_queued": len(agent_queued_jobs),
            "message": f"Dự án đã được xếp vào hàng đợi ở vị trí #{q_pos}"
        })
        return {"status": "queued", "queue_position": q_pos}
    else:
        print(f"[Queue Manager] Agent free. Starting execution immediately for '{project_name}'.")
        if pipeline_type == "music":
            t = asyncio.create_task(run_music_pipeline_task(
                project_name, project_path_str, websocket, voice_config, art_style,
                use_watermark, use_subtitles, subtitle_preset, use_whisper,
                music_b64, music_filename, music_local_path, force_rerun
            ))
            agent_active_tasks[project_name] = t
        else:
            t = asyncio.create_task(run_pipeline_task(
                project_name, project_path_str, websocket, voice_config, art_style,
                use_watermark=use_watermark, use_waveform=use_waveform,
                use_subtitles=use_subtitles, subtitle_preset=subtitle_preset,
                story_text=story_text, force_rerun=force_rerun, effect_type=effect_type,
                image_generator=image_generator, rerun_mode=rerun_mode, aspect_ratio=aspect_ratio
            ))
            agent_active_tasks[project_name] = t
        return {"status": "running"}

async def process_next_queued_job():
    if agent_active_tasks:
        return
    while not pipeline_queue.empty():
        try:
            job = await pipeline_queue.get()
            project_name = job["project_name"]
            
            # Skip if job was removed/deleted from queue
            if project_name not in agent_queued_jobs:
                print(f"[Queue Manager] Job '{project_name}' was deleted or removed. Skipping queue item.")
                continue

            websocket = job["websocket"]
            payload = job["payload"]
            pipeline_type = job.get("pipeline_type", "story")
            
            agent_queued_jobs.pop(project_name, None)
            reorder_queue_positions()

            print(f"[Queue Manager] Popped job '{project_name}' from queue. Starting execution...")

            if pipeline_type == "music":
                music_b64 = payload.get("music_b64")
                music_filename = payload.get("music_filename")
                music_local_path = payload.get("music_local_path")
                t = asyncio.create_task(run_music_pipeline_task(
                    project_name, payload.get("project_path"), websocket,
                    payload.get("voice_config"), payload.get("art_style"),
                    payload.get("use_watermark", True), payload.get("use_subtitles", True),
                    payload.get("subtitle_preset", "karaoke-green"), payload.get("use_whisper", False),
                    music_b64, music_filename, music_local_path, payload.get("force_rerun", False)
                ))
                agent_active_tasks[project_name] = t
            else:
                story_text = payload.get("story_text")
                effect_type = payload.get("effect_type", "leaves")
                image_generator = payload.get("image_generator", "ima2")
                rerun_mode = payload.get("rerun_mode", "all")
                aspect_ratio = payload.get("aspect_ratio")
                t = asyncio.create_task(run_pipeline_task(
                    project_name, payload.get("project_path"), websocket,
                    payload.get("voice_config"), payload.get("art_style"),
                    use_watermark=payload.get("use_watermark", True),
                    use_waveform=payload.get("use_waveform", True),
                    use_subtitles=payload.get("use_subtitles", True),
                    subtitle_preset=payload.get("subtitle_preset", "viral-bold-yellow"),
                    story_text=story_text,
                    force_rerun=payload.get("force_rerun", False),
                    effect_type=effect_type,
                    image_generator=image_generator,
                    rerun_mode=rerun_mode,
                    aspect_ratio=aspect_ratio
                ))
                agent_active_tasks[project_name] = t
            break
        except Exception as e:
            print(f"[Queue Manager] Error processing next queued job: {e}")

# Load config
_CONFIG_PATH = AGENT_DIR / "config.ini"
config = configparser.ConfigParser()
config.read(_CONFIG_PATH, encoding="utf-8")

import getpass
import uuid
import hashlib
import socket

def get_default_workspace_id():
    try:
        user = getpass.getuser().lower()
        clean_user = "".join(c for c in user if c.isalnum() or c in ("-", "_")).strip() or "user"
        mac = uuid.getnode()
        hostname = socket.gethostname()
        dev_hash = hashlib.md5(f"{mac}-{hostname}".encode()).hexdigest()[:6]
        return f"{clean_user}_{dev_hash}"
    except Exception:
        pass
    return "default_workspace"

server_env = os.environ.get("SERVER_URL")
if server_env:
    SERVER_URL = server_env
else:
    SERVER_URL = config.get("TAKA_AGENT", "SERVER_URL", fallback="https://tales.taka.zone")
config_ws = config.get("TAKA_AGENT", "WORKSPACE_ID", fallback="").strip()
if config_ws and config_ws.lower() not in ("auto", "default", "default_workspace") and not config_ws.startswith("device_"):
    WORKSPACE_ID = config_ws
else:
    WORKSPACE_ID = get_default_workspace_id()

print(f"[Agent] Starting agent with WORKSPACE_ID: '{WORKSPACE_ID}'")
agent_running_jobs = {}

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

async def send_ws_message(msg: dict) -> bool:
    """Send WS message safely using active_websocket connection."""
    global active_websocket
    ws = active_websocket
    if ws:
        try:
            await ws.send(json.dumps(msg))
            return True
        except Exception as err:
            print(f"[Agent WS Warning] Failed to send message over active WS: {err}")
    return False

_env_cache = None

async def check_environment() -> dict:
    """Check availability of local CUDA/MPS, Ollama, and OmniVoice setup non-blockingly."""
    global _env_cache
    if _env_cache:
        return _env_cache

    def _do_check():
        cuda_available = False
        mps_available = False
        try:
            import torch
            cuda_available = torch.cuda.is_available()
            mps_available = hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        except Exception:
            pass

        ollama_active = False
        try:
            res = requests.get("http://localhost:11434/api/tags", timeout=1.0)
            if res.status_code == 200:
                ollama_active = True
        except Exception:
            pass

        omnivoice_installed = OMNIVOICE_PATH.exists() and (OMNIVOICE_PATH / "pyproject.toml").exists()

        return {
            "cuda_available": cuda_available,
            "mps_available": mps_available,
            "ollama_active": ollama_active,
            "omnivoice_installed": omnivoice_installed,
            "agent_version": "0.4.4"
        }

    res = await asyncio.to_thread(_do_check)
    _env_cache = res
    return res

def sync_and_migrate_voice_dir(voice_dir: pathlib.Path):
    if not voice_dir or not voice_dir.exists():
        return
    local_path_file = voice_dir / "local_path.txt"
    if local_path_file.exists():
        try:
            with open(local_path_file, "r", encoding="utf-8") as f:
                src_str = f.read().strip()
            if src_str:
                src_path = pathlib.Path(src_str)
                if src_path.exists():
                    import shutil
                    ext = src_path.suffix.lower() or ".wav"
                    dest_file = voice_dir / f"ref{ext}"
                    shutil.copy2(str(src_path), str(dest_file))
                    if ext != ".wav":
                        dest_wav = voice_dir / "ref.wav"
                        shutil.copy2(str(src_path), str(dest_wav))
                    local_path_file.unlink()
        except Exception as ex:
            print(f"[VoiceMigrate] Error migrating local_path.txt for {voice_dir.name}: {ex}")

    ref_text_file = voice_dir / "ref_text.txt"
    ref_txt_file = voice_dir / "ref.txt"
    if ref_text_file.exists() and not ref_txt_file.exists():
        try:
            import shutil
            shutil.copy2(str(ref_text_file), str(ref_txt_file))
        except Exception:
            pass
    elif ref_txt_file.exists() and not ref_text_file.exists():
        try:
            import shutil
            shutil.copy2(str(ref_txt_file), str(ref_text_file))
        except Exception:
            pass

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

def normalize_units_for_tts(text: str, language: str = "vi") -> str:
    from core.text_formatter import normalize_units_for_tts as _core_normalize
    return _core_normalize(text, language=language)


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
    
    # Resolve language
    language = "vi"
    if voice_config and voice_config.get("language"):
        language = voice_config["language"]
    else:
        language = OMNIVOICE_LANG
    if not language:
        language = "vi"
        
    import re
    clean_text = text.replace("\\n", " ").replace("\n", " ")
    clean_text = re.sub(r'\[.*?\]', '', clean_text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    if not clean_text:
        clean_text = text

    cmd = [
        sys.executable, str(script_path),
        "--model", "k2-fsa/OmniVoice",
        "--text", clean_text,
        "--language", language,
        "--output", str(out)
    ]
    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    except ImportError:
        device = "cpu"
    cmd += ["--device", device]
    
    # Add voice cloning or voice design flags if voice_config matches
    if voice_config:
        mode = voice_config.get("omnivoice_mode")
        if mode == "clone" and voice_config.get("ref_audio_path"):
            cmd += ["--ref_audio", voice_config["ref_audio_path"]]
            # Omit --ref_text so OmniVoice auto-transcribes reference audio with Whisper ASR,
            # which avoids prepending reference text to generated voiceover.
        elif mode == "design" and voice_config.get("voice_instruct"):
            raw_inst = voice_config["voice_instruct"].strip()
            valid_tags = {
                "american accent", "australian accent", "british accent", "canadian accent",
                "child", "chinese accent", "elderly", "female", "high pitch", "indian accent",
                "japanese accent", "korean accent", "low pitch", "male", "middle-aged",
                "moderate pitch", "portuguese accent", "russian accent", "teenager",
                "very high pitch", "very low pitch", "whisper", "young adult"
            }
            parts = [p.strip().lower() for p in raw_inst.replace("\n", ",").split(",") if p.strip()]
            matched = [p for p in parts if p in valid_tags]
            if not matched:
                matched = ["male", "low pitch"] if "nam" in raw_inst.lower() else ["female", "low pitch"]
            cmd += ["--instruct", ", ".join(matched)]

        if voice_config.get("speed") is not None:
            try:
                sp_val = float(voice_config["speed"])
                cmd += ["--speed", str(sp_val)]
            except (ValueError, TypeError):
                pass

    print(f"[Agent] Executing OmniVoice CLI command: {' '.join(cmd)}")
    try:
        import os
        sub_env = os.environ.copy()
        sub_env["PYTHONPATH"] = str(OMNIVOICE_PATH)
        res = subprocess.run(cmd, check=True, capture_output=True, text=True, env=sub_env, cwd=str(OMNIVOICE_PATH))
        print(f"[Agent] Generated OmniVoice audio at {out}")
        if res.stdout:
            print(f"[Agent] OmniVoice stdout: {res.stdout}")
        if res.stderr:
            print(f"[Agent] OmniVoice stderr: {res.stderr}")
    except subprocess.CalledProcessError as e:
        print(f"[Agent] OmniVoice script failed: {e.stderr}")
        raise RuntimeError(f"OmniVoice synthesis failed: {e.stderr}")

async def generate_voiceover(text: str, out: pathlib.Path, voice_config: dict = None) -> None:
    """Routing helper that generates voiceover according to provider settings."""
    default_config = {
        "provider": config.get("AUDIO", "TTS_PROVIDER", fallback="edge"),
        "omnivoice_mode": config.get("AUDIO", "OMNIVOICE_MODE", fallback="auto"),
        "ref_audio_path": config.get("AUDIO", "OMNIVOICE_REF_AUDIO", fallback=None),
        "ref_text": config.get("AUDIO", "OMNIVOICE_REF_TEXT", fallback=None),
        "voice_instruct": config.get("AUDIO", "OMNIVOICE_INSTRUCT", fallback=None),
        "voice_id": config.get("AUDIO", "VOICE", fallback=None),
        "language": config.get("OMNIVOICE", "LANGUAGE", fallback="vi"),
        "speed": float(config.get("AUDIO", "OMNIVOICE_SPEED", fallback="0.85"))
    }
    
    # Merge custom voice_config over defaults
    merged_config = default_config.copy()
    if voice_config:
        for k, v in voice_config.items():
            if v is not None and v != "":
                merged_config[k] = v
                
    # If ref_audio_b64 is passed, save it to a local temp file
    ref_audio_b64 = merged_config.get("ref_audio_b64")
    if ref_audio_b64 and not merged_config.get("ref_audio_path"):
        try:
            import base64
            voices_base = pathlib.Path.home() / ".taka-agent" / "voices"
            voices_base.mkdir(parents=True, exist_ok=True)
            voice_id = merged_config.get("voice_id", "temp_voice")
            voice_dir = voices_base / voice_id
            voice_dir.mkdir(parents=True, exist_ok=True)
            
            ref_audio_file = voice_dir / "ref.wav"
            with open(ref_audio_file, "wb") as f:
                f.write(base64.b64decode(ref_audio_b64))
            merged_config["ref_audio_path"] = str(ref_audio_file)
            print(f"[Agent] Decoded and saved ref_audio_b64 to {ref_audio_file}")
        except Exception as ex:
            print(f"[Agent] Failed to decode ref_audio_b64: {ex}")

    # Resolve local voice profile directory if voice_id is specified
    voice_id = merged_config.get("voice_id")
    if voice_id:
        voices_base = pathlib.Path.home() / ".taka-agent" / "voices"
        if not voices_base.exists():
            voices_base = AGENT_DIR / "voices"
            
        voice_dir = voices_base / voice_id
        if not voice_dir.exists():
            voice_dir = AGENT_DIR / "voices" / voice_id
            
        if voice_dir.exists():
            local_path_file = voice_dir / "local_path.txt"
            ref_audio_file = voice_dir / "ref.wav"
            if not ref_audio_file.exists():
                for ext in ["mp3", "m4a", "flac", "ogg"]:
                    alt = voice_dir / f"ref.{ext}"
                    if alt.exists():
                        ref_audio_file = alt
                        break
            ref_text_file = voice_dir / "ref_text.txt"
            if not ref_text_file.exists():
                ref_text_file = voice_dir / "ref.txt"
            voice_instruct_file = voice_dir / "voice_instruct.txt"
            if not voice_instruct_file.exists():
                voice_instruct_file = voice_dir / "instruct.txt"
            
            profile_ref_audio = None
            if local_path_file.exists():
                try:
                    with open(local_path_file, "r", encoding="utf-8") as f:
                        path_str = f.read().strip()
                        if path_str and pathlib.Path(path_str).exists():
                            profile_ref_audio = path_str
                except Exception as ex:
                    print(f"[Agent] Failed to read local_path.txt: {ex}")
            if not profile_ref_audio and ref_audio_file and ref_audio_file.exists():
                profile_ref_audio = str(ref_audio_file)
            
            if profile_ref_audio:
                merged_config["ref_audio_path"] = profile_ref_audio
            
            if ref_text_file.exists():
                try:
                    with open(ref_text_file, "r", encoding="utf-8") as f:
                        merged_config["ref_text"] = f.read().strip()
                except Exception as ex:
                    print(f"[Agent] Failed to read ref_text.txt: {ex}")

            if voice_instruct_file.exists():
                try:
                    with open(voice_instruct_file, "r", encoding="utf-8") as f:
                        merged_config["voice_instruct"] = f.read().strip()
                        merged_config["omnivoice_mode"] = "design"
                        merged_config["provider"] = "omnivoice"
                except Exception as ex:
                    print(f"[Agent] Failed to read voice_instruct.txt: {ex}")

    lang = merged_config.get("language", "vi")
    from core.text_formatter import format_for_voice
    formatted_text = format_for_voice(text, language=lang)

    provider = merged_config.get("provider", "edge").lower()
    print(f"[Agent] Routing TTS generation. provider={provider}, language={lang}, voice_config: { {k: (v[:30]+'...' if isinstance(v, str) and len(v) > 30 else v) for k, v in merged_config.items() if k != 'ref_audio_b64'} }")
    if voice_id:
        print(f"[Agent] Resolved local voice profile for voice_id='{voice_id}': ref_audio_path='{merged_config.get('ref_audio_path')}', ref_text='{merged_config.get('ref_text')}'")
    
    if provider == "omnivoice":
        await asyncio.to_thread(tts_omnivoice, formatted_text, out, merged_config)
    elif provider == "kokoro":
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.KOKORO_VOICE_ID
        if custom_voice:
            video_engine.KOKORO_VOICE_ID = custom_voice
        try:
            await asyncio.to_thread(video_engine.tts_kokoro, formatted_text, out)
        finally:
            video_engine.KOKORO_VOICE_ID = orig_voice
    elif provider == "elevenlabs":
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.ELEVENLABS_VOICE_ID
        if custom_voice:
            video_engine.ELEVENLABS_VOICE_ID = custom_voice
        try:
            await asyncio.to_thread(video_engine.tts_elevenlabs, formatted_text, out)
        finally:
            video_engine.ELEVENLABS_VOICE_ID = orig_voice
    else:  # edge-tts
        custom_voice = merged_config.get("voice_id")
        orig_voice = video_engine.VOICE
        if custom_voice:
            video_engine.VOICE = custom_voice
        try:
            await video_engine.tts_edge(formatted_text, out)
        finally:
            video_engine.VOICE = orig_voice

    # Apply audio speed post-processing if speed != 1.0
    desired_speed = float(merged_config.get("speed", 1.0))
    if abs(desired_speed - 1.0) > 0.01 and out.exists() and out.stat().st_size > 0:
        print(f"[Agent] Applying TTS speed adjustment: {desired_speed}x to {out.name}")
        tmp_speed_out = out.with_suffix(".speed.tmp" + out.suffix)
        cmd = [
            "ffmpeg", "-y", "-i", str(out),
            "-filter:a", f"atempo={desired_speed}",
            str(tmp_speed_out)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and tmp_speed_out.exists() and tmp_speed_out.stat().st_size > 0:
            shutil.move(str(tmp_speed_out), str(out))

def update_reels_content_json(category_dir: pathlib.Path) -> None:
    """Scan created chapter subdirectories in category_dir and update category-level content.json (appending new items to the end)."""
    if not category_dir or not category_dir.exists():
        return
    import glob
    kien_thuc_map = {}
    for fpath in glob.glob("data/kien-thuc/*.json") + glob.glob("data/kien-thuc-longform/*.json") + glob.glob("data/monochromatic_pencil_sketch/*.json"):
        try:
            data = json.load(open(fpath, encoding="utf-8"))
            if isinstance(data, dict) and "slug" in data:
                kien_thuc_map[data["slug"]] = data
        except Exception:
            pass

    # 1. Gather all currently existing valid chapter subdirectories
    existing_subdirs = {
        p.name: p for p in category_dir.iterdir()
        if p.is_dir() and (p / "project_config.json").exists()
    }

    rel_workspace = pathlib.Path("projects") / category_dir.name
    if rel_workspace.exists():
        for p in rel_workspace.iterdir():
            if p.is_dir() and (p / "project_config.json").exists():
                existing_subdirs[p.name] = p

    # 2. Read existing items from content.json (if present) to keep order
    content_file = category_dir / "content.json"
    if not content_file.exists() and (rel_workspace / "content.json").exists():
        content_file = rel_workspace / "content.json"

    base_dict = {}
    old_items = []
    if content_file.exists():
        try:
            with open(content_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            if isinstance(raw_data, dict):
                base_dict = raw_data
                old_items = raw_data.get("items", [])
            elif isinstance(raw_data, list):
                old_items = raw_data
        except Exception:
            old_items = []

    # 3. Filter old items to keep only those that still exist on disk (matching slug or id)
    final_items = []
    seen_slugs = set()
    for item in old_items:
        if isinstance(item, dict):
            item_slug = item.get("slug") or item.get("id")
            if item_slug and item_slug in existing_subdirs and item_slug not in seen_slugs:
                final_items.append(item)
                seen_slugs.add(item_slug)

    # 4. Append newly created chapters to the END of the list
    for slug in sorted(existing_subdirs.keys()):
        if slug not in seen_slugs:
            p = existing_subdirs[slug]
            item_meta = {}
            if (p / "item.json").exists():
                try:
                    with open(p / "item.json", "r", encoding="utf-8") as f:
                        item_meta = json.load(f)
                except Exception: pass
            
            info = kien_thuc_map.get(slug, {})
            title = item_meta.get("title") or info.get("title", slug.replace("-", " ").title())
            short_title = item_meta.get("short_title") or info.get("short_title", title)
            
            item_data = {
                "id": slug,
                "slug": slug,
                "title": title,
                "short_title": short_title
            }
            if "episode" in item_meta:
                item_data["episode"] = item_meta["episode"]
            if "episode_label" in item_meta:
                item_data["episode_label"] = item_meta["episode_label"]
                
            final_items.append(item_data)
            seen_slugs.add(slug)

    # 5. Write content.json inside each chapter folder
    for slug, p in existing_subdirs.items():
        info = kien_thuc_map.get(slug, {})
        title = info.get("title", slug.replace("-", " ").title())
        short_title = info.get("short_title", title)
        item_data = {
            "slug": slug,
            "title": title,
            "short_title": short_title
        }
        try:
            with open(p / "content.json", "w", encoding="utf-8") as f:
                json.dump(item_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 6. Write final category-level content.json (preserving dict structure with project_type)
    if base_dict:
        out_content = dict(base_dict)
        out_content["items"] = final_items
    else:
        out_content = {
            "project_name": category_dir.name,
            "project_type": ("long" if "videos" in category_dir.name or "longform" in category_dir.name or "sketch" in category_dir.name else ("reels" if "reels" in category_dir.name else "story")),
            "items": final_items
        }

    for dest in (category_dir, rel_workspace):
        try:
            dest.mkdir(parents=True, exist_ok=True)
            cpath = dest / "content.json"
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(out_content, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            print(f"[Agent] Failed to write content.json to {dest}: {ex}")

def normalize_rerun_mode(r_mode: Optional[str]) -> str:
    if not r_mode:
        return "all"
    rm = str(r_mode).lower().strip()
    if rm in ("images", "image", "images_only", "image_only", "img"):
        return "images_only"
    if rm in ("audio", "voice", "audio_only", "voice_only", "tts"):
        return "audio_only"
    if rm in ("subtitles", "subtitle", "subtitles_only", "subtitle_only", "sub"):
        return "subtitles_only"
    if rm in ("video", "render", "video_only", "render_only", "clip"):
        return "video_only"
    return "all"

async def run_pipeline_task(project_name: str, project_path_str: str, websocket, voice_config: dict = None, art_style: str = None, use_watermark: bool = True, use_waveform: bool = True, use_subtitles: bool = True, subtitle_preset: str = "viral-bold-yellow", story_text: str = None, force_rerun: bool = False, effect_type: str = "leaves", image_generator: str = "ima2", rerun_mode: str = "all", aspect_ratio: str = None):
    """Executes the full Taka-Tales pipeline and reports progress in real time."""
    try:
        rerun_mode = normalize_rerun_mode(rerun_mode)
        # Resolve project folder relative to AGENT_DIR/projects to support remote server
        path_obj = pathlib.Path(project_path_str)
        story_id = path_obj.parent.name
        chapter_id = path_obj.name
        project_dir = AGENT_PROJECTS_DIR / story_id / chapter_id
        if not project_dir.exists():
            for cdir in [
                AGENT_PROJECTS_DIR / "reels" / chapter_id,
                AGENT_PROJECTS_DIR / "dao-ly" / chapter_id,
                AGENT_PROJECTS_DIR / "longform" / chapter_id,
                AGENT_DIR / "_projects" / "longform" / chapter_id,
                AGENT_DIR / "_projects" / "reels" / chapter_id,
                AGENT_DIR / "_projects" / "dao-ly" / chapter_id,
                AGENT_PROJECTS_DIR / chapter_id
            ]:
                if cdir.exists():
                    project_dir = cdir
                    break

        req_aspect = aspect_ratio
        if not req_aspect and voice_config:
            req_aspect = voice_config.get("aspect_ratio")
        if not req_aspect and (project_dir / "item.json").exists():
            try:
                with open(project_dir / "item.json", "r", encoding="utf-8") as f:
                    ij = json.load(f)
                req_aspect = ij.get("aspect_ratio")
            except Exception:
                pass
        if not req_aspect and (project_dir / "aspect_ratio.txt").exists():
            try:
                req_aspect = (project_dir / "aspect_ratio.txt").read_text(encoding="utf-8").strip()
            except Exception:
                pass
        if not req_aspect and (project_dir / "project_config.json").exists():
            try:
                with open(project_dir / "project_config.json", "r", encoding="utf-8") as f:
                    pc = json.load(f)
                req_aspect = pc.get("aspect_ratio")
            except Exception:
                pass

        is_long = ("long" in story_id.lower() or "videos" in story_id.lower() or "sketch" in story_id.lower())
        final_aspect = req_aspect or ("16:9" if is_long else "9:16")
        project_dir.mkdir(parents=True, exist_ok=True)
        with open(project_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
            f.write(final_aspect)

        video_engine.configure_project_resolution(project_dir, final_aspect)
        
        existing_story = None
        if (project_dir / "story.txt").exists():
            try:
                with open(project_dir / "story.txt", "r", encoding="utf-8") as f:
                    existing_story = f.read()
            except Exception:
                pass

        if force_rerun:
            print(f"[Agent] force_rerun enabled: Clearing generated cache files only for {project_dir}")

        project_dir.mkdir(parents=True, exist_ok=True)
        
        # Save story.txt if sent by server or if it already existed
        script_lang = None
        story_to_save = story_text or existing_story
        if story_to_save:
            story_str = story_to_save.strip()
            if story_str.startswith("{") and story_str.endswith("}"):
                try:
                    js = json.loads(story_str)
                    if isinstance(js, dict):
                        if "language" in js:
                            script_lang = js["language"]
                        if "content" in js:
                            story_to_save = js["content"]
                except Exception:
                    pass

        if not script_lang and project_dir:
            import glob
            for fpath in glob.glob("data/kien-thuc/*.json") + glob.glob("data/kien-thuc-longform/*.json") + glob.glob("data/monochromatic_pencil_sketch/*.json"):
                try:
                    data = json.load(open(fpath, encoding="utf-8"))
                    if isinstance(data, dict) and data.get("slug") == project_dir.name:
                        script_lang = data.get("language")
                        break
                except Exception:
                    pass

        if script_lang:
            if not voice_config:
                voice_config = {}
            voice_config["language"] = script_lang

        if story_to_save:
            story_to_save = story_to_save.replace("\\n", "\n")
            with open(project_dir / "story.txt", "w", encoding="utf-8") as f:
                f.write(story_to_save)
                
        # Save project_config.json
        config_data = {
            "art_style": art_style or "comic",
            "use_watermark": use_watermark,
            "use_waveform": use_waveform,
            "use_subtitles": use_subtitles,
            "subtitle_preset": subtitle_preset,
            "aspect_ratio": final_aspect,
            "use_whisper": False,
            "effect_type": effect_type,
            "voice_config": voice_config or {}
        }
        with open(project_dir / "project_config.json", "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
            
        # Update category-level content.json (only created chapters)
        try:
            update_reels_content_json(project_dir.parent)
        except Exception as ex:
            print(f"[Agent] Failed to update category content.json: {ex}")
        
        # 1. Setup folders and clean based on rerun_mode
        for sub in ("text", "audio", "images", "videos"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)
            
        if rerun_mode == "audio_only":
            print(f"[Agent] rerun_mode='audio_only': Preserving existing images; updating audio and videos.")
            (project_dir / "audio").mkdir(parents=True, exist_ok=True)
            (project_dir / "videos").mkdir(parents=True, exist_ok=True)
        elif rerun_mode == "images_only":
            print(f"[Agent] rerun_mode='images_only': Preserving existing audio & other images; updating target images.")
            (project_dir / "text/image_prompts").mkdir(parents=True, exist_ok=True)
            (project_dir / "images").mkdir(parents=True, exist_ok=True)
            (project_dir / "videos").mkdir(parents=True, exist_ok=True)
        elif rerun_mode == "subtitles_only":
            print(f"[Agent] rerun_mode='subtitles_only': Clearing videos/")
            (project_dir / "videos").mkdir(parents=True, exist_ok=True)
        else: # "all"
            # Keep text/story_sentences and text/story_fragments if they exist unless force_rerun
            folders_to_clear = ["videos"]
            if force_rerun:
                folders_to_clear.extend(["text/story_sentences", "text/story_fragments", "text/image_prompts"])
            for folder in folders_to_clear:
                fpath = project_dir / folder
                if fpath.exists():
                    shutil.rmtree(fpath, ignore_errors=True)
                fpath.mkdir(parents=True, exist_ok=True)

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

        # 2. Text preprocessing and splitting (Preserve prepared fragments if present)
        story_file = project_dir / "story.txt"
        frag_dir = project_dir / "text/story_fragments"
        existing_frags = list(frag_dir.glob("story_fragment*.txt")) if frag_dir.exists() else []

        if not existing_frags or force_rerun:
            num_sentences = video_engine.load_and_split_to_sentences(story_file)
            num_frags = video_engine.sentences_to_fragments(num_sentences, project_dir)
        else:
            num_frags = len(existing_frags)
            print(f"[Agent] Preserving {num_frags} existing story fragments in {frag_dir}")

        # 2.5 Slice fragments range without destroying text/story_fragments
        frag_dir = project_dir / "text/story_fragments"
        existing_frag_files = sorted(list(frag_dir.glob("story_fragment*.txt")), key=lambda f: int(re.search(r'\d+', f.stem).group()) if re.search(r'\d+', f.stem) else 9999) if frag_dir.exists() else []
        total_frags = len(existing_frag_files) if existing_frag_files else num_frags

        start_frag = 0
        limit_frag = 0
        if voice_config:
            start_frag = int(voice_config.get("start_fragment", 0))
            limit_frag = int(voice_config.get("limit_fragments", 0))

        start_idx = 0
        end_idx = total_frags
        if start_frag > 0 or limit_frag > 0:
            s_idx = max(0, start_frag - 1) if start_frag > 0 else 0
            start_idx = min(s_idx, max(0, total_frags - 1)) if total_frags > 0 else 0
            if limit_frag > 0:
                end_idx = min(start_idx + limit_frag, total_frags)

        target_indices = list(range(start_idx, end_idx))
        sub_count = len(target_indices)

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "generating_prompts",
            "current_fragment": 0,
            "total_fragments": sub_count
        }))

        # 3. Generate prompts (Ollama)
        if rerun_mode in ("audio_only", "subtitles_only", "video_only", "render_only"):
            print(f"[Agent] rerun_mode='{rerun_mode}': Preserving existing image prompts.")
        else:
            await asyncio.to_thread(video_engine._unload_sd)
            await asyncio.to_thread(video_engine._reload_ollama)
            prompt_dir = project_dir / "text/image_prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            for idx in target_indices:
                prompt_file = prompt_dir / f"image_prompt{idx}.txt"
                frag_file = frag_dir / f"story_fragment{idx}.txt"
                if frag_file.exists() and (not prompt_file.exists() or rerun_mode == "images_only" or force_rerun):
                    prompt = await asyncio.to_thread(
                        video_engine.build_image_prompt,
                        video_engine._read_text(frag_file),
                        art_style
                    )
                    video_engine._write_text(prompt_file, prompt)
                    
                await websocket.send(json.dumps({
                    "type": "pipeline_progress",
                    "project_name": project_name,
                    "status": "generating_prompts",
                    "current_fragment": idx + 1,
                    "total_fragments": total_frags
                }))

        # 4. Generate audio (OmniVoice)
        audio_dir = project_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        
        if rerun_mode in ("images_only", "subtitles_only", "video_only", "render_only"):
            print(f"[Agent] rerun_mode='{rerun_mode}': Preserving existing audio files. Skipping TTS generation.")
        else:
            saved_vc = config_data.get("voice_config", {})
            req_speed = voice_config.get("speed") if isinstance(voice_config, dict) else None
            saved_speed = saved_vc.get("speed") if isinstance(saved_vc, dict) else None
            req_voice = voice_config.get("voice_id") if isinstance(voice_config, dict) else None
            saved_voice = saved_vc.get("voice_id") if isinstance(saved_vc, dict) else None
            req_lang = voice_config.get("language") if isinstance(voice_config, dict) else None
            saved_lang = saved_vc.get("language") if isinstance(saved_vc, dict) else None

            if force_rerun or rerun_mode == "audio_only" or (req_speed is not None and saved_speed is not None and float(req_speed) != float(saved_speed)) or (req_voice and saved_voice and req_voice != saved_voice) or (req_lang and saved_lang and req_lang != saved_lang):
                print(f"[Agent] Invalidation triggered for target fragments (rerun_mode={rerun_mode}). Clearing audio cache for target indices...")
                for idx in target_indices:
                    for f in (audio_dir / f"voiceover{idx}.wav", audio_dir / f"voiceover{idx}.mp3", audio_dir / f"processed_voiceover{idx}.wav"):
                        if f.exists():
                            try:
                                f.unlink()
                            except Exception:
                                pass

            for idx in target_indices:
                await websocket.send(json.dumps({
                    "type": "pipeline_progress",
                    "project_name": project_name,
                    "status": "generating_audio",
                    "current_fragment": idx,
                    "total_fragments": total_frags,
                    "fragment_status": {"idx": idx, "step": "tts"}
                }))
                
                wav = project_dir / f"audio/voiceover{idx}.wav"
                mp3 = project_dir / f"audio/voiceover{idx}.mp3"
                if not (wav.exists() or mp3.exists()):
                    frag_file = frag_dir / f"story_fragment{idx}.txt"
                    if frag_file.exists():
                        frag = video_engine._read_text(frag_file)
                        await generate_voiceover(frag, wav, voice_config)

        # IF AUDIO ONLY: STOP HERE IMMEDIATELY!
        if rerun_mode == "audio_only":
            print(f"[Agent] rerun_mode='audio_only': Completed audio generation. Skipping image & video rendering.")
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "completed",
                "current_fragment": total_frags,
                "total_fragments": total_frags,
                "message": "🎙️ Audio generation completed!"
            }))
            return

        # 5. Generate images
        if rerun_mode in ("subtitles_only", "video_only", "render_only"):
            print(f"[Agent] rerun_mode='{rerun_mode}': Preserving existing image files. Skipping image generation.")
        else:
            await asyncio.to_thread(video_engine._unload_ollama)
            await asyncio.to_thread(video_engine._reload_sd)
            
            img_gen = image_generator or config_data.get("image_generator", "ima2")
            orig_sd_api = video_engine.USE_SD_API
            video_engine.USE_SD_API = img_gen

            try:
                sem = asyncio.Semaphore(1 if img_gen == "ima2" else 3)
                force_img_gen = (rerun_mode == "images_only" or force_rerun)

                async def gen_single(idx):
                    async with sem:
                        img = project_dir / f"images/image{idx}.jpg"
                        if force_img_gen or not img.exists():
                            await websocket.send(json.dumps({
                                "type": "pipeline_progress",
                                "project_name": project_name,
                                "status": "generating_images",
                                "current_fragment": idx,
                                "total_fragments": total_frags,
                                "fragment_status": {"idx": idx, "step": "image"}
                            }))
                            await asyncio.to_thread(video_engine.generate_image, idx, project_dir, art_style, force_img_gen)

                tasks = [gen_single(idx) for idx in target_indices]
                await asyncio.gather(*tasks)
            finally:
                video_engine.USE_SD_API = orig_sd_api

        # IF IMAGES ONLY: STOP HERE IMMEDIATELY!
        if rerun_mode == "images_only":
            print(f"[Agent] rerun_mode='images_only': Completed image generation. Skipping video rendering.")
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "completed",
                "current_fragment": total_frags,
                "total_fragments": total_frags,
                "message": "🎨 Image generation completed!"
            }))
            return

        # 6. Render clips (MoviePy)
        force_clip_render = (rerun_mode in ("subtitles_only", "video_only", "render_only") or force_rerun)
        for idx in target_indices:
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "compiling_clips",
                "current_fragment": idx,
                "total_fragments": total_frags,
                "fragment_status": {"idx": idx, "step": "clip"}
            }))
            
            out_clip = project_dir / f"videos/video{idx}.mp4"
            if force_clip_render or not out_clip.exists():
                await asyncio.to_thread(video_engine.create_video_clip, idx, project_dir)

        # 7. Final Concatenation and music assembly
        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "assembling_final_video",
            "current_fragment": total_frags,
            "total_fragments": total_frags
        }))
        
        final_video = project_dir / f"{project_name}.mp4"
        server_final = project_dir / "final.mp4"
        
        await asyncio.to_thread(video_engine.make_final_video, project_name, project_dir, start_idx=start_idx, end_idx=end_idx)
        if final_video.exists():
            shutil.copy(str(final_video), str(server_final))

        await websocket.send(json.dumps({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "completed",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        }))
    except asyncio.CancelledError:
        print(f"[Agent] Pipeline task for '{project_name}' was cancelled.")
        try:
            await websocket.send(json.dumps({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "stopped",
                "current_fragment": 0,
                "total_fragments": 0
            }))
        except Exception:
            pass
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
    finally:
        agent_active_tasks.pop(project_name, None)
        asyncio.create_task(process_next_queued_job())


async def run_music_pipeline_task(project_name: str, project_path_str: str, websocket, voice_config: dict = None, art_style: str = None, use_watermark: bool = False, use_subtitles: bool = False, subtitle_preset: str = "karaoke-green", use_whisper: bool = False, music_b64: str = None, music_filename: str = None, music_local_path: str = None, force_rerun: bool = False):
    """Executes the music-to-video pipeline by transcribing audio and generating images/subtitles."""
    try:
        # Resolve project folder relative to AGENT_DIR/projects to support remote server
        path_obj = pathlib.Path(project_path_str)
        story_id = path_obj.parent.name
        chapter_id = path_obj.name
        project_dir = AGENT_PROJECTS_DIR / story_id / chapter_id
        
        # Keep list of existing music files to restore
        existing_music = []
        if project_dir.exists():
            for item in project_dir.iterdir():
                if item.is_file() and item.suffix.lower() in [".mp3", ".wav", ".m4a"] and not item.name.startswith("processed_"):
                    try:
                        existing_music.append((item.name, item.read_bytes()))
                    except Exception:
                        pass

        if force_rerun:
            print(f"[Agent] force_rerun enabled: Deleting entire project folder {project_dir}")
            if project_dir.exists():
                shutil.rmtree(project_dir, ignore_errors=True)

        project_dir.mkdir(parents=True, exist_ok=True)
        
        # Save music file if sent by server or local path, or restore if it existed
        if music_local_path and os.path.exists(music_local_path):
            suffix = pathlib.Path(music_local_path).suffix or ".mp3"
            shutil.copy(music_local_path, project_dir / f"music{suffix}")
        elif music_b64 and music_filename:
            import base64
            with open(project_dir / music_filename, "wb") as f:
                f.write(base64.b64decode(music_b64))
        else:
            for name, data in existing_music:
                with open(project_dir / name, "wb") as f:
                    f.write(data)
                
        # Save project_config.json
        config_data = {
            "use_watermark": use_watermark,
            "use_subtitles": use_subtitles,
            "subtitle_preset": subtitle_preset,
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
        
        img_gen = image_generator or config_data.get("image_generator", "ima2")
        orig_sd_api = video_engine.USE_SD_API
        video_engine.USE_SD_API = img_gen

        try:
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
        finally:
            video_engine.USE_SD_API = orig_sd_api

        # 5. Render clips (MoviePy)
        for idx in range(num_frags):
            await send_ws_message({
                "type": "pipeline_progress",
                "project_name": project_name,
                "status": "compiling_clips",
                "current_fragment": idx,
                "total_fragments": num_frags,
                "fragment_status": {"idx": idx, "step": "clip"}
            })
            
            out_clip = project_dir / f"videos/video{idx}.mp4"
            if not out_clip.exists():
                await asyncio.to_thread(video_engine.create_video_clip, idx, project_dir)

        # 6. Final Concatenation and music assembly
        await send_ws_message({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "assembling_final_video",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        })
        
        await asyncio.to_thread(video_engine.make_final_music_video, project_name, project_dir, music_file_path, segments)
        if final_video.exists():
            shutil.copy(str(final_video), str(server_final))

        await send_ws_message({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "completed",
            "current_fragment": num_frags,
            "total_fragments": num_frags
        })
    except asyncio.CancelledError:
        print(f"[Agent] Music pipeline task for '{project_name}' was cancelled.")
        await send_ws_message({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "stopped",
            "current_fragment": 0,
            "total_fragments": 0
        })
    except Exception as e:
        print(f"[Agent] Music pipeline task failed: {e}")
        await send_ws_message({
            "type": "pipeline_progress",
            "project_name": project_name,
            "status": "failed",
            "error": str(e),
            "current_fragment": 0,
            "total_fragments": 0
        })
    finally:
        agent_active_tasks.pop(project_name, None)
        asyncio.create_task(process_next_queued_job())


def start_local_media_server():
    """Start a lightweight HTTP server on port 8766 serving AGENT_PROJECTS_DIR."""
    import http.server
    import socketserver
    import threading
    
    projects_dir = AGENT_PROJECTS_DIR
    projects_dir.mkdir(parents=True, exist_ok=True)

    class CORSHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(projects_dir), **kwargs)

        def translate_path(self, path):
            filepath = pathlib.Path(super().translate_path(path))
            if filepath.exists() and filepath.is_file():
                return str(filepath)
                
            # If not found, check image extension fallbacks (.jpg, .jpeg, .png, .webp)
            if filepath.parent.name == "images" or "images" in filepath.parts:
                stem = filepath.stem
                parent = filepath.parent
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    alt_img = parent / f"{stem}{ext}"
                    if alt_img.exists() and alt_img.is_file():
                        return str(alt_img)
                        
            # Check video name fallback
            if filepath.name == "final.mp4":
                chapter_dir = filepath.parent
                story_id = chapter_dir.parent.name
                chapter_id = chapter_dir.name
                alt_video = chapter_dir / f"{story_id}_{chapter_id}.mp4"
                if alt_video.exists() and alt_video.is_file():
                    return str(alt_video)
                    
            return str(filepath)

        def do_GET(self):
            if self.path.startswith("/v1/local/info") or self.path == "/v1/local/info":
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                info = {
                    "workspace_id": WORKSPACE_ID,
                    "agent_version": "0.4.4"
                }
                self.wfile.write(json.dumps(info).encode("utf-8"))
                return

            path = self.translate_path(self.path)
            fpath = pathlib.Path(path)
            if not fpath.exists() or not fpath.is_file():
                self.send_error(404, "File not found")
                return

            file_size = fpath.stat().st_size
            range_header = self.headers.get('Range')

            if range_header and range_header.startswith('bytes='):
                try:
                    ranges = range_header.split('=')[1].split('-')
                    start = int(ranges[0]) if ranges[0] else 0
                    end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1
                    if start >= file_size:
                        self.send_error(416, "Requested Range Not Satisfiable")
                        return
                    end = min(end, file_size - 1)
                    length = end - start + 1

                    import mimetypes
                    ctype, _ = mimetypes.guess_type(str(fpath))
                    if not ctype and str(fpath).endswith('.mp4'):
                        ctype = 'video/mp4'

                    self.send_response(206)
                    self.send_header('Content-Type', ctype or 'application/octet-stream')
                    self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                    self.send_header('Content-Length', str(length))
                    self.end_headers()

                    with open(fpath, 'rb') as f:
                        f.seek(start)
                        chunk_size = 64 * 1024
                        bytes_to_send = length
                        while bytes_to_send > 0:
                            read_size = min(chunk_size, bytes_to_send)
                            data = f.read(read_size)
                            if not data:
                                break
                            self.wfile.write(data)
                            bytes_to_send -= len(data)
                    return
                except Exception as ex:
                    pass

            return super().do_GET()

        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Accept-Ranges', 'bytes')
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()

    port = 8766
    
    def run_server():
        socketserver.TCPServer.allow_reuse_address = True
        try:
            with socketserver.TCPServer(("", port), CORSHTTPRequestHandler) as httpd:
                print(f"[Agent] Local media server started on port {port}. Root: {projects_dir}")
                httpd.serve_forever()
        except Exception as e:
            print(f"[Agent] Local media server port {port} error: {e} (might already be running).")

    t = threading.Thread(target=run_server, daemon=True)
    t.start()


async def start_local_websocket_server():
    """Start local WebSocket server on port 8767 for direct zero-cloud media streaming."""
    async def handle_client(ws):
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                    mtype = data.get("type")
                    req_id = data.get("request_id")
                    if mtype == "get_local_media":
                        story_id = data.get("story_id", "")
                        chapter_id = data.get("chapter_id", "")
                        file_path = data.get("file_path", "")
                        found_file = None
                        if AGENT_PROJECTS_DIR and AGENT_PROJECTS_DIR.exists():
                            for bdir in [AGENT_PROJECTS_DIR / story_id / chapter_id, AGENT_PROJECTS_DIR / chapter_id, AGENT_PROJECTS_DIR / story_id]:
                                found_file = resolve_local_media_file(bdir, file_path)
                                if found_file:
                                    break
                            if not found_file and chapter_id:
                                matches = list(AGENT_PROJECTS_DIR.glob(f"**/{chapter_id}"))
                                if matches:
                                    found_file = resolve_local_media_file(matches[0], file_path)

                        if found_file and found_file.exists():
                            import mimetypes
                            ctype, _ = mimetypes.guess_type(str(found_file))
                            if not ctype:
                                if str(found_file).endswith(".mp4"):
                                    ctype = "video/mp4"
                                elif str(found_file).endswith(".jpg") or str(found_file).endswith(".jpeg"):
                                    ctype = "image/jpeg"
                                elif str(found_file).endswith(".png"):
                                    ctype = "image/png"
                                elif str(found_file).endswith(".wav"):
                                    ctype = "audio/wav"
                                elif str(found_file).endswith(".mp3"):
                                    ctype = "audio/mpeg"
                                else:
                                    ctype = "application/octet-stream"

                            with open(found_file, "rb") as f:
                                file_bytes = f.read()

                            await ws.send(json.dumps({
                                "request_id": req_id,
                                "exists": True,
                                "size": len(file_bytes),
                                "content_type": ctype
                            }))
                            await ws.send(file_bytes)
                        else:
                            await ws.send(json.dumps({"request_id": req_id, "exists": False}))
                except Exception as ex:
                    pass
        except Exception:
            pass

    try:
        server = await websockets.serve(handle_client, "127.0.0.1", 8767, max_size=100 * 1024 * 1024)
        print("[Agent] Direct Local WebSocket Media Server started on ws://127.0.0.1:8767")
    except Exception as e:
        print(f"[Agent] Local WS Port 8767 error: {e}")


async def main():
    global active_websocket
    start_local_media_server()
    asyncio.create_task(start_local_websocket_server())
    
    ws_base = SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")
    if "localhost" in SERVER_URL or "127.0.0.1" in SERVER_URL:
        ws_base = SERVER_URL.replace("http://", "ws://").replace("https://", "ws://")
    current_ws_url = f"{ws_base}/v1/system/agent/ws?workspace_id={WORKSPACE_ID}"
    print(f"[Agent] Starting Taka-Agent ({WORKSPACE_ID}). Connecting to server {current_ws_url}...")
    
    while True:
        try:
            async with websockets.connect(current_ws_url, ping_interval=15, ping_timeout=15, max_size=100 * 1024 * 1024) as websocket:
                print("[Agent] Connected to Taka Server successfully.")
                active_websocket = websocket
                
                async def heartbeat_loop(ws):
                    while True:
                        try:
                            await asyncio.sleep(12)
                            await ws.send(json.dumps({"type": "heartbeat"}))
                        except Exception:
                            break
                            
                heartbeat_task = asyncio.create_task(heartbeat_loop(websocket))
                
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

                try:
                    async for message_str in websocket:
                        try:
                            message = json.loads(message_str)
                        except json.JSONDecodeError:
                            continue

                        msg_type = message.get("type")
                        payload = message.get("payload", {})
                        if msg_type == "heartbeat":
                            continue

                        if msg_type == "run_pipeline":
                            project_name = payload.get("project_name")
                            project_path_str = payload.get("project_path")
                            voice_config = payload.get("voice_config")
                            pipeline_type = payload.get("pipeline_type", "story")
                            print(f"[Agent] Received run_pipeline message. project_name={project_name}, pipeline_type={pipeline_type}, voice_config: { {k: (v[:30]+'...' if isinstance(v, str) and len(v) > 30 else v) for k, v in voice_config.items() if k != 'ref_audio_b64'} if voice_config else None}")
                            art_style = payload.get("art_style")
                            use_watermark = payload.get("use_watermark", True)
                            use_waveform = payload.get("use_waveform", True)
                            use_subtitles = payload.get("use_subtitles", True)
                            subtitle_preset = payload.get("subtitle_preset", "viral-bold-yellow")
                            use_whisper = payload.get("use_whisper", False)
                            force_rerun = payload.get("force_rerun", False)
                            
                            image_generator = payload.get("image_generator", "ima2")
                            rerun_mode = payload.get("rerun_mode", "all")
                            aspect_ratio = payload.get("aspect_ratio")
                            
                            story_text = payload.get("story_text")
                            effect_type = payload.get("effect_type", "leaves")
                            music_b64 = payload.get("music_b64")
                            music_filename = payload.get("music_filename")
                            music_local_path = payload.get("music_local_path")

                            await enqueue_or_run_job(
                                project_name, project_path_str, websocket, voice_config, art_style,
                                use_watermark=use_watermark, use_waveform=use_waveform,
                                use_subtitles=use_subtitles, subtitle_preset=subtitle_preset,
                                use_whisper=use_whisper, story_text=story_text, force_rerun=force_rerun,
                                effect_type=effect_type, pipeline_type=pipeline_type,
                                music_b64=music_b64, music_filename=music_filename, music_local_path=music_local_path,
                                image_generator=image_generator, rerun_mode=rerun_mode, aspect_ratio=aspect_ratio
                            )
                        elif msg_type == "delete_project_request":
                            request_id = message.get("request_id")
                            story_id = payload.get("story_id", "").strip()
                            chapter_id = payload.get("chapter_id")
                            
                            search_patterns = [story_id]
                            if chapter_id:
                                search_patterns.append(f"{story_id}/{chapter_id}")
                                search_patterns.append(f"{story_id}_{chapter_id}")

                            remove_from_queue_and_active(story_id, chapter_id)
                            
                            # Clear running jobs state
                            job_keys = [k for k in agent_running_jobs.keys() if any(k == p or k.startswith(f"{p}/") or k.startswith(f"{p}_") for p in search_patterns)]
                            for k in job_keys:
                                agent_running_jobs.pop(k, None)

                            # 2. Delete project directory on Agent
                            dirs_to_check = []
                            if chapter_id and chapter_id != "story":
                                dirs_to_check.append(AGENT_PROJECTS_DIR / story_id / chapter_id)
                                dirs_to_check.append(AGENT_PROJECTS_DIR / "dao-ly" / chapter_id)
                                dirs_to_check.append(AGENT_PROJECTS_DIR / "dao_ly" / chapter_id)
                                dirs_to_check.append(AGENT_PROJECTS_DIR / chapter_id)
                            else:
                                dirs_to_check.append(AGENT_PROJECTS_DIR / story_id)

                            for d in dirs_to_check:
                                if d.exists():
                                    parent_dir = d.parent
                                    try:
                                        shutil.rmtree(d, ignore_errors=True)
                                        print(f"[Agent] Successfully deleted agent folder: {d}")
                                        update_reels_content_json(parent_dir)
                                    except Exception as ex:
                                        print(f"[Agent] Failed to delete agent folder {d}: {ex}")

                            for cat in ("dao-ly", "dao_ly", "music", story_id):
                                cat_dir = AGENT_PROJECTS_DIR / cat
                                if cat_dir.exists() and cat_dir.is_dir() and not any(p for p in cat_dir.iterdir() if not p.name.startswith(".")):
                                    shutil.rmtree(cat_dir, ignore_errors=True)

                            await websocket.send(json.dumps({
                                "type": "delete_project_response",
                                "request_id": request_id,
                                "payload": {"ok": True, "story_id": story_id, "chapter_id": chapter_id}
                            }))
                        elif msg_type in ("cancel_chapter_job_request", "cancel_all_jobs_request"):
                            request_id = message.get("request_id")
                            c_story = payload.get("story_id") if payload else None
                            c_chap = payload.get("chapter_id") if payload else None
                            target_key = f"{c_story}/{c_chap}" if (c_story and c_chap) else None

                            for k, t in list(agent_active_tasks.items()):
                                is_match = (not c_story or c_story in k) and (not c_chap or c_chap in k)
                                if is_match or target_key is None:
                                    if t and not t.done():
                                        print(f"[Agent] Cancelling active task: {k}")
                                        t.cancel()
                                    agent_active_tasks.pop(k, None)

                            if target_key:
                                agent_queued_jobs.pop(target_key, None)
                            else:
                                agent_active_tasks.clear()
                                agent_queued_jobs.clear()
                                while not pipeline_queue.empty():
                                    try:
                                        pipeline_queue.get_nowait()
                                    except Exception:
                                        break

                            # Cancel ALL active ima2-gen jobs and kill child CLI processes
                            try:
                                subprocess.run(["pkill", "-f", "ima2 gen"], capture_output=True)
                                res = subprocess.run(["ima2", "ps", "--json"], capture_output=True, text=True, timeout=5)
                                if res.returncode == 0 and res.stdout.strip():
                                    data = json.loads(res.stdout)
                                    for job in data.get("jobs", []):
                                        req_id = job.get("requestId")
                                        if req_id:
                                            subprocess.run(["ima2", "cancel", req_id], capture_output=True, timeout=5)
                                            print(f"[Agent] Cancelled ima2-gen job: {req_id}")
                            except Exception as e:
                                print(f"[Agent] Warning: Failed to query/cancel ima2 jobs: {e}")

                            await websocket.send(json.dumps({
                                "type": "cancel_chapter_job_response" if msg_type == "cancel_chapter_job_request" else "cancel_all_jobs_response",
                                "request_id": request_id,
                                "payload": {"ok": True}
                            }))
                        elif msg_type == "select_file_request":
                            request_id = message.get("request_id")
                            prompt = payload.get("prompt", "Select a file")
                            
                            def pick_file():
                                import sys, os, subprocess
                                clean_prompt = prompt.replace('"', '').replace("'", "").replace("\n", " ").strip() or "Select a file"

                                if sys.platform == "darwin":
                                    try:
                                        script = f'POSIX path of (choose file with prompt "{clean_prompt}")'
                                        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
                                        if proc.returncode == 0 and proc.stdout.strip():
                                            return proc.stdout.strip()
                                    except Exception as ex:
                                        print(f"[Agent] Error choosing file on macOS: {ex}")

                                if sys.platform.startswith("win"):
                                    try:
                                        ps_cmd = (
                                            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
                                            "$f = New-Object System.Windows.Forms.OpenFileDialog; "
                                            f"$f.Title = '{clean_prompt}'; "
                                            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.FileName }"
                                        )
                                        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=60)
                                        if proc.returncode == 0 and proc.stdout.strip():
                                            return proc.stdout.strip()
                                    except Exception as ex:
                                        print(f"[Agent] Error choosing file on Windows: {ex}")

                                if not (sys.platform.startswith("linux") and not os.environ.get("DISPLAY")):
                                    try:
                                        import tkinter as tk
                                        from tkinter import filedialog
                                        root = tk.Tk()
                                        root.withdraw()
                                        root.attributes("-topmost", True)
                                        selected = filedialog.askopenfilename(title=clean_prompt)
                                        root.destroy()
                                        if selected:
                                            return selected
                                    except Exception as ex:
                                        print(f"[Agent] Tkinter error: {ex}")

                                return ""
                            
                            selected_path = await asyncio.to_thread(pick_file)
                            await websocket.send(json.dumps({
                                "type": "select_file_response",
                                "request_id": request_id,
                                "payload": {"path": selected_path}
                            }))
                        elif msg_type == "list_projects_request":
                            request_id = message.get("request_id")
                            story_folders = set()
                            local_files = {}
                            projects_metadata = {}
                            
                            search_dirs = [AGENT_PROJECTS_DIR]
                            for projects_dir in search_dirs:
                                if projects_dir and projects_dir.exists():
                                    for item in projects_dir.iterdir():
                                        if item.is_dir() and not item.name.startswith(".") and item.name not in ("test_project_1", "affiliate"):
                                            story_folders.add(item.name)
                                            content_file = item / "content.json"
                                            if content_file.exists():
                                                try:
                                                    with open(content_file, "r", encoding="utf-8") as f:
                                                        projects_metadata[item.name] = json.load(f)
                                                except Exception:
                                                    pass
                                            for ch_dir in item.iterdir():
                                                if ch_dir.name.startswith("."):
                                                    continue
                                                if ch_dir.is_dir():
                                                    ch_id = ch_dir.name
                                                    key = f"{item.name}/{ch_id}"
                                                    if key not in local_files:
                                                        local_files[key] = {
                                                            "has_story": (ch_dir / "story.txt").exists(),
                                                            "has_video": (ch_dir / "final.mp4").exists() or (ch_dir / f"{item.name}_{ch_id}.mp4").exists()
                                                        }
                                                elif ch_dir.is_file() and ch_dir.name.endswith(".json") and ch_dir.name not in ("content.json", "index.json", "project_config.json", "branding.json"):
                                                    ch_id = ch_dir.stem
                                                    key = f"{item.name}/{ch_id}"
                                                    if key not in local_files:
                                                        local_files[key] = {
                                                            "has_story": True,
                                                            "has_video": (item / f"{ch_id}.mp4").exists() or (item / f"{item.name}_{ch_id}.mp4").exists()
                                                        }
                            await websocket.send(json.dumps({
                                "type": "list_projects_response",
                                "request_id": request_id,
                                "payload": {
                                    "story_folders": list(story_folders),
                                    "local_files": local_files,
                                    "projects_metadata": projects_metadata
                                }
                            }))
                        elif msg_type == "get_fragments_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = (payload.get("story_id") or "").strip()
                            chapter_id = (payload.get("chapter_id") or "").strip()
                            
                            target_dir = None
                            if AGENT_PROJECTS_DIR and AGENT_PROJECTS_DIR.exists():
                                cands = [
                                    AGENT_PROJECTS_DIR / story_id / chapter_id,
                                    AGENT_PROJECTS_DIR / "reels" / chapter_id,
                                    AGENT_PROJECTS_DIR / "dao-ly" / chapter_id,
                                    AGENT_PROJECTS_DIR / chapter_id,
                                    AGENT_PROJECTS_DIR / story_id
                                ]
                                for cand in cands:
                                    if cand.exists() and cand.is_dir():
                                        target_dir = cand
                                        break
                                if not target_dir and chapter_id:
                                    matches = list(AGENT_PROJECTS_DIR.glob(f"**/{chapter_id}"))
                                    if matches:
                                        target_dir = matches[0]

                            frags_result = []
                            if target_dir and target_dir.exists():
                                frag_dir = target_dir / "text" / "story_fragments"
                                frag_files = sorted([f for f in frag_dir.glob("*.txt")], key=lambda f: int(re.search(r'\d+', f.stem).group()) if re.search(r'\d+', f.stem) else 9999) if (frag_dir.exists() and frag_dir.is_dir()) else []
                                img_dir = target_dir / "images"
                                aud_dir = target_dir / "audio"
                                vid_dir = target_dir / "videos"
                                configured_aspect_ratio = None
                                if (target_dir / "aspect_ratio.txt").exists():
                                    try:
                                        configured_aspect_ratio = (target_dir / "aspect_ratio.txt").read_text(encoding="utf-8").strip()
                                    except Exception:
                                        pass

                                if not configured_aspect_ratio and (target_dir / "project_config.json").exists():
                                    try:
                                        with open(target_dir / "project_config.json", "r", encoding="utf-8") as f:
                                            cfg = json.load(f)
                                            configured_aspect_ratio = cfg.get("aspect_ratio")
                                    except Exception:
                                        pass

                                if not configured_aspect_ratio and (AGENT_PROJECTS_DIR / story_id / "content.json").exists():
                                    try:
                                        with open(AGENT_PROJECTS_DIR / story_id / "content.json", "r", encoding="utf-8") as f:
                                            cdata = json.load(f)
                                            items = cdata.get("items", [])
                                            for it in items:
                                                if isinstance(it, dict) and (it.get("slug") == chapter_id or it.get("id") == chapter_id):
                                                    configured_aspect_ratio = it.get("aspect_ratio")
                                                    break
                                            if not configured_aspect_ratio and isinstance(cdata, dict):
                                                configured_aspect_ratio = cdata.get("aspect_ratio")
                                    except Exception:
                                        pass

                                if not configured_aspect_ratio:
                                    configured_aspect_ratio = "16:9"

                                if not frag_files:
                                    story_txt = target_dir / "story.txt"
                                    if story_txt.exists():
                                        txt_content = story_txt.read_text(encoding="utf-8").strip()
                                        if txt_content:
                                            try:
                                                from core import video_engine
                                                video_engine.prepare_chapter_structure(story_id, chapter_id, txt_content, chapter_dir=target_dir)
                                                frag_dir = target_dir / "text" / "story_fragments"
                                                if frag_dir.exists():
                                                    frag_files = sorted([f for f in frag_dir.glob("*.txt")], key=lambda f: int(re.search(r'\d+', f.stem).group()) if re.search(r'\d+', f.stem) else 9999)
                                            except Exception as ex:
                                                print(f"[Agent] Failed auto-generating fragments: {ex}")

                                if not frag_files:
                                    story_txt = target_dir / "story.txt"
                                    if story_txt.exists():
                                        txt_content = story_txt.read_text(encoding="utf-8").strip()
                                        lines = [l.strip() for l in txt_content.split("\n") if l.strip()]
                                        for i, l in enumerate(lines):
                                            frags_result.append({"index": i, "text": l})
                                else:
                                    from PIL import Image
                                    ws_suffix = f"?ws={WORKSPACE_ID}" if WORKSPACE_ID else ""
                                    for i, ff in enumerate(frag_files):
                                        text = ff.read_text(encoding="utf-8").strip() if ff.exists() else ""
                                        frag_idx = int(re.search(r'\d+', ff.stem).group()) if re.search(r'\d+', ff.stem) else i
                                        item = {"index": frag_idx, "text": text}
                                        
                                        img_url = None
                                        img_width, img_height = None, None
                                        aspect_mismatch = False

                                        if img_dir.exists() and img_dir.is_dir():
                                            for img_stem in [f"image{frag_idx}", f"image_{frag_idx}", f"frame{frag_idx}", f"frame_{frag_idx}", str(frag_idx)]:
                                                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                                                    found_img_path = img_dir / f"{img_stem}{ext}"
                                                    if found_img_path.exists():
                                                        img_url = f"/v1/media/{story_id}/{chapter_id}/images/{img_stem}{ext}{ws_suffix}"
                                                        try:
                                                            with Image.open(found_img_path) as img:
                                                                img_width, img_height = img.size
                                                            if img_width and img_height:
                                                                actual_ratio = img_width / img_height
                                                                target_ratio = 16.0 / 9.0
                                                                if configured_aspect_ratio == "9:16":
                                                                    target_ratio = 9.0 / 16.0
                                                                elif configured_aspect_ratio == "1:1":
                                                                    target_ratio = 1.0
                                                                elif configured_aspect_ratio == "4:3":
                                                                    target_ratio = 4.0 / 3.0
                                                                elif configured_aspect_ratio == "3:4":
                                                                    target_ratio = 3.0 / 4.0
                                                                elif configured_aspect_ratio == "4:5":
                                                                    target_ratio = 4.0 / 5.0
                                                                elif configured_aspect_ratio == "21:9":
                                                                    target_ratio = 21.0 / 9.0

                                                                if configured_aspect_ratio == "16:9":
                                                                    if actual_ratio < 1.25:
                                                                        aspect_mismatch = True
                                                                elif configured_aspect_ratio == "9:16":
                                                                    if actual_ratio > 0.80:
                                                                        aspect_mismatch = True
                                                                elif configured_aspect_ratio == "1:1":
                                                                    if actual_ratio < 0.85 or actual_ratio > 1.15:
                                                                        aspect_mismatch = True
                                                                elif configured_aspect_ratio in ("4:3", "21:9"):
                                                                    if actual_ratio < 1.15:
                                                                        aspect_mismatch = True
                                                                elif configured_aspect_ratio in ("3:4", "4:5"):
                                                                    if actual_ratio > 0.90:
                                                                        aspect_mismatch = True
                                                                else:
                                                                    if abs(actual_ratio - target_ratio) / target_ratio > 0.15:
                                                                        aspect_mismatch = True
                                                        except Exception:
                                                            pass
                                                        break
                                                if img_url: break
                                        
                                        aud_url = None
                                        if aud_dir.exists() and aud_dir.is_dir():
                                            for aud_stem in [f"processed_voiceover{frag_idx}", f"processed_voiceover_{frag_idx}", f"voiceover{frag_idx}", f"voiceover_{frag_idx}", f"voice{frag_idx}", f"voice_{frag_idx}", f"audio{frag_idx}", f"audio_{frag_idx}", str(frag_idx)]:
                                                for ext in [".mp3", ".wav", ".m4a"]:
                                                    if (aud_dir / f"{aud_stem}{ext}").exists():
                                                        aud_url = f"/v1/media/{story_id}/{chapter_id}/audio/{aud_stem}{ext}{ws_suffix}"
                                                        break
                                                if aud_url: break
                                                
                                        vid_url = None
                                        if vid_dir.exists() and vid_dir.is_dir():
                                            for vid_stem in [f"clip{frag_idx}", f"clip_{frag_idx}", f"video{frag_idx}", f"video_{frag_idx}", str(frag_idx)]:
                                                for ext in [".mp4", ".mov", ".webm"]:
                                                    if (vid_dir / f"{vid_stem}{ext}").exists():
                                                        vid_url = f"/v1/media/{story_id}/{chapter_id}/videos/{vid_stem}{ext}{ws_suffix}"
                                                        break
                                                if vid_url: break

                                        item["image_url"] = img_url
                                        item["image_width"] = img_width
                                        item["image_height"] = img_height
                                        item["aspect_mismatch"] = aspect_mismatch
                                        item["configured_aspect_ratio"] = configured_aspect_ratio
                                        item["audio_url"] = aud_url
                                        item["video_url"] = vid_url
                                        frags_result.append(item)
                                        
                                    frags_result.sort(key=lambda x: x["index"])
                                        
                            await websocket.send(json.dumps({
                                "type": "get_fragments_response",
                                "request_id": request_id,
                                "payload": {"fragments": frags_result}
                            }))

                        elif msg_type == "open_folder_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = (payload.get("story_id") or "").strip()
                            chapter_id = payload.get("chapter_id")
                            
                            target_dir = None
                            if AGENT_PROJECTS_DIR and AGENT_PROJECTS_DIR.exists():
                                if chapter_id and chapter_id != "story":
                                    cands = [
                                        AGENT_PROJECTS_DIR / story_id / chapter_id,
                                        AGENT_PROJECTS_DIR / "reels" / chapter_id,
                                        AGENT_PROJECTS_DIR / "dao-ly" / chapter_id,
                                        AGENT_PROJECTS_DIR / story_id
                                    ]
                                else:
                                    cands = [
                                        AGENT_PROJECTS_DIR / story_id,
                                        AGENT_PROJECTS_DIR / "reels" / story_id,
                                        AGENT_PROJECTS_DIR / "dao-ly" / story_id
                                    ]
                                for cand in cands:
                                    if cand.exists():
                                        target_dir = cand
                                        break
                                if not target_dir:
                                    query = chapter_id if (chapter_id and chapter_id != "story") else story_id
                                    matches = list(AGENT_PROJECTS_DIR.glob(f"**/{query}"))
                                    if matches:
                                        target_dir = matches[0]
                                    else:
                                        target_dir = AGENT_PROJECTS_DIR / story_id
                                        target_dir.mkdir(parents=True, exist_ok=True)
                            
                            if target_dir and target_dir.exists():
                                try:
                                    if sys.platform == "darwin":
                                        subprocess.Popen(["open", str(target_dir)])
                                    elif sys.platform == "win32":
                                        subprocess.Popen(["explorer", str(target_dir)])
                                    else:
                                        if shutil.which("xdg-open"):
                                            subprocess.Popen(["xdg-open", str(target_dir)])
                                    await websocket.send(json.dumps({
                                        "type": "open_folder_response",
                                        "request_id": request_id,
                                        "payload": {"status": "ok", "message": f"Opened folder: {target_dir}", "path": str(target_dir)}
                                    }))
                                except Exception as ex:
                                    await websocket.send(json.dumps({
                                        "type": "open_folder_response",
                                        "request_id": request_id,
                                        "payload": {"status": "error", "error": str(ex)}
                                    }))
                            else:
                                await websocket.send(json.dumps({
                                    "type": "open_folder_response",
                                    "request_id": request_id,
                                    "payload": {"status": "error", "error": "Folder not found"}
                                }))

                        elif msg_type == "delete_project_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = payload.get("story_id", "")
                            raw_id = payload.get("raw_id", "")
                            chapter_id = payload.get("chapter_id")
                            for sid in (story_id, raw_id):
                                if sid:
                                    remove_from_queue_and_active(sid, chapter_id)
                                    if chapter_id and chapter_id != "story":
                                        target_dir = AGENT_PROJECTS_DIR / sid / chapter_id
                                    else:
                                        target_dir = AGENT_PROJECTS_DIR / sid
                                    if target_dir.exists():
                                        shutil.rmtree(target_dir, ignore_errors=True)
                                        print(f"[Agent] Deleted project folder: {target_dir}")
                            await websocket.send(json.dumps({
                                "type": "delete_project_response",
                                "request_id": request_id,
                                "payload": {"ok": True}
                            }))

                        elif msg_type == "get_chapter_status_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = payload.get("story_id", "")
                            chapter_id = payload.get("chapter_id", "")
                            
                            projects_dir = AGENT_PROJECTS_DIR
                            ch_dir = projects_dir / story_id / chapter_id
                            has_video = (ch_dir / "final.mp4").exists() or (ch_dir / f"{story_id}_{chapter_id}.mp4").exists()
                            
                            img_dir = ch_dir / "images"
                            aud_dir = ch_dir / "audio"
                            has_images = img_dir.exists() and img_dir.is_dir() and len([f for f in img_dir.iterdir() if not f.name.startswith(".") and f.is_file()]) > 0
                            has_audio = aud_dir.exists() and aud_dir.is_dir() and len([f for f in aud_dir.iterdir() if not f.name.startswith(".") and f.is_file()]) > 0
                            
                            max_frags = 0
                            for sub_path in ["images", "audio", "videos", "text/story_fragments", "text/image_prompts"]:
                                d = ch_dir / sub_path
                                if d.exists() and d.is_dir():
                                    count = len([f for f in d.iterdir() if not f.name.startswith(".") and not f.name.startswith("processed_") and not f.is_dir()])
                                    if count > max_frags:
                                        max_frags = count
                                        
                            proj_key_1 = f"{story_id}_{chapter_id}"
                            proj_key_2 = f"{story_id}/{chapter_id}"
                            
                            queued_info = agent_queued_jobs.get(proj_key_1) or agent_queued_jobs.get(proj_key_2) or agent_queued_jobs.get(chapter_id)
                            job_info = agent_running_jobs.get(proj_key_1) or agent_running_jobs.get(proj_key_2) or agent_running_jobs.get(story_id)
                            is_active = (proj_key_1 in agent_active_tasks or proj_key_2 in agent_active_tasks or chapter_id in agent_active_tasks or story_id in agent_active_tasks)
                            
                            current_status = "completed" if has_video else "idle"
                            queue_pos = 0
                            if queued_info:
                                current_status = "queued"
                                queue_pos = queued_info.get("position", 1)
                            elif is_active:
                                current_status = "processing"
                            elif job_info and job_info.get("status") not in ("completed", "failed", "idle", None):
                                current_status = job_info.get("status")

                            art_style = None
                            subtitle_preset = None
                            aspect_ratio = None
                            use_watermark = True
                            use_subtitles = True
                            use_waveform = True
                            image_generator = "ima2"

                            cfg_file = ch_dir / "project_config.json"
                            if cfg_file.exists():
                                try:
                                    with open(cfg_file, "r", encoding="utf-8") as f:
                                        cfg = json.load(f)
                                        art_style = cfg.get("art_style")
                                        subtitle_preset = cfg.get("subtitle_preset")
                                        aspect_ratio = cfg.get("aspect_ratio")
                                        if "use_watermark" in cfg: use_watermark = cfg.get("use_watermark")
                                        if "use_subtitles" in cfg: use_subtitles = cfg.get("use_subtitles")
                                        if "use_waveform" in cfg: use_waveform = cfg.get("use_waveform")
                                        if "image_generator" in cfg: image_generator = cfg.get("image_generator")
                                except Exception:
                                    pass

                            if not aspect_ratio and (ch_dir / "aspect_ratio.txt").exists():
                                try:
                                    aspect_ratio = (ch_dir / "aspect_ratio.txt").read_text(encoding="utf-8").strip()
                                except Exception:
                                    pass

                            if not aspect_ratio and (projects_dir / story_id / "content.json").exists():
                                try:
                                    with open(projects_dir / story_id / "content.json", "r", encoding="utf-8") as f:
                                        cdata = json.load(f)
                                        items = cdata.get("items", [])
                                        for it in items:
                                            if isinstance(it, dict) and (it.get("slug") == chapter_id or it.get("id") == chapter_id):
                                                aspect_ratio = it.get("aspect_ratio")
                                                if not art_style and it.get("art_style"): art_style = it.get("art_style")
                                                break
                                except Exception:
                                    pass

                            status_res = {
                                "status": current_status,
                                "queue_position": queue_pos,
                                "total_queued": len(agent_queued_jobs),
                                "total_fragments": job_info.get("total_fragments", max_frags) if job_info else max_frags,
                                "current_fragment": job_info.get("current_fragment", max_frags if has_video else 0) if job_info else (max_frags if has_video else 0),
                                "has_video": has_video,
                                "has_images": has_images,
                                "has_audio": has_audio,
                                "art_style": art_style,
                                "subtitle_preset": subtitle_preset,
                                "aspect_ratio": aspect_ratio,
                                "use_watermark": use_watermark,
                                "use_subtitles": use_subtitles,
                                "use_waveform": use_waveform,
                                "image_generator": image_generator
                            }
                            
                            await websocket.send(json.dumps({
                                "type": "get_chapter_status_response",
                                "request_id": request_id,
                                "payload": status_res
                            }))

                        elif msg_type == "save_project_config_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = payload.get("story_id", "")
                            chapter_id = payload.get("chapter_id", "")
                            cfg_data = payload.get("config", {})

                            ch_dir = AGENT_PROJECTS_DIR / story_id / chapter_id
                            ch_dir.mkdir(parents=True, exist_ok=True)

                            cfg_path = ch_dir / "project_config.json"
                            existing_cfg = {}
                            if cfg_path.exists():
                                try:
                                    with open(cfg_path, "r", encoding="utf-8") as f:
                                        existing_cfg = json.load(f)
                                except Exception:
                                    pass
                            existing_cfg.update(cfg_data)
                            try:
                                with open(cfg_path, "w", encoding="utf-8") as f:
                                    json.dump(existing_cfg, f, ensure_ascii=False, indent=2)
                                if "aspect_ratio" in existing_cfg:
                                    with open(ch_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
                                        f.write(str(existing_cfg["aspect_ratio"]))
                            except Exception as ex:
                                print(f"[Agent] Error saving project_config.json: {ex}")

                            await websocket.send(json.dumps({
                                "type": "save_project_config_response",
                                "request_id": request_id,
                                "payload": {"status": "ok"}
                            }))

                        elif msg_type == "get_media_file_request":
                            request_id = message.get("request_id")
                            payload = message.get("payload", {})
                            story_id = (payload.get("story_id") or "").strip()
                            chapter_id = (payload.get("chapter_id") or "").strip()
                            file_path = (payload.get("file_path") or "").strip()
                            
                            found_file = None
                            if AGENT_PROJECTS_DIR and AGENT_PROJECTS_DIR.exists():
                                cands = [
                                    AGENT_PROJECTS_DIR / story_id / chapter_id,
                                    AGENT_PROJECTS_DIR / "reels" / chapter_id,
                                    AGENT_PROJECTS_DIR / "dao-ly" / chapter_id,
                                    AGENT_PROJECTS_DIR / chapter_id,
                                    AGENT_PROJECTS_DIR / story_id
                                ]
                                for bdir in cands:
                                    found_file = resolve_local_media_file(bdir, file_path)
                                    if found_file:
                                        break

                                if not found_file and chapter_id:
                                    matches = list(AGENT_PROJECTS_DIR.glob(f"**/{chapter_id}"))
                                    if matches:
                                        found_file = resolve_local_media_file(matches[0], file_path)

                            if found_file:
                                import base64
                                import mimetypes
                                content_type, _ = mimetypes.guess_type(str(found_file))
                                if not content_type:
                                    if str(found_file).endswith(".mp4"):
                                        content_type = "video/mp4"
                                    elif str(found_file).endswith(".jpg") or str(found_file).endswith(".jpeg"):
                                        content_type = "image/jpeg"
                                    elif str(found_file).endswith(".png"):
                                        content_type = "image/png"
                                    elif str(found_file).endswith(".wav"):
                                        content_type = "audio/wav"
                                    elif str(found_file).endswith(".mp3"):
                                        content_type = "audio/mpeg"
                                    else:
                                        content_type = "application/octet-stream"

                                file_size = found_file.stat().st_size
                                range_hdr = payload.get("range")
                                if payload.get("head_only"):
                                    res_payload = {
                                        "exists": True,
                                        "size": file_size,
                                        "content_type": content_type,
                                        "filename": found_file.name
                                    }
                                else:
                                    import base64
                                    try:
                                        with open(found_file, "rb") as f:
                                            max_chunk = 2 * 1024 * 1024  # 2MB max chunk per WebSocket frame
                                            if range_hdr and range_hdr.startswith("bytes="):
                                                try:
                                                    r_parts = range_hdr.split("=")[1].split("-")
                                                    start = int(r_parts[0]) if r_parts[0] else 0
                                                    end = int(r_parts[1]) if len(r_parts) > 1 and r_parts[1] else min(start + max_chunk - 1, file_size - 1)
                                                    start = max(0, min(start, file_size - 1))
                                                    end = max(start, min(end, file_size - 1))
                                                    f.seek(start)
                                                    chunk_data = f.read(end - start + 1)
                                                    data_b64 = base64.b64encode(chunk_data).decode("utf-8")
                                                    res_payload = {
                                                        "exists": True,
                                                        "content_b64": data_b64,
                                                        "content_type": content_type,
                                                        "size": file_size,
                                                        "start": start,
                                                        "end": start + len(chunk_data) - 1,
                                                        "partial": True,
                                                        "filename": found_file.name
                                                    }
                                                except Exception:
                                                    start = 0
                                                    end = min(max_chunk - 1, file_size - 1)
                                                    f.seek(start)
                                                    chunk_data = f.read(end - start + 1)
                                                    data_b64 = base64.b64encode(chunk_data).decode("utf-8")
                                                    res_payload = {
                                                        "exists": True,
                                                        "content_b64": data_b64,
                                                        "content_type": content_type,
                                                        "size": file_size,
                                                        "start": start,
                                                        "end": start + len(chunk_data) - 1,
                                                        "partial": (file_size > max_chunk),
                                                        "filename": found_file.name
                                                    }
                                            elif file_size > max_chunk:
                                                start = 0
                                                end = min(max_chunk - 1, file_size - 1)
                                                f.seek(start)
                                                chunk_data = f.read(end - start + 1)
                                                data_b64 = base64.b64encode(chunk_data).decode("utf-8")
                                                res_payload = {
                                                    "exists": True,
                                                    "content_b64": data_b64,
                                                    "content_type": content_type,
                                                    "size": file_size,
                                                    "start": start,
                                                    "end": start + len(chunk_data) - 1,
                                                    "partial": True,
                                                    "filename": found_file.name
                                                }
                                            else:
                                                data_b64 = base64.b64encode(f.read()).decode("utf-8")
                                                res_payload = {
                                                    "exists": True,
                                                    "content_b64": data_b64,
                                                    "content_type": content_type,
                                                    "size": file_size,
                                                    "filename": found_file.name
                                                }
                                    except Exception as read_err:
                                        res_payload = {"exists": False, "error": str(read_err)}
                            else:
                                res_payload = {"exists": False}

                            await websocket.send(json.dumps({
                                "type": "get_media_file_response",
                                "request_id": request_id,
                                "payload": res_payload
                            }))

                        elif msg_type == "list_voices_request":
                            request_id = message.get("request_id")
                            voices_list = []
                            voices_base = AGENT_VOICES_DIR
                            if voices_base.exists():
                                for item in voices_base.iterdir():
                                    if item.is_dir():
                                        sync_and_migrate_voice_dir(item)
                                        voice_id = item.name
                                        has_audio = (item / "ref.wav").exists() or (item / "local_path.txt").exists()
                                        has_text = (item / "ref_text.txt").exists() or (item / "ref.txt").exists()
                                        voices_list.append({
                                            "id": voice_id,
                                            "name": voice_id,
                                            "has_audio": has_audio,
                                            "has_text": has_text
                                        })
                            await websocket.send(json.dumps({
                                "type": "list_voices_response",
                                "request_id": request_id,
                                "payload": {"voices": voices_list}
                            }))

                        elif msg_type == "save_voice_request":
                            request_id = message.get("request_id")
                            voice_id = payload.get("voice_id")
                            ref_text = payload.get("ref_text", "")
                            local_path = payload.get("local_path", "")
                            ref_audio_b64 = payload.get("ref_audio_b64")
                            
                            voices_base = AGENT_VOICES_DIR
                            voice_dir = voices_base / voice_id
                            voice_dir.mkdir(parents=True, exist_ok=True)
                            
                            out_b64 = ref_audio_b64
                            if ref_audio_b64:
                                import base64
                                with open(voice_dir / "ref.wav", "wb") as buffer:
                                    buffer.write(base64.b64decode(ref_audio_b64))
                                local_path_file = voice_dir / "local_path.txt"
                                if local_path_file.exists():
                                    local_path_file.unlink()
                            elif local_path.strip():
                                src_path = pathlib.Path(local_path.strip())
                                if src_path.exists():
                                    import base64
                                    ext = src_path.suffix.lower() or ".wav"
                                    dest_file = voice_dir / f"ref{ext}"
                                    shutil.copy2(str(src_path), str(dest_file))
                                    if ext != ".wav":
                                        dest_wav = voice_dir / "ref.wav"
                                        shutil.copy2(str(src_path), str(dest_wav))
                                    local_path_file = voice_dir / "local_path.txt"
                                    if local_path_file.exists():
                                        local_path_file.unlink()
                                    try:
                                        ref_audio_target = voice_dir / "ref.wav"
                                        if ref_audio_target.exists():
                                            with open(ref_audio_target, "rb") as bf:
                                                out_b64 = base64.b64encode(bf.read()).decode("utf-8")
                                    except Exception:
                                        pass
                                else:
                                    with open(voice_dir / "local_path.txt", "w", encoding="utf-8") as f:
                                        f.write(local_path.strip())
                                    
                            if ref_text.strip():
                                with open(voice_dir / "ref_text.txt", "w", encoding="utf-8") as f:
                                    f.write(ref_text.strip())
                                with open(voice_dir / "ref.txt", "w", encoding="utf-8") as f:
                                    f.write(ref_text.strip())
                            else:
                                ref_text_file = voice_dir / "ref_text.txt"
                                if ref_text_file.exists():
                                    ref_text_file.unlink()
                                    
                            await websocket.send(json.dumps({
                                "type": "save_voice_response",
                                "request_id": request_id,
                                "payload": {"ok": True, "ref_audio_b64": out_b64}
                            }))

                        elif msg_type == "create_project_request":
                            request_id = message.get("request_id")
                            story_id = payload.get("story_id") or payload.get("project_name")
                            p_type = payload.get("project_type", "reels")
                            aspect_ratio = payload.get("aspect_ratio", "16:9")
                            language = payload.get("language", "vi")
                            items = payload.get("items", [])

                            story_dir = AGENT_PROJECTS_DIR / story_id
                            story_dir.mkdir(parents=True, exist_ok=True)

                            from core import video_engine
                            for it in items:
                                ch_id = it.get("id") if isinstance(it, dict) else it
                                ch_title = it.get("title") if isinstance(it, dict) else ch_id
                                if ch_id:
                                    ch_dir = story_dir / ch_id
                                    ch_dir.mkdir(parents=True, exist_ok=True)
                                    agent_prepare_chapter_structure(story_id, ch_id, "", chapter_dir=ch_dir)
                                    cfg = {"aspect_ratio": aspect_ratio, "language": language}
                                    with open(ch_dir / "project_config.json", "w", encoding="utf-8") as f:
                                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                                    with open(ch_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
                                        f.write(aspect_ratio)

                            c_file = story_dir / "content.json"
                            c_data = {
                                "project_name": story_id,
                                "project_type": p_type,
                                "title": payload.get("title") or story_id,
                                "aspect_ratio": aspect_ratio,
                                "language": language,
                                "items": items
                            }
                            with open(c_file, "w", encoding="utf-8") as f:
                                json.dump(c_data, f, ensure_ascii=False, indent=2)

                            await websocket.send(json.dumps({
                                "type": "create_project_response",
                                "request_id": request_id,
                                "payload": {"ok": True}
                            }))

                        elif msg_type == "add_project_item_request":
                            request_id = message.get("request_id")
                            story_id = payload.get("story_id")
                            item_id = payload.get("item_id") or payload.get("slug")
                            title = payload.get("title") or item_id
                            content = payload.get("content", "")
                            aspect_ratio = payload.get("aspect_ratio", "16:9")
                            language = payload.get("language", "vi")
                            channel = payload.get("channel", "@playnet.zone-vi")
                            episode = payload.get("episode", 1)
                            episode_label = payload.get("episode_label", "Tập 01")

                            story_dir = AGENT_PROJECTS_DIR / story_id
                            story_dir.mkdir(parents=True, exist_ok=True)

                            item_dir = story_dir / item_id
                            item_dir.mkdir(parents=True, exist_ok=True)
                            
                            agent_prepare_chapter_structure(story_id, item_id, content, chapter_dir=item_dir)

                            if content and content.strip():
                                (item_dir / "story.txt").write_text(content.strip(), encoding="utf-8")
                                agent_prepare_chapter_structure(story_id, item_id, content.strip(), chapter_dir=item_dir)

                            meta = {
                                "episode": episode,
                                "episode_label": episode_label,
                                "title": title,
                                "short_title": title,
                                "slug": item_id,
                                "aspect_ratio": aspect_ratio,
                                "language": language,
                                "channel": channel,
                                "content": content
                            }
                            with open(item_dir / "item.json", "w", encoding="utf-8") as f:
                                json.dump(meta, f, ensure_ascii=False, indent=2)

                            cfg = {"aspect_ratio": aspect_ratio, "language": language}
                            with open(item_dir / "project_config.json", "w", encoding="utf-8") as f:
                                json.dump(cfg, f, ensure_ascii=False, indent=2)
                            with open(item_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
                                f.write(aspect_ratio)

                            # Update content.json at story level
                            c_file = story_dir / "content.json"
                            c_data = {}
                            if c_file.exists():
                                try:
                                    with open(c_file, "r", encoding="utf-8") as f:
                                        c_data = json.load(f)
                                except Exception:
                                    pass
                            if not isinstance(c_data, dict):
                                c_data = {"items": []}
                            if "items" not in c_data or not isinstance(c_data["items"], list):
                                c_data["items"] = []
                            c_data["project_name"] = story_id
                            c_data["aspect_ratio"] = aspect_ratio
                            
                            existing = next((it for it in c_data["items"] if (isinstance(it, dict) and (it.get("id") == item_id or it.get("slug") == item_id))), None)
                            if not existing:
                                c_data["items"].append({"id": item_id, "slug": item_id, "title": title, "status": "idle"})
                            else:
                                existing["title"] = title

                            with open(c_file, "w", encoding="utf-8") as f:
                                json.dump(c_data, f, ensure_ascii=False, indent=2)

                            await websocket.send(json.dumps({
                                "type": "add_project_item_response",
                                "request_id": request_id,
                                "payload": {"ok": True, "item_id": item_id}
                            }))

                        elif msg_type == "create_music_project_request":
                            request_id = message.get("request_id")
                            project_name = payload.get("project_name")
                            local_path = payload.get("local_path", "")
                            music_b64 = payload.get("music_b64")
                            
                            projects_base = AGENT_PROJECTS_DIR
                            project_dir = projects_base / "music" / project_name
                            if project_dir.exists():
                                shutil.rmtree(project_dir)
                            project_dir.mkdir(parents=True, exist_ok=True)
                            
                            if music_b64:
                                import base64
                                music_filename = payload.get("music_filename") or "music.mp3"
                                ext = pathlib.Path(music_filename).suffix or ".mp3"
                                audio_path = project_dir / f"music{ext}"
                                with open(audio_path, "wb") as buffer:
                                    buffer.write(base64.b64decode(music_b64))
                            elif local_path.strip():
                                with open(project_dir / "local_music_path.txt", "w", encoding="utf-8") as f:
                                    f.write(local_path.strip())
                                    
                            await websocket.send(json.dumps({
                                "type": "create_music_project_response",
                                "request_id": request_id,
                                "payload": {"ok": True}
                            }))

                        elif msg_type == "delete_voice_request":
                            request_id = message.get("request_id")
                            voice_id = payload.get("voice_id")
                            
                            try:
                                PROTECTED_VOICES = {"nam-dao-ly", "nu-doc-truyen", "nam_dao_ly", "nu_doc_truyen"}
                                if voice_id in PROTECTED_VOICES:
                                    raise ValueError(f"Giọng '{voice_id}' là giọng đọc hệ thống được bảo vệ, không thể xóa!")
                                voices_base = AGENT_VOICES_DIR
                                voice_dir = voices_base / voice_id
                                if voice_dir.exists() and voice_dir.is_dir():
                                    shutil.rmtree(voice_dir)
                                res_payload = {"ok": True}
                            except Exception as del_err:
                                print(f"[Agent] Error deleting voice directory '{voice_id}': {del_err}")
                                res_payload = {"ok": False, "error": str(del_err)}

                            await websocket.send(json.dumps({
                                "type": "delete_voice_response",
                                "request_id": request_id,
                                "payload": res_payload
                            }))
                finally:
                    heartbeat_task.cancel()
                        
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
