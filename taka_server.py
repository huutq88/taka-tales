import asyncio
import datetime
import json
import os
import re
import unicodedata

def slugify(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "d")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
import pathlib
from typing import Dict, List, Set, Optional
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
import shutil

app = FastAPI(title="Taka Coordinator Server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
AGENT_VERSION = "0.4.4"

LORE_KEEPER_URL = os.environ.get("LORE_KEEPER_URL") or os.environ.get("LORE_KEEPER_API") or "http://lore-keeper:8080"
LORE_KEEPER_URL = LORE_KEEPER_URL.rstrip("/")
ENABLE_LORE_KEEPER = os.environ.get("ENABLE_LORE_KEEPER", "1").lower() not in ("0", "false", "no", "off") and os.environ.get("DISABLE_LORE_KEEPER", "0").lower() not in ("1", "true", "yes", "on")

BASE_DIR = pathlib.Path(__file__).parent
DATA_DIR_ENV = os.environ.get("TAKA_DATA_DIR")
if DATA_DIR_ENV:
    DATA_DIR = pathlib.Path(DATA_DIR_ENV).resolve()
else:
    DATA_DIR = pathlib.Path.home() / ".taka-agent"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

VOICES_DIR = DATA_DIR / "voices"
VOICES_DIR.mkdir(parents=True, exist_ok=True)

def migrate_projects_structure(projects_dir: pathlib.Path):
    if not projects_dir or not projects_dir.exists():
        return
    dao_ly_dir = projects_dir / "dao-ly"
    dao_ly_dir.mkdir(parents=True, exist_ok=True)
    
    for item in list(projects_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(".") or item.name in ("music", "dao-ly", "affiliate", "test_project_1"):
            continue
            
        if item.name.startswith("dao_ly_") or item.name.startswith("dao-ly-"):
            sub_story = item / "story"
            target_dir = dao_ly_dir / item.name
            if sub_story.exists() and sub_story.is_dir():
                target_dir.mkdir(parents=True, exist_ok=True)
                for f in sub_story.iterdir():
                    shutil.move(str(f), str(target_dir / f.name))
                print(f"[Migration] Moved legacy sub_story {item / 'story'} -> {target_dir}")
            elif item != target_dir and not target_dir.exists():
                shutil.move(str(item), str(target_dir))
                print(f"[Migration] Moved legacy project {item} -> {target_dir}")

migrate_projects_structure(PROJECTS_DIR)

# In-memory stores
agents_by_workspace: Dict[str, WebSocket] = {}  # workspace_id -> websocket
agent_status: Dict[str, dict] = {}              # workspace_id -> status dict
agent_ip_map: Dict[str, str] = {}               # client_ip -> workspace_id
project_jobs: Dict[str, dict] = {}              # project_name -> job state
pending_file_selects: Dict[str, dict] = {}
pending_agent_requests: Dict[str, dict] = {}

def get_workspace_id_from_request(request: Request) -> str:
    ws_id = request.headers.get("x-workspace-id") or request.query_params.get("workspace_id") or request.query_params.get("ws")
    if not ws_id or ws_id == "null" or ws_id == "undefined":
        ws_id = ""
    else:
        ws_id = ws_id.strip()

    if not ws_id or ws_id not in agents_by_workspace:
        client_ip = ""
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
        elif request.client and request.client.host:
            client_ip = request.client.host

        if client_ip:
            matched_ws = agent_ip_map.get(client_ip)
            if matched_ws and matched_ws in agents_by_workspace:
                ws_id = matched_ws

        if (not ws_id or ws_id not in agents_by_workspace) and len(agents_by_workspace) >= 1:
            ws_id = list(agents_by_workspace.keys())[0]

    return ws_id

async def tunnel_request_to_agent(message_type: str, payload: dict, workspace_id: str = "", timeout: float = 10.0) -> Optional[dict]:
    if not workspace_id:
        return None
    agent_ws = agents_by_workspace.get(workspace_id)
    if not agent_ws:
        return None

    import uuid
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    pending_agent_requests[request_id] = {"event": event, "result": None}
    
    request_message = {
        "type": message_type,
        "request_id": request_id,
        "payload": payload
    }
    
    try:
        await agent_ws.send_text(json.dumps(request_message))
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return pending_agent_requests[request_id]["result"]
    except Exception as e:
        print(f"[Server] Tunnel request {message_type} for workspace '{workspace_id}' failed: {e}")
        return None
    finally:
        pending_agent_requests.pop(request_id, None)

# Config Parser helper
import configparser
_CONFIG_PATH = BASE_DIR / "config.ini"
config = configparser.ConfigParser()
if _CONFIG_PATH.exists():
    config.read(_CONFIG_PATH, encoding="utf-8")

def fetch_chapter_content(chapter_id: str) -> str:
    """Fetches chapter content directly from the Lore-Keeper HTTP API with fallback to public domain."""
    if not ENABLE_LORE_KEEPER:
        raise RuntimeError("Lore-Keeper integration is disabled via ENABLE_LORE_KEEPER=0")

    urls_to_try = [LORE_KEEPER_URL]
    if "taka.zone" not in LORE_KEEPER_URL:
        urls_to_try.append("https://lore-keeper.taka.zone")
        
    last_err = None
    for base_url in urls_to_try:
        try:
            url = f"{base_url.rstrip('/')}/api/chapters/{chapter_id}"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") and "chapter" in data:
                return data["chapter"]["content"]
        except Exception as api_err:
            last_err = api_err
            continue

    raise RuntimeError(f"Failed to fetch chapter content from Lore-Keeper API: {last_err}")

def fetch_story_chapters(story_id: str) -> list:
    """Fetches story chapters directly from the Lore-Keeper HTTP API with fallback to public domain."""
    if not ENABLE_LORE_KEEPER:
        return [
            {"id": f"chap_{story_id}_1", "title": "Chương 1 (Tắt Lore-Keeper)"}
        ]

    urls_to_try = [LORE_KEEPER_URL]
    if "taka.zone" not in LORE_KEEPER_URL:
        urls_to_try.append("https://lore-keeper.taka.zone")
        
    last_err = None
    for base_url in urls_to_try:
        try:
            url = f"{base_url.rstrip('/')}/api/stories/{story_id}/chapters"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") and "chapters" in data:
                return [{"id": ch["id"], "title": ch["title"]} for ch in data["chapters"]]
        except Exception as api_err:
            last_err = api_err
            continue

    print(f"[Server] Failed to fetch story chapters from Lore-Keeper API: {last_err}")
    return [
        {"id": f"chap_{story_id}_1", "title": f"Chương 1 (Mẫu - Lỗi kết nối: {str(last_err)[:20]})"},
        {"id": f"chap_{story_id}_2", "title": f"Chương 2 (Mẫu)"}
    ]

def prepare_chapter_structure(story_id: str, chapter_id: str, content: str = "") -> pathlib.Path:
    """Creates chapter subfolders (text/story_fragments, text/story_sentences, text/image_prompts)
    and populates story.txt, story_fragments, story_sentences immediately when project item is created.
    Excludes audio, images, videos directories until pipeline execution."""
    chapter_dir = PROJECTS_DIR / story_id / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)

    text_dir = chapter_dir / "text"
    frag_dir = text_dir / "story_fragments"
    sent_dir = text_dir / "story_sentences"
    prompts_dir = text_dir / "image_prompts"

    frag_dir.mkdir(parents=True, exist_ok=True)
    sent_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)

    story_file = chapter_dir / "story.txt"
    
    # Fetch content if not provided and story.txt doesn't exist
    if not content:
        if story_file.exists():
            try:
                content = story_file.read_text(encoding="utf-8")
            except Exception:
                pass
        if not content:
            try:
                content = fetch_chapter_content(chapter_id)
            except Exception as e:
                print(f"[Server] Warning: Could not fetch content for {chapter_id}: {e}")

    if content and content.strip():
        story_file.write_text(content, encoding="utf-8")
        
        frag_dir = text_dir / "story_fragments"
        existing_frags = list(frag_dir.glob("story_fragment*.txt")) if frag_dir.exists() else []
        if not existing_frags:
            try:
                from core import video_engine
                num_sentences = video_engine.load_and_split_to_sentences(story_file)
                video_engine.sentences_to_fragments(num_sentences, chapter_dir)
            except Exception as err:
                print(f"[Server] Failed to tokenize via video_engine: {err}")

    return chapter_dir

def fetch_lore_keeper_stories() -> list:
    """Fetches list of available stories from Lore-Keeper HTTP API with fallback to local defaults."""
    if not ENABLE_LORE_KEEPER:
        return [
            {"id": "bang", "title": "Băng (Chuyển Sinh Thượng Cổ)"},
            {"id": "het-buon-het-dien-het-say", "title": "Hết Buồn Hết Điên Hết Sảy"}
        ]
    urls_to_try = [LORE_KEEPER_URL]
    if "taka.zone" not in LORE_KEEPER_URL:
        urls_to_try.append("https://lore-keeper.taka.zone")
    for base_url in urls_to_try:
        try:
            url = f"{base_url.rstrip('/')}/api/stories"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") and "stories" in data:
                return data["stories"]
            elif isinstance(data, list):
                return data
        except Exception:
            continue
    return [
        {"id": "bang", "title": "Băng (Chuyển Sinh Thượng Cổ)"},
        {"id": "het-buon-het-dien-het-say", "title": "Hết Buồn Hết Điên Hết Sảy"}
    ]

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
            
    import re
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

# Serve output videos and media
@app.api_route("/v1/media/{story_id}/{chapter_id}/{file_path:path}", methods=["GET", "HEAD"])
@app.api_route("/media/{story_id}/{chapter_id}/{file_path:path}", methods=["GET", "HEAD"])
async def get_project_media(request: Request, story_id: str, chapter_id: str, file_path: str):
    found_local = None
    cands = [
        PROJECTS_DIR / story_id / chapter_id,
        PROJECTS_DIR / "reels" / chapter_id,
        PROJECTS_DIR / "dao-ly" / chapter_id,
        PROJECTS_DIR / chapter_id,
        PROJECTS_DIR / story_id
    ]
    for bdir in cands:
        found_local = resolve_local_media_file(bdir, file_path)
        if found_local:
            break

    # If file not found on server disk, tunnel request to connected WebSocket agent
    ws_id = get_workspace_id_from_request(request)

    if not found_local:
        is_head = (request.method == "HEAD")
        range_header = request.headers.get("range")
        res = await tunnel_request_to_agent("get_media_file_request", {
            "story_id": story_id,
            "chapter_id": chapter_id,
            "file_path": file_path,
            "head_only": is_head,
            "range": range_header
        }, workspace_id=ws_id, timeout=10.0 if is_head else 30.0)

        if res and isinstance(res, dict) and res.get("exists"):
            content_type = res.get("content_type", "application/octet-stream")
            file_size = res.get("size", 0)
            if is_head:
                return Response(status_code=200, headers={
                    "Content-Type": content_type,
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes"
                })

            content_bytes = b""
            if res.get("content_b64"):
                import base64
                content_bytes = base64.b64decode(res["content_b64"])

            if res.get("partial"):
                start = res.get("start", 0)
                end = res.get("end", len(content_bytes) - 1)
                length = len(content_bytes)
                return Response(
                    content=content_bytes,
                    status_code=206,
                    headers={
                        "Content-Type": content_type,
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Length": str(length),
                        "Accept-Ranges": "bytes"
                    }
                )

            return Response(content=content_bytes, media_type=content_type, headers={"Accept-Ranges": "bytes"})

    if found_local and found_local.exists():
        import mimetypes
        ctype, _ = mimetypes.guess_type(str(found_local))
        if not ctype:
            if str(found_local).endswith(".mp4"):
                ctype = "video/mp4"
            elif str(found_local).endswith(".wav"):
                ctype = "audio/wav"
            elif str(found_local).endswith(".mp3"):
                ctype = "audio/mpeg"
            else:
                ctype = "application/octet-stream"

        file_size = found_local.stat().st_size

        if request.method == "HEAD":
            return Response(status_code=200, headers={
                "Content-Type": ctype,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes"
            })

        range_header = request.headers.get("range")
        if range_header and range_header.startswith("bytes="):
            try:
                ranges = range_header.split("=")[1].split("-")
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1
                if start < file_size:
                    max_chunk = 2 * 1024 * 1024
                    if end - start + 1 > max_chunk:
                        end = start + max_chunk - 1
                    end = min(end, file_size - 1)
                    length = end - start + 1
                    with open(found_local, "rb") as f:
                        f.seek(start)
                        chunk = f.read(length)
                    return Response(
                        content=chunk,
                        status_code=206,
                        headers={
                            "Content-Type": ctype,
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                            "Content-Length": str(length),
                            "Accept-Ranges": "bytes"
                        }
                    )
            except Exception:
                pass

        return FileResponse(str(found_local), media_type=ctype, headers={"Accept-Ranges": "bytes"})

    raise HTTPException(status_code=404, detail="Media file not found")

# WebSocket endpoint for agent connection
@app.websocket("/v1/system/agent/ws")
async def agent_ws_endpoint(websocket: WebSocket, workspace_id: str = "default_workspace"):
    await websocket.accept()
    agents_by_workspace[workspace_id] = websocket
    client_ip = websocket.client.host if (websocket.client and websocket.client.host) else ""
    if client_ip:
        agent_ip_map[client_ip] = workspace_id
    print(f"[Server] Taka-Agent connected. Workspace: {workspace_id} (IP: {client_ip})")
    try:
        while True:
            data_str = await websocket.receive_text()
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            payload = data.get("payload", {})

            if msg_type == "status_update":
                agent_status[workspace_id] = payload
            elif msg_type == "pipeline_progress":
                project_name = data.get("project_name")
                story_id = data.get("story_id")
                chapter_id = data.get("chapter_id")
                
                if story_id and chapter_id:
                    job_key = f"{story_id}/{chapter_id}"
                elif project_name and "_" in project_name:
                    story_p, chap_p = project_name.rsplit("_", 1)
                    job_key = f"{story_p}/{chap_p}"
                else:
                    job_key = project_name or ""

                if job_key:
                    project_jobs[job_key] = {
                        "status": data.get("status"),
                        "queue_position": data.get("queue_position", 0),
                        "total_queued": data.get("total_queued", 0),
                        "current_fragment": data.get("current_fragment", 0),
                        "total_fragments": data.get("total_fragments", 0),
                        "fragment_status": data.get("fragment_status", {}),
                        "error": data.get("error"),
                        "updated_at": data.get("updated_at")
                    }
            elif msg_type == "select_file_response":
                request_id = data.get("request_id")
                selected_path = payload.get("path", "")
                if request_id in pending_file_selects:
                    pending_file_selects[request_id]["path"] = selected_path
                    pending_file_selects[request_id]["event"].set()
            elif msg_type and msg_type.endswith("_response"):
                request_id = data.get("request_id")
                if request_id in pending_agent_requests:
                    pending_agent_requests[request_id]["result"] = payload
                    pending_agent_requests[request_id]["event"].set()
    except WebSocketDisconnect:
        print(f"[Server] Taka-Agent disconnected: {workspace_id}")
    finally:
        agents_by_workspace.pop(workspace_id, None)
        agent_status.pop(workspace_id, None)
        if client_ip and agent_ip_map.get(client_ip) == workspace_id:
            agent_ip_map.pop(client_ip, None)

@app.get("/v1/agent/workspaces")
async def list_active_workspaces():
    return {
        "active_workspaces": list(agents_by_workspace.keys()),
        "agent_status": agent_status
    }

def get_default_workspace_id():
    try:
        import getpass, uuid, hashlib, socket
        user = getpass.getuser().lower()
        clean_user = "".join(c for c in user if c.isalnum() or c in ("-", "_")).strip() or "user"
        mac = uuid.getnode()
        hostname = socket.gethostname()
        dev_hash = hashlib.md5(f"{mac}-{hostname}".encode()).hexdigest()[:6]
        return f"{clean_user}_{dev_hash}"
    except Exception:
        pass
    return "default_workspace"

@app.get("/v1/agent/status")
async def get_agent_status(request: Request):
    active_ws_list = list(agents_by_workspace.keys())
    request_ws = get_workspace_id_from_request(request)
    
    if (not request_ws or request_ws not in agents_by_workspace) and active_ws_list:
        request_ws = active_ws_list[0]

    connected = bool(request_ws and request_ws in agents_by_workspace)
    st = agent_status.get(request_ws, {}) if (request_ws and connected) else {}

    agent_ver = st.get("agent_version", AGENT_VERSION) if connected else AGENT_VERSION
    needs_update = (agent_ver != AGENT_VERSION) if connected else False

    running_jobs = [k for k, v in project_jobs.items() if v.get("status") not in ("idle", "completed", "failed", "stopped", "queued", None)]
    queued_jobs = [k for k, v in project_jobs.items() if v.get("status") == "queued"]
    active_running = running_jobs[0] if running_jobs else None

    return JSONResponse(
        content={
            "connected": connected,
            "workspace_id": request_ws,
            "active_workspaces": active_ws_list,
            "agents": {request_ws: st} if (request_ws and connected) else {},
            "server_version": AGENT_VERSION,
            "needs_update": needs_update,
            "agent_version": agent_ver,
            "active_running_project": active_running,
            "total_running": len(running_jobs),
            "total_queued": len(queued_jobs)
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

@app.get("/v1/system/install-agent.sh", response_class=PlainTextResponse)
async def get_install_script(request: Request, workspace_id: str = "default_workspace"):
    server_url = str(request.base_url).rstrip('/')
    
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")
    ollama_model = config.get("IMAGE_PROMPT", "OLLAMA_MODEL", fallback="qwen2.5-coder:14b")
    
    script_content = f"""#!/bin/bash
set -e

SERVER_URL="{server_url}"
WORKSPACE_ID="{workspace_id}"

echo "============================================="
echo "   Taka Agent Installer v{AGENT_VERSION}     "
echo "============================================="
echo "Coordinator Server: $SERVER_URL"
echo "Workspace ID:       $WORKSPACE_ID"
echo "Agent Version:      {AGENT_VERSION}"
echo "============================================="

# 1. Create and change to agent directory
echo "[1/6] Creating directory '~/.taka-agent'..."
mkdir -p "$HOME/.taka-agent"
cd "$HOME/.taka-agent"

# 2. Download agent files from Server
echo "[2/6] Downloading agent files from server..."
curl -fsSL "$SERVER_URL/v1/system/agent/files/requirements-agent.txt" -o requirements.txt
curl -fsSL "$SERVER_URL/v1/system/agent/files/taka_agent.py" -o taka_agent.py
curl -fsSL "$SERVER_URL/v1/system/agent/files/config.ini" -o config.ini

mkdir -p core
curl -fsSL "$SERVER_URL/v1/system/agent/files/core/__init__.py" -o core/__init__.py
curl -fsSL "$SERVER_URL/v1/system/agent/files/core/video_engine.py" -o core/video_engine.py
curl -fsSL "$SERVER_URL/v1/system/agent/files/core/text_formatter.py" -o core/text_formatter.py
curl -fsSL "$SERVER_URL/v1/system/agent/files/core/characters_descriptions.ini" -o core/characters_descriptions.ini

mkdir -p subtitle_engine
for sfile in __init__.py alignment.py ass_renderer.py cache.py caption_segmenter.py cli.py domain.py emoji_engine.py font_manager.py layout_engine.py processor.py quality_analyzer.py speaker_manager.py svg_renderer.py transcript_resolver.py; do
    curl -fsSL "$SERVER_URL/v1/system/agent/files/subtitle_engine/$sfile" -o "subtitle_engine/$sfile" || true
done

mkdir -p presets
curl -fsSL "$SERVER_URL/v1/system/agent/files/presets/2d-stick-figure-cartoon.json" -o presets/2d-stick-figure-cartoon.json || true

# 3. Configure config.ini with SERVER_URL and WORKSPACE_ID
echo "[3/6] Configuring config.ini..."
python3 -c "
import configparser, getpass, uuid, hashlib, socket

user = getpass.getuser().lower()
clean_user = ''.join(c for c in user if c.isalnum() or c in ('-', '_')).strip() or 'user'
mac = uuid.getnode()
hostname = socket.gethostname()
dev_hash = hashlib.md5(f'{mac}-{hostname}'.encode()).hexdigest()[:6]
default_ws = f'{clean_user}_{dev_hash}'

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')
if not config.has_section('TAKA_AGENT'):
    config.add_section('TAKA_AGENT')
config.set('TAKA_AGENT', 'SERVER_URL', '$SERVER_URL')
config.set('TAKA_AGENT', 'WORKSPACE_ID', default_ws)

with open('config.ini', 'w', encoding='utf-8') as f:
    config.write(f)
"

# 4. Set up virtual environment
echo "[4/6] Setting up Python virtual environment..."
python3 -m venv env
source env/bin/activate

# 5. Install core connection requirements first and start Agent immediately
echo "[5/6] Installing core connection dependencies..."
pip3 install websockets requests configparser edge-tts psutil

echo "============================================="
echo "🎉 Taka Agent Core Connected!"
echo "============================================="
echo "👉 Workspace ID của máy bạn là: $WORKSPACE_ID"
echo "👉 Mở https://tales.taka.zone và chọn Workspace ID: $WORKSPACE_ID"
echo "============================================="
echo "Starting Taka Agent connection..."
pkill -f "python.*taka_agent.py" || true
pkill -f "taka_agent.py" || true
sleep 1
nohup python -u taka_agent.py > agent.log 2>&1 &

echo "Installing PyTorch, Whisper & AI rendering packages (in background)..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    pip3 install torch torchvision torchaudio
else
    pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
fi
pip install -r requirements.txt
pip install faster-whisper whisperx imageio-ffmpeg || true

# 6. Setup OmniVoice (Vietnamese Voice Cloning Tool)
echo "[6/6] Pre-installing OmniVoice tool..."
if [ ! -d "tools/OmniVoice" ]; then
    echo "Cloning OmniVoice repository..."
    git clone https://github.com/k2-fsa/OmniVoice tools/OmniVoice
    if [ -f "tools/OmniVoice/requirements.txt" ]; then
        echo "Installing OmniVoice requirements..."
        pip install -r tools/OmniVoice/requirements.txt
    fi
else
    echo "OmniVoice is already pre-installed."
fi

echo "Pre-downloading AI models and NLTK assets (this may take a few minutes)..."
python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); from huggingface_hub import snapshot_download; snapshot_download(repo_id='k2-fsa/OmniVoice'); snapshot_download(repo_id='openai/whisper-small'); from keybert import KeyBERT; KeyBERT()" || echo "Warning: Failed to pre-download some models, they will download on first run."

echo "============================================="
echo "🎉 Taka Agent Installation / Update Complete!"
echo "============================================="
echo "👉 Workspace ID của máy bạn là: $WORKSPACE_ID"
echo "👉 Mở https://tales.taka.zone và chọn Workspace ID: $WORKSPACE_ID"
echo "============================================="
echo "Restarting Taka Agent in the background..."
pkill -f "python.*taka_agent.py" || true
pkill -f "taka_agent.py" || true
sleep 1
nohup python -u taka_agent.py > agent.log 2>&1 &
echo "Agent is running (v{AGENT_VERSION}). You can check logs in ~/.taka-agent/agent.log"
echo "============================================="
"""
    return PlainTextResponse(content=script_content, media_type="text/x-shellscript")

@app.get("/v1/system/install-agent.ps1", response_class=PlainTextResponse)
async def get_install_script_ps1(request: Request, workspace_id: str = "default_workspace"):
    server_url = str(request.base_url).rstrip('/')
    
    script_content = fr"""
$SERVER_URL = "{server_url}"
$uName = $env:USERNAME.ToLower() -replace '[^a-zA-Z0-9_-]', ''
if (-not $uName) {{ $uName = "user" }}
$hName = $env:COMPUTERNAME
$md5 = [System.Security.Cryptography.MD5]::Create()
$hashBytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$hName-$uName"))
$devHash = ([BitConverter]::ToString($hashBytes).Replace("-","").ToLower()).Substring(0, 6)
$WORKSPACE_ID = "${{uName}}_${{devHash}}"

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "   Taka Agent Installer v{AGENT_VERSION} (Windows PowerShell) " -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "Coordinator Server: $SERVER_URL"
Write-Host "Workspace ID:       $WORKSPACE_ID"
Write-Host "Agent Version:      {AGENT_VERSION}"
Write-Host "====================================================="

# 1. Create and change to agent directory
Write-Host "[1/6] Creating directory '$HOME/.taka-agent'..." -ForegroundColor Green
New-Item -ItemType Directory -Force -Path "$HOME/.taka-agent" | Out-Null
Set-Location -Path "$HOME/.taka-agent"

# 2. Download agent files from Server
Write-Host "[2/6] Downloading agent files from server..." -ForegroundColor Green
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/requirements-agent.txt" -OutFile "requirements.txt"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/taka_agent.py" -OutFile "taka_agent.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/config.ini" -OutFile "config.ini"

New-Item -ItemType Directory -Force -Path "core" | Out-Null
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/__init__.py" -OutFile "core/__init__.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/video_engine.py" -OutFile "core/video_engine.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/characters_descriptions.ini" -OutFile "core/characters_descriptions.ini"

# 3. Locate Python Executable
Write-Host "[3/6] Locating Python environment..." -ForegroundColor Green
$PYTHON_EXE = $null

$searchPaths = @(
    "py",
    "python",
    "$env:LocalAppData\Programs\Python\Python311\python.exe",
    "$env:LocalAppData\Programs\Python\Python310\python.exe",
    "$env:LocalAppData\Programs\Python\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe",
    "C:\Python312\python.exe"
)

foreach ($p in $searchPaths) {{
    try {{
        $ver = & $p --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3\.") {{
            $PYTHON_EXE = $p
            Write-Host "Found Python: $ver ($p)" -ForegroundColor Yellow
            break
        }}
    }} catch {{}}
}}

if (-not $PYTHON_EXE) {{
    Write-Host "Python 3 was not found. Automatically installing Python 3.10..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {{
        Write-Host "Installing Python via Windows Package Manager (winget)..." -ForegroundColor Yellow
        winget install -e --id Python.Python.3.10 --scope user --accept-source-agreements --accept-package-agreements
    }} else {{
        Write-Host "Downloading official Python installer..." -ForegroundColor Yellow
        $pyUrl = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"
        Invoke-WebRequest -Uri $pyUrl -OutFile "$env:TEMP\python_setup.exe"
        Write-Host "Installing Python 3.10 in background..." -ForegroundColor Yellow
        Start-Process "$env:TEMP\python_setup.exe" -ArgumentList "/passive", "InstallAllUsers=1", "PrependPath=1", "Include_pip=1" -Wait
        Remove-Item "$env:TEMP\python_setup.exe" -Force -ErrorAction SilentlyContinue
    }}

    # Refresh PATH environment variable in current session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    # Re-search Python executable
    foreach ($p in $searchPaths) {{
        try {{
            $ver = & $p --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3\.") {{
                $PYTHON_EXE = $p
                Write-Host "Found freshly installed Python: $ver" -ForegroundColor Green
                break
            }}
        }} catch {{}}
    }}
}}

if (-not $PYTHON_EXE) {{
    Write-Host "ERROR: Could not complete automatic Python installation." -ForegroundColor Red
    Write-Host "Please download Python 3.10 manually from https://www.python.org/downloads/ (check 'Add Python to PATH') and re-run." -ForegroundColor Yellow
    exit 1
}}

# Configure config.ini
& $PYTHON_EXE -c "
import configparser, uuid, hashlib, socket

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')
if not config.has_section('TAKA_AGENT'):
    config.add_section('TAKA_AGENT')
config.set('TAKA_AGENT', 'SERVER_URL', '$SERVER_URL')
config.set('TAKA_AGENT', 'WORKSPACE_ID', '$WORKSPACE_ID')

with open('config.ini', 'w', encoding='utf-8') as f:
    config.write(f)
"

# 4. Set up virtual environment
Write-Host "[4/6] Setting up Python virtual environment..." -ForegroundColor Green
if (-not (Test-Path "env/Scripts/python.exe")) {{
    & $PYTHON_EXE -m venv env
}}

$ENV_PYTHON = "$HOME/.taka-agent/env/Scripts/python.exe"
$ENV_PIP = "$HOME/.taka-agent/env/Scripts/pip.exe"
if (-not (Test-Path $ENV_PYTHON)) {{
    $ENV_PYTHON = $PYTHON_EXE
    $ENV_PIP = "$PYTHON_EXE -m pip"
}}

# 5. Install core connection requirements first and start Agent immediately
Write-Host "[5/6] Installing core connection dependencies..." -ForegroundColor Green
& $ENV_PIP install websockets requests configparser edge-tts psutil

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "🎉 Taka Agent Connected Successfully!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "👉 Workspace ID của máy bạn là: $WORKSPACE_ID" -ForegroundColor Yellow
Write-Host "👉 Tự động mở trình duyệt $SERVER_URL/?ws=$WORKSPACE_ID..." -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Starting Taka Agent connection..." -ForegroundColor Yellow
Start-Process -FilePath "$ENV_PYTHON" -ArgumentList "-u", "taka_agent.py" -WorkingDirectory "$HOME/.taka-agent" -WindowStyle Hidden
Start-Process "$SERVER_URL/?ws=$WORKSPACE_ID"

Write-Host "Installing PyTorch, Whisper & AI rendering packages (in background)..." -ForegroundColor Yellow
& $ENV_PIP install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
& $ENV_PIP install -r requirements.txt
& $ENV_PIP install faster-whisper whisperx imageio-ffmpeg

# 6. Setup OmniVoice (Vietnamese Voice Cloning Tool)
Write-Host "[6/6] Pre-installing OmniVoice tool..." -ForegroundColor Green
if (-not (Test-Path "tools/OmniVoice")) {{
    New-Item -ItemType Directory -Force -Path "tools" | Out-Null
    if (Get-Command git -ErrorAction SilentlyContinue) {{
        Write-Host "Cloning OmniVoice repository via Git..." -ForegroundColor Yellow
        & git clone https://github.com/k2-fsa/OmniVoice tools/OmniVoice
    }} else {{
        Write-Host "Git not found. Downloading OmniVoice zip archive..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://github.com/k2-fsa/OmniVoice/archive/refs/heads/main.zip" -OutFile "tools/omnivoice.zip"
        Expand-Archive -Path "tools/omnivoice.zip" -DestinationPath "tools" -Force
        if (Test-Path "tools/OmniVoice-main") {{
            Rename-Item -Path "tools/OmniVoice-main" -NewName "OmniVoice" -Force
        }}
        Remove-Item -Path "tools/omnivoice.zip" -Force -ErrorAction SilentlyContinue
    }}

    if (Test-Path "tools/OmniVoice/requirements.txt") {{
        Write-Host "Installing OmniVoice requirements..." -ForegroundColor Yellow
        & $ENV_PIP install -r tools/OmniVoice/requirements.txt
    }}
}} else {{
    Write-Host "OmniVoice is already pre-installed."
}}

Write-Host "Pre-downloading AI models and NLTK assets (this may take a few minutes)..." -ForegroundColor Yellow
try {{
    & $ENV_PYTHON -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); from huggingface_hub import snapshot_download; snapshot_download(repo_id='k2-fsa/OmniVoice'); snapshot_download(repo_id='openai/whisper-small'); from keybert import KeyBERT; KeyBERT()"
}} catch {{
    Write-Host "Warning: Failed to pre-download some models, they will download on first run." -ForegroundColor Gray
}}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "🎉 All Taka Agent packages successfully ready!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
"""
    return PlainTextResponse(content=script_content, media_type="text/plain")

@app.get("/v1/system/agent/files/{filepath:path}")
async def get_agent_file(filepath: str):
    allowed_exact = [
        "taka_agent.py",
        "config.ini",
        "requirements-agent.txt",
        "requirements.txt",
    ]
    is_allowed = (
        filepath in allowed_exact
        or filepath.startswith("core/")
        or filepath.startswith("subtitle_engine/")
        or filepath.startswith("presets/")
    )
    if not is_allowed or ".." in filepath:
        raise HTTPException(status_code=403, detail="Access denied")
    
    file_path = BASE_DIR / filepath
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(str(file_path))


def pick_file_cross_platform(prompt: str = "Select a file") -> str:
    import sys, os, subprocess
    clean_prompt = prompt.replace('"', '').replace("'", "").replace("\n", " ").strip() or "Select a file"

    if sys.platform == "darwin":
        try:
            script = f'POSIX path of (choose file with prompt "{clean_prompt}")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
            pass

    return ""


@app.get("/v1/system/select-file")
async def select_local_file(request: Request, prompt: str = "Select a file"):
    import sys, os
    # 1. Direct local GUI file picker if server is running locally on desktop (macOS / Windows / Linux GUI)
    if not (sys.platform.startswith("linux") and not os.environ.get("DISPLAY")):
        local_path = await asyncio.to_thread(pick_file_cross_platform, prompt)
        if local_path:
            return {"path": local_path}

    # 2. Route to connected Agent via WebSocket if running on headless server
    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id) if ws_id else None

    if agent_ws:
        import uuid
        request_id = str(uuid.uuid4())
        event = asyncio.Event()
        pending_file_selects[request_id] = {"event": event, "path": ""}
        
        msg = {
            "type": "select_file_request",
            "request_id": request_id,
            "payload": {"prompt": prompt}
        }
        
        try:
            await agent_ws.send_text(json.dumps(msg))
            await asyncio.wait_for(event.wait(), timeout=30.0)
            result = pending_file_selects.pop(request_id, {"path": ""})
            return {"path": result.get("path", "")}
        except Exception:
            pending_file_selects.pop(request_id, None)
            return {"path": ""}

    return {"path": ""}


@app.post("/v1/projects")
async def create_project(request: Request, story_id: str):
    if not story_id.strip():
        raise HTTPException(status_code=400, detail="story_id cannot be empty")
    # Sanitize story_id to prevent path traversal
    clean_id = "".join(c for c in story_id if c.isalnum() or c in ("-", "_")).strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Invalid story_id format")

    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id) if ws_id else None

    # Fetch chapters from Lore Keeper if available
    fetched_chaps = []
    try:
        from fastapi.concurrency import run_in_threadpool
        fetched_chaps = await run_in_threadpool(fetch_story_chapters, clean_id)
    except Exception as e:
        print(f"[Server] Warning: Could not fetch chapters for {clean_id}: {e}")

    ch_ids = [ch["id"] for ch in fetched_chaps] if fetched_chaps else [f"{clean_id}-chuong-1"]

    if agent_ws:
        try:
            await tunnel_request_to_agent("create_project_request", {
                "story_id": clean_id,
                "chapters": ch_ids
            }, workspace_id=ws_id, timeout=10.0)
        except Exception as e:
            print(f"[Server] Warning: create_project_request to Agent failed: {e}")

    story_dir = PROJECTS_DIR / clean_id
    story_dir.mkdir(parents=True, exist_ok=True)
    for ch in ch_ids:
        prepare_chapter_structure(clean_id, ch)

    return {"ok": True, "story_id": clean_id, "chapters": ch_ids}

@app.post("/v1/projects/{story_id}/open-folder")
@app.post("/v1/projects/{story_id}/{chapter_id}/open-folder")
async def open_project_folder(request: Request, story_id: str, chapter_id: Optional[str] = None):
    clean_story = story_id.strip()
    if not clean_story or ".." in clean_story:
        raise HTTPException(status_code=400, detail="Invalid story_id format")

    ws_id = get_workspace_id_from_request(request)
    if ws_id and ws_id in agents_by_workspace:
        res = await tunnel_request_to_agent("open_folder_request", {
            "story_id": clean_story,
            "chapter_id": chapter_id
        }, workspace_id=ws_id, timeout=10.0)
        if res and isinstance(res, dict):
            if res.get("status") == "ok":
                return res
            elif res.get("error"):
                raise HTTPException(status_code=400, detail=f"Agent failed to open folder: {res['error']}")

    base_dir = PROJECTS_DIR
    target_dir = None
    if chapter_id and chapter_id != "story":
        candidates = [
            base_dir / clean_story / chapter_id,
            base_dir / "reels" / chapter_id,
            base_dir / "dao-ly" / chapter_id,
            base_dir / clean_story
        ]
    else:
        candidates = [
            base_dir / clean_story,
            base_dir / "reels" / clean_story,
            base_dir / "dao-ly" / clean_story
        ]

    for cand in candidates:
        if cand.exists():
            target_dir = cand
            break

    if not target_dir:
        # Fallback glob matching
        query_name = chapter_id if (chapter_id and chapter_id != "story") else clean_story
        matches = list(base_dir.glob(f"**/{query_name}"))
        if matches:
            target_dir = matches[0]
        else:
            target_dir = base_dir / clean_story
            target_dir.mkdir(parents=True, exist_ok=True)

    import subprocess, platform, shutil
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(target_dir)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(target_dir)])
        else:
            if shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(target_dir)])
            else:
                raise HTTPException(status_code=400, detail="Môi trường Cloud Server không có giao diện hiển thị. Vui lòng kết nối Taka Agent để mở thư mục trên máy cá nhân.")
        return {"status": "ok", "message": f"Opened folder: {target_dir}", "path": str(target_dir)}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {e}")

@app.delete("/v1/projects/{story_id}")
@app.delete("/v1/projects/{story_id}/{chapter_id}")
@app.post("/v1/projects/{story_id}/delete")
@app.post("/v1/projects/{story_id}/{chapter_id}/delete")
async def delete_project(request: Request, story_id: str, chapter_id: Optional[str] = None):
    clean_story = story_id.strip()
    if not clean_story or "/" in clean_story or ".." in clean_story:
        raise HTTPException(status_code=400, detail="Invalid story_id format")

    clean_slug = slugify(clean_story) or clean_story
    for target_ws in list(agents_by_workspace.keys()):
        try:
            await tunnel_request_to_agent("delete_project_request", {
                "story_id": clean_slug,
                "raw_id": clean_story,
                "chapter_id": chapter_id
            }, workspace_id=target_ws, timeout=5.0)
        except Exception as e:
            print(f"[Server] Warning: delete_project_request to Agent '{target_ws}' failed: {e}")

    try:
        import taka_agent
        taka_agent.remove_from_queue_and_active(clean_story, chapter_id)
        taka_agent.remove_from_queue_and_active(clean_slug, chapter_id)
    except Exception as ex:
        print(f"[Server] Warning: local remove_from_queue_and_active failed: {ex}")

    # Remove matching job state
    for sid in (clean_slug, clean_story):
        target_pattern = f"{sid}/{chapter_id}" if (chapter_id and chapter_id != "story") else sid
        keys_to_del = [k for k in project_jobs.keys() if k == target_pattern or k.startswith(f"{target_pattern}/")]
        for k in keys_to_del:
            project_jobs.pop(k, None)

    # Delete local folders on server
    dirs_to_check = []
    if chapter_id and chapter_id != "story":
        dirs_to_check.append(PROJECTS_DIR / clean_slug / chapter_id)
        dirs_to_check.append(PROJECTS_DIR / clean_story / chapter_id)
    else:
        dirs_to_check.append(PROJECTS_DIR / clean_slug)
        dirs_to_check.append(PROJECTS_DIR / clean_story)

    for d in dirs_to_check:
        if d.exists():
            parent_dir = d.parent
            try:
                shutil.rmtree(d, ignore_errors=True)
                print(f"[Server] Successfully deleted folder: {d}")
                try:
                    import taka_agent
                    taka_agent.update_reels_content_json(parent_dir)
                except Exception:
                    pass
            except Exception as ex:
                print(f"[Server] Warning deleting {d}: {ex}")

    for cat in ("dao-ly", "dao_ly", "music", clean_story):
        cat_dir = PROJECTS_DIR / cat
        if cat_dir.exists() and cat_dir.is_dir() and not any(p for p in cat_dir.iterdir() if not p.name.startswith(".")):
            shutil.rmtree(cat_dir, ignore_errors=True)

    print(f"[Server] Successfully deleted project directory for story_id={clean_story}, chapter_id={chapter_id}")
    return {"ok": True, "story_id": clean_story, "chapter_id": chapter_id}


@app.post("/v1/projects/cancel-all")
async def cancel_all_jobs(request: Request):
    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id) if ws_id else None
    if agent_ws:
        try:
            await tunnel_request_to_agent("cancel_all_jobs_request", {}, workspace_id=ws_id, timeout=3.0)
        except Exception as e:
            print(f"[Server] Warning: cancel_all_jobs_request failed: {e}")

    try:
        import taka_agent
        taka_agent.agent_active_tasks.clear()
        taka_agent.agent_queued_jobs.clear()
        while not taka_agent.pipeline_queue.empty():
            try:
                taka_agent.pipeline_queue.get_nowait()
            except Exception:
                break
    except Exception as ex:
        print(f"[Server] Clear local queue warning: {ex}")

    for k, v in list(project_jobs.items()):
        if v.get("status") not in ("completed", "failed", "idle"):
            v["status"] = "stopped"
            v["queue_position"] = 0

    return {"message": "All running and queued pipeline jobs have been cancelled and cleared."}


@app.post("/v1/projects/music")
async def create_music_project(project_name: str, local_path: str = "", file: Optional[UploadFile] = File(None)):
    if not project_name.strip():
        raise HTTPException(status_code=400, detail="project_name cannot be empty")
    clean_name = "".join(c for c in project_name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Invalid project_name format")
    
    project_dir = PROJECTS_DIR / "music" / clean_name
    
    # Clear old directory content completely if exists to avoid leftover music files with different extensions
    if project_dir.exists():
        shutil.rmtree(project_dir)
        
    project_dir.mkdir(parents=True, exist_ok=True)
    
    if file is not None and file.filename:
        ext = pathlib.Path(file.filename).suffix or ".mp3"
        audio_path = project_dir / f"music{ext}"
        try:
            with open(audio_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            print(f"[Server] Music file saved for project {clean_name} at {audio_path}")
        except Exception as e:
            if project_dir.exists():
                shutil.rmtree(project_dir)
            raise HTTPException(status_code=500, detail=f"Failed to save music file: {str(e)}")
    elif local_path.strip():
        local_path_file = project_dir / "local_music_path.txt"
        try:
            with open(local_path_file, "w", encoding="utf-8") as f:
                f.write(local_path.strip())
            print(f"[Server] Music local path saved for project {clean_name}: {local_path.strip()}")
        except Exception as e:
            if project_dir.exists():
                shutil.rmtree(project_dir)
            raise HTTPException(status_code=500, detail=f"Failed to save local path: {str(e)}")
    else:
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise HTTPException(status_code=400, detail="Either a music file upload or a local path is required.")
        
    return {"ok": True, "project_name": clean_name}



@app.get("/v1/projects")
async def list_projects(request: Request, story_id: Optional[str] = None):
    ws_id = get_workspace_id_from_request(request)
    stories = []
    story_ids = set()
    agent_files = {}
    projects_metadata = {}
    
    agent_ws = agents_by_workspace.get(ws_id) if ws_id else None
    if agent_ws:
        res = await tunnel_request_to_agent("list_projects_request", {}, workspace_id=ws_id, timeout=15.0)
        if res:
            story_ids = set(res.get("story_folders", []))
            agent_files = res.get("local_files", {})
            projects_metadata = res.get("projects_metadata", {})
            print(f"[Server] Fetched project folders from Agent ({ws_id}): {story_ids}")
            
    if PROJECTS_DIR.exists():
        for item in PROJECTS_DIR.iterdir():
            if item.is_dir() and not item.name.startswith(".") and item.name not in ("test_project_1", "affiliate"):
                story_ids.add(item.name)
                if item.name not in projects_metadata:
                    content_file = item / "content.json"
                    if content_file.exists():
                        try:
                            with open(content_file, "r", encoding="utf-8") as f:
                                projects_metadata[item.name] = json.load(f)
                        except Exception:
                            pass
                for ch_dir in item.iterdir():
                    if ch_dir.is_dir() and not ch_dir.name.startswith("."):
                        ch_id = ch_dir.name
                        key = f"{item.name}/{ch_id}"
                        if key not in agent_files:
                            agent_files[key] = {
                                "has_story": (ch_dir / "story.txt").exists(),
                                "has_video": (ch_dir / "final.mp4").exists() or (ch_dir / f"{item.name}_{ch_id}.mp4").exists()
                            }

    stories_map = {s_id: [] for s_id in story_ids}
    for key, info in agent_files.items():
        if "/" not in key:
            continue
        s_id, ch_id = key.split("/", 1)
        if s_id not in stories_map:
            stories_map[s_id] = []

        job_key = f"{s_id}/{ch_id}"
        job_state = project_jobs.get(job_key, {"status": "idle"}).copy()
        if info.get("has_video") and job_state.get("status") == "idle":
            job_state["status"] = "completed"
            
        item_data = {
            "id": ch_id,
            "title": ch_id.replace("-", " ").replace("_", " ").title(),
            "has_story": info.get("has_story", False),
            "has_video": info.get("has_video", False),
            "status": job_state.get("status", "idle"),
            "progress": job_state
        }
        stories_map[s_id].append(item_data)

    for s_id in sorted(list(stories_map.keys())):
        chaps = stories_map[s_id]
        meta = projects_metadata.get(s_id, {})
        if not isinstance(meta, dict):
            meta = {}
        p_type = meta.get("project_type")
        p_title = meta.get("title") or meta.get("project_name")

        if not chaps and meta.get("items"):
            for it in meta["items"]:
                chaps.append({
                    "id": it.get("id", s_id),
                    "title": it.get("title", s_id),
                    "has_story": False,
                    "has_video": False,
                    "status": it.get("status", "idle"),
                    "progress": {"status": it.get("status", "idle")}
                })

        for idx, item in enumerate(chaps, 1):
            ch_id = item["id"]
            meta_item = {}
            item_json = PROJECTS_DIR / s_id / ch_id / "item.json"
            if not item_json.exists():
                alt_json = pathlib.Path.home() / ".taka-agent" / "projects" / s_id / ch_id / "item.json"
                if alt_json.exists():
                    item_json = alt_json

            if item_json.exists():
                try:
                    with open(item_json, "r", encoding="utf-8") as f:
                        meta_item = json.load(f)
                except Exception:
                    pass

            ep_n = meta_item.get("episode")
            if ep_n is None:
                m = re.search(r"^(?:ep|tap|episode)?[-_\s]*(\d+)", ch_id, re.IGNORECASE) or re.search(r"^(?:Tập|Episode|#)?\s*(\d+)", item.get("title", ""), re.IGNORECASE)
                ep_n = int(m.group(1)) if m else idx
            item["episode_num"] = ep_n

            ep_label = meta_item.get("episode_label") or f"Tập {ep_n:02d}"
            short_t = meta_item.get("short_title") or meta_item.get("title")
            if short_t:
                item["title"] = f"{ep_label} - {short_t}"
            else:
                clean_t = re.sub(r"^#?\d+[\s-]*", "", item["title"])
                item["title"] = f"{ep_label} - {clean_t}"

        chaps = sorted(chaps, key=lambda x: (x.get("episode_num", 999), x.get("id", "")))

        if not p_type:
            cfg_file = PROJECTS_DIR / s_id / "project_config.json"
            if cfg_file.exists():
                try:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        if isinstance(d, dict): p_type = d.get("project_type")
                except Exception: pass

        if not p_type:
            if "reels" in s_id.lower():
                p_type = "reels"
            elif "videos" in s_id.lower() or "long" in s_id.lower() or "longform" in s_id.lower():
                p_type = "long"
            elif "sketch" in s_id.lower():
                p_type = "sketch"
            elif "music" in s_id.lower():
                p_type = "music"
            else:
                p_type = "story"

        type_prefixes = {
            "long": "📺 Long",
            "reels": "📱 Reels",
            "sketch": "✏️ Sketch",
            "music": "🎵 Music",
            "story": "📖 Story"
        }
        prefix = type_prefixes.get(p_type, "📖 Story")
        clean_name = meta.get("title") or s_id.replace("-", " ").replace("_", " ").title()
        p_title = f"{prefix}: {clean_name}"

        stories.append({
            "story_id": s_id,
            "title": p_title,
            "project_type": p_type,
            "chapters": chaps
        })
        
    if story_id:
        target_sid = story_id.strip()
        stories = [s for s in stories if s["story_id"] == target_sid or (target_sid in ("reels", "dao-ly", "dao_ly") and s["story_id"] in ("reels", "dao-ly", "dao_ly"))]

    return stories

@app.get("/v1/projects/{story_id}/{chapter_id}/status")
async def get_project_status(request: Request, story_id: str, chapter_id: str):
    ws_id = get_workspace_id_from_request(request)
    job_key = f"{story_id}/{chapter_id}"
    job_state = project_jobs.get(job_key, {"status": "idle"}).copy()
    
    chapter_dir = PROJECTS_DIR / story_id / chapter_id
    img_dir = chapter_dir / "images"
    aud_dir = chapter_dir / "audio"
    
    has_images = img_dir.exists() and img_dir.is_dir() and len([f for f in img_dir.iterdir() if not f.name.startswith(".") and f.is_file()]) > 0
    has_audio = aud_dir.exists() and aud_dir.is_dir() and len([f for f in aud_dir.iterdir() if not f.name.startswith(".") and f.is_file()]) > 0
    
    cfg_file = chapter_dir / "project_config.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k in ["art_style", "subtitle_preset", "aspect_ratio", "use_watermark", "use_subtitles", "use_waveform", "effect_type", "voice_id", "tts_provider", "image_generator", "short_title", "start_fragment", "end_fragment"]:
                    if k in cfg and cfg[k] is not None:
                        job_state[k] = cfg[k]
        except Exception:
            pass

    job_state["has_images"] = has_images
    job_state["has_audio"] = has_audio

    # If active agent connected via WebSocket, query real-time chapter status & files from agent
    res = await tunnel_request_to_agent("get_chapter_status_request", {"story_id": story_id, "chapter_id": chapter_id}, workspace_id=ws_id, timeout=3.0)
    if res and isinstance(res, dict):
        for k, v in res.items():
            if v is not None:
                if k == "status" and v == "idle" and job_state.get("status") not in ("idle", "completed", "failed", "queued", None):
                    continue
                job_state[k] = v
        if "has_images" not in res:
            job_state["has_images"] = has_images
        if "has_audio" not in res:
            job_state["has_audio"] = has_audio
        return job_state

    chapter_dir = PROJECTS_DIR / story_id / chapter_id
    final_file = chapter_dir / "final.mp4"
    if not final_file.exists():
        final_file = chapter_dir / f"{story_id}_{chapter_id}.mp4"
        
    if final_file.exists() and job_state.get("status") in ("idle", None):
        job_state["status"] = "completed"
        
    # Dynamically restore fragment count from disk if total_fragments is missing (e.g. after server restart)
    if not job_state.get("total_fragments"):
        max_frags = 0
        for sub_path in ["images", "audio", "videos", "text/story_fragments", "text/image_prompts"]:
            d = chapter_dir / sub_path
            if d.exists() and d.is_dir():
                count = len([f for f in d.iterdir() if not f.name.startswith(".") and not f.name.startswith("processed_") and not f.is_dir()])
                if count > max_frags:
                    max_frags = count
        if max_frags > 0:
            job_state["total_fragments"] = max_frags
            if job_state.get("status") == "completed":
                job_state["current_fragment"] = max_frags
                
    return job_state

class VoiceConfig(BaseModel):
    provider: Optional[str] = None
    voice_id: Optional[str] = None
    omnivoice_mode: Optional[str] = None  # "clone", "design", "auto"
    ref_audio_b64: Optional[str] = None
    ref_audio_filename: Optional[str] = None
    ref_audio_local_path: Optional[str] = None
    ref_text: Optional[str] = None
    voice_instruct: Optional[str] = None
    start_fragment: Optional[int] = 1
    end_fragment: Optional[int] = 5
    limit_fragments: Optional[int] = 0
    speed: Optional[float] = 0.85
    language: Optional[str] = "vi"

class RunPipelineRequest(BaseModel):
    voice_config: Optional[VoiceConfig] = None
    art_style: Optional[str] = None
    aspect_ratio: Optional[str] = "9:16"
    image_generator: Optional[str] = "ima2"
    effect_type: Optional[str] = "leaves"
    story_text: Optional[str] = None
    short_title: Optional[str] = None
    slug: Optional[str] = None
    use_watermark: Optional[bool] = True
    use_waveform: Optional[bool] = True
    use_subtitles: Optional[bool] = True
    subtitle_preset: Optional[str] = "viral-bold-yellow"
    use_whisper: Optional[bool] = False
    force_rerun: Optional[bool] = False
    rerun_mode: Optional[str] = "all"
    start_fragment: Optional[int] = 1
    end_fragment: Optional[int] = 5

class CreateProjectRequest(BaseModel):
    project_type: str  # 'story', 'reels', 'long', 'sketch', 'music'
    project_name: Optional[str] = None
    story_id: Optional[str] = None
    title: Optional[str] = None
    short_title: Optional[str] = None
    language: Optional[str] = "vi"
    aspect_ratio: Optional[str] = None
    voice_config: Optional[dict] = None
    template_slug: Optional[str] = None

@app.get("/v1/lore-keeper/stories")
async def get_lore_keeper_stories():
    return fetch_lore_keeper_stories()

@app.post("/v1/projects/create")
async def create_project(request: Request, body: CreateProjectRequest):
    ws_id = get_workspace_id_from_request(request)
    p_type = body.project_type.lower().strip()
    if p_type not in ("story", "reels", "long", "sketch", "music"):
        p_type = "reels"

    raw_name = body.project_name or body.story_id or body.template_slug or f"project_{int(time.time())}"
    p_name = slugify(raw_name)

    proj_dir = PROJECTS_DIR / p_name
    proj_dir.mkdir(parents=True, exist_ok=True)

    aspect_ratio = body.aspect_ratio or ("16:9" if p_type in ("long", "sketch") else "9:16")
    title = body.title or body.project_name or body.story_id or p_name.replace("-", " ").title()

    items = []
    if p_type == "story":
        sid = body.story_id or p_name
        chaps = fetch_story_chapters(sid)
        items = [{"id": ch["id"], "title": ch["title"], "status": "idle"} for ch in chaps]
        for ch in chaps:
            ch_dir = proj_dir / ch["id"]
            ch_dir.mkdir(parents=True, exist_ok=True)
    else:
        item_id = p_name
        item_title = title
        short_t = body.short_title or title
        item_content = ""
        
        if body.template_slug:
            try:
                tpl = await get_script_template(body.template_slug)
                if tpl:
                    item_title = tpl.get("title", title)
                    short_t = tpl.get("short_title", title)
                    item_content = tpl.get("content", "")
            except Exception as e:
                print(f"[Server] Template load error: {e}")

        item_dir = prepare_chapter_structure(p_name, item_id)
        if item_content:
            (item_dir / "story.txt").write_text(item_content, encoding="utf-8")
            prepare_chapter_structure(p_name, item_id, item_content)

        meta = {
            "episode": 1,
            "episode_label": "Episode 01" if (body.language or "vi") != "vi" else "Tập 01",
            "title": item_title,
            "short_title": short_t,
            "slug": item_id,
            "aspect_ratio": aspect_ratio,
            "language": body.language or "vi",
            "channel": "@playnet.zone-en" if (body.language or "vi") == "en" else "@playnet.zone-vi",
            "content": item_content
        }
        with open(item_dir / "item.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        cfg = {"aspect_ratio": aspect_ratio, "language": body.language or "vi"}
        with open(item_dir / "project_config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(item_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
            f.write(aspect_ratio)

        items.append({
            "id": item_id,
            "title": item_title,
            "short_title": short_t,
            "status": "idle",
            "slug": item_id
        })

    content_data = {
        "project_name": p_name,
        "project_type": p_type,
        "title": title,
        "short_title": body.short_title or title,
        "aspect_ratio": aspect_ratio,
        "language": body.language or "vi",
        "voice_config": body.voice_config or {},
        "created_at": datetime.datetime.now().isoformat(),
        "items": items
    }
    with open(proj_dir / "content.json", "w", encoding="utf-8") as f:
        json.dump(content_data, f, ensure_ascii=False, indent=2)

    try:
        await tunnel_request_to_agent("create_project_request", {
            "project_name": p_name,
            "story_id": p_name,
            "project_type": p_type,
            "title": title,
            "aspect_ratio": aspect_ratio,
            "language": body.language or "vi",
            "items": items
        }, workspace_id=ws_id, timeout=10.0)
    except Exception as err:
        print(f"[Server] Warning: Failed tunneling create_project_request to agent: {err}")

    return {"status": "ok", "project_name": p_name, "project_type": p_type, "content": content_data}

class AddItemRequest(BaseModel):
    title: str
    short_title: Optional[str] = None
    item_id: Optional[str] = None
    slug: Optional[str] = None
    episode: Optional[int] = None
    episode_label: Optional[str] = None
    aspect_ratio: Optional[str] = None
    language: Optional[str] = "vi"
    channel: Optional[str] = "@playnet.zone-vi"
    content: Optional[str] = None

@app.post("/v1/projects/{story_id}/items/add")
async def add_project_item(request: Request, story_id: str, body: AddItemRequest):
    proj_dir = PROJECTS_DIR / story_id
    if not proj_dir.exists():
        proj_dir.mkdir(parents=True, exist_ok=True)

    parent_cfg = {}
    content_file = proj_dir / "content.json"
    if content_file.exists():
        try:
            with open(content_file, "r", encoding="utf-8") as f:
                parent_cfg = json.load(f)
        except Exception:
            pass

    parent_ar = parent_cfg.get("aspect_ratio") if isinstance(parent_cfg, dict) else None
    parent_type = parent_cfg.get("project_type", "") if isinstance(parent_cfg, dict) else ""
    default_ar = parent_ar or ("16:9" if parent_type in ("long", "sketch") or "videos" in story_id or "longform" in story_id else "9:16")
    final_ar = body.aspect_ratio or default_ar

    raw_item = body.slug or body.item_id or body.short_title or body.title
    item_slug = slugify(raw_item) or f"item_{int(time.time())}"

    item_dir = prepare_chapter_structure(story_id, item_slug)

    # Save script content into story.txt and create fragments immediately
    if body.content and body.content.strip():
        story_file = item_dir / "story.txt"
        with open(story_file, "w", encoding="utf-8") as f:
            f.write(body.content.strip())
        prepare_chapter_structure(story_id, item_slug, body.content.strip())

    # Save item.json metadata matching longform / reels format
    display_title = body.short_title or body.title
    ep_num = body.episode if body.episode is not None else 1
    ep_label = body.episode_label or (f"Tập {ep_num:02d}" if (body.language or "vi") == "vi" else f"Episode {ep_num:02d}")
    meta = {
        "episode": ep_num,
        "episode_label": ep_label,
        "title": body.title,
        "short_title": display_title,
        "slug": item_slug,
        "aspect_ratio": final_ar,
        "language": body.language or "vi",
        "channel": body.channel or "@playnet.zone-vi",
        "content": body.content or ""
    }
    with open(item_dir / "item.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Save project_config.json for aspect ratio and language
    cfg = {}
    cfg_file = item_dir / "project_config.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    cfg["aspect_ratio"] = final_ar
    cfg["language"] = body.language or cfg.get("language", "vi")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # Also sync aspect ratio to aspect_ratio.txt
    with open(item_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
        f.write(final_ar)

    content_file = proj_dir / "content.json"
    content_data = {}
    if content_file.exists():
        try:
            with open(content_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    content_data = loaded
                elif isinstance(loaded, list):
                    content_data = {"items": loaded}
        except Exception:
            pass
            
    if not isinstance(content_data, dict):
        content_data = {"items": []}
    if "items" not in content_data or not isinstance(content_data["items"], list):
        content_data["items"] = []
        
    if "project_name" not in content_data:
        content_data["project_name"] = story_id
    if "project_type" not in content_data or not content_data["project_type"]:
        content_data["project_type"] = parent_type or ("long" if "videos" in story_id or "longform" in story_id or "sketch" in story_id else ("reels" if "reels" in story_id else "story"))
    if "aspect_ratio" not in content_data:
        content_data["aspect_ratio"] = final_ar
        
    existing = next((it for it in content_data["items"] if (it.get("id") == item_slug or it.get("slug") == item_slug)), None)
    if not existing:
        new_item = {
            "id": item_slug,
            "slug": item_slug,
            "title": display_title,
            "status": "idle"
        }
        content_data["items"].append(new_item)
    else:
        existing["title"] = display_title

    with open(content_file, "w", encoding="utf-8") as f:
        json.dump(content_data, f, ensure_ascii=False, indent=2)

    ws_id = get_workspace_id_from_request(request)
    try:
        await tunnel_request_to_agent("add_project_item_request", {
            "story_id": story_id,
            "item_id": item_slug,
            "title": display_title,
            "short_title": display_title,
            "slug": item_slug,
            "episode": ep_num,
            "episode_label": ep_label,
            "aspect_ratio": final_ar,
            "language": body.language or "vi",
            "channel": body.channel or "@playnet.zone-vi",
            "content": body.content or ""
        }, workspace_id=ws_id, timeout=10.0)
    except Exception as err:
        print(f"[Server] Warning: Failed tunneling add_project_item_request to agent: {err}")

    return {"ok": True, "item_id": item_slug, "title": display_title}

@app.get("/v1/voice/defaults")
async def get_voice_defaults():
    return {
        "provider": config.get("AUDIO", "TTS_PROVIDER", fallback="edge"),
        "omnivoice_mode": config.get("AUDIO", "OMNIVOICE_MODE", fallback="auto"),
        "ref_audio_local_path": config.get("AUDIO", "OMNIVOICE_REF_AUDIO", fallback=""),
        "ref_text": config.get("AUDIO", "OMNIVOICE_REF_TEXT", fallback=""),
        "voice_instruct": config.get("AUDIO", "OMNIVOICE_INSTRUCT", fallback=""),
        "voice_id": config.get("AUDIO", "VOICE", fallback="vi-VN-HoaiMyNeural")
    }

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


@app.get("/v1/voices")
async def list_voices(request: Request):
    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        res = await tunnel_request_to_agent("list_voices_request", {}, workspace_id=ws_id, timeout=5.0)
        if res and isinstance(res, dict) and isinstance(res.get("voices"), list):
            v_items = res.get("voices")
            for v in v_items:
                if isinstance(v, dict):
                    v_id = v.get("id", "")
                    v["is_protected"] = v_id in ("nam-dao-ly", "nu-doc-truyen")
            return sorted(v_items, key=lambda x: x.get("id", "") if isinstance(x, dict) else "")
            
    # Fallback to local server folder
    voices_list = []
    if VOICES_DIR and VOICES_DIR.exists():
        for item in VOICES_DIR.iterdir():
            if item.is_dir():
                sync_and_migrate_voice_dir(item)
                voice_id = item.name
                has_audio = (item / "ref.wav").exists() or (item / "local_path.txt").exists()
                ref_txt_p = item / "ref_text.txt" if (item / "ref_text.txt").exists() else (item / "ref.txt" if (item / "ref.txt").exists() else None)
                ref_text = ref_txt_p.read_text(encoding="utf-8").strip() if ref_txt_p else ""
                voices_list.append({
                    "id": voice_id,
                    "name": voice_id,
                    "has_audio": has_audio,
                    "has_text": bool(ref_text),
                    "ref_text": ref_text,
                    "is_protected": voice_id in ("nam-dao-ly", "nu-doc-truyen")
                })
    return sorted(voices_list, key=lambda x: x.get("id", "") if isinstance(x, dict) else "")

@app.get("/v1/voices/{voice_id}/ref.wav")
async def get_voice_audio(voice_id: str):
    v_dir = VOICES_DIR / voice_id
    wav_file = v_dir / "ref.wav"
    if not wav_file.exists():
        user_voice = pathlib.Path.home() / ".taka-agent" / "voices" / voice_id / "ref.wav"
        if user_voice.exists():
            wav_file = user_voice
    if wav_file.exists():
        return FileResponse(str(wav_file), media_type="audio/wav")
    raise HTTPException(status_code=404, detail="Reference audio file not found")

@app.post("/v1/voices")
async def create_voice(
    request: Request,
    voice_id: str = Form(...),
    ref_text: str = Form(""),
    local_path: str = Form(""),
    file: Optional[UploadFile] = File(None)
):
    ws_id = get_workspace_id_from_request(request)
    if not voice_id.strip():
        raise HTTPException(status_code=400, detail="Voice ID cannot be empty")
    clean_id = "".join(c for c in voice_id if c.isalnum() or c in ("-", "_")).strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Invalid Voice ID format")
        
    file_b64 = None
    if file is not None and file.filename:
        try:
            import base64
            file_content = await file.read()
            file_b64 = base64.b64encode(file_content).decode("utf-8")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read upload file: {e}")
            
    # Send save command to Agent if connected
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        tunnel_payload = {
            "voice_id": clean_id,
            "ref_text": ref_text,
            "local_path": local_path,
            "ref_audio_b64": file_b64
        }
        res = await tunnel_request_to_agent("save_voice_request", tunnel_payload, workspace_id=ws_id, timeout=10.0)
        print(f"[Server] Saved voice profile on Agent ({ws_id}): {res}")
        if res and res.get("ref_audio_b64"):
            file_b64 = res.get("ref_audio_b64")
        
    # Fallback/also save on Server local disk
    voice_dir = VOICES_DIR / clean_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    if file_b64:
        import base64
        with open(voice_dir / "ref.wav", "wb") as buffer:
            buffer.write(base64.b64decode(file_b64))
        local_path_file = voice_dir / "local_path.txt"
        if local_path_file.exists():
            local_path_file.unlink()
    elif local_path.strip():
        src_path = pathlib.Path(local_path.strip())
        if src_path.exists():
            import shutil
            ext = src_path.suffix.lower() or ".wav"
            dest_file = voice_dir / f"ref{ext}"
            shutil.copy2(str(src_path), str(dest_file))
            if ext != ".wav":
                dest_wav = voice_dir / "ref.wav"
                shutil.copy2(str(src_path), str(dest_wav))
            local_path_file = voice_dir / "local_path.txt"
            if local_path_file.exists():
                local_path_file.unlink()
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
            
    return {"ok": True, "voice_id": clean_id}

@app.delete("/v1/voices/{voice_id}")
async def delete_voice(request: Request, voice_id: str):
    ws_id = get_workspace_id_from_request(request)
    clean_id = "".join(c for c in voice_id if c.isalnum() or c in ("-", "_")).strip()
    
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        await tunnel_request_to_agent("delete_voice_request", {"voice_id": clean_id}, workspace_id=ws_id)
        
    voice_dir = VOICES_DIR / clean_id
    if voice_dir.exists() and voice_dir.is_dir():
        try:
            shutil.rmtree(voice_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete voice profile: {str(e)}")
        return {"ok": True}
@app.get("/v1/scripts/template/{slug}")
async def get_script_template(slug: str):
    import glob
    for fpath in glob.glob("data/kien-thuc/*.json") + glob.glob("data/kien-thuc-longform/*.json") + glob.glob("data/monochromatic_pencil_sketch/*.json"):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("slug") == slug:
                    return data
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="Script template not found")

@app.get("/v1/projects/{story_id}/{chapter_id}/fragments")
async def get_project_fragments(request: Request, story_id: str, chapter_id: str):
    try:
        ws_id = get_workspace_id_from_request(request)
        if ws_id and ws_id in agents_by_workspace:
            res = await tunnel_request_to_agent("get_fragments_request", {
                "story_id": story_id,
                "chapter_id": chapter_id
            }, workspace_id=ws_id, timeout=10.0)
            if res and isinstance(res, dict) and res.get("fragments"):
                return res["fragments"]

        content = ""
        project_dir = PROJECTS_DIR / story_id / chapter_id
        story_file = project_dir / "story.txt"
        if story_file.exists():
            try:
                content = story_file.read_text(encoding="utf-8")
            except Exception:
                pass

        if story_id == "music":
            if not content and (PROJECTS_DIR.parent / "downloaded_albums/music").exists():
                music_story_dir = PROJECTS_DIR.parent / "downloaded_albums/music"
                for p in music_story_dir.glob("*.txt"):
                    if chapter_id.replace("_", " ").replace("-", " ").lower() in p.name.lower():
                        content = p.read_text(encoding="utf-8")
                        break
        elif not content:
            try:
                from fastapi.concurrency import run_in_threadpool
                content = await run_in_threadpool(fetch_chapter_content, chapter_id)
            except Exception as e:
                print(f"[Server] Warning: Failed to fetch fragments from Lore-Keeper: {e}")

        if not content or not content.strip():
            ws_id = get_workspace_id_from_request(request)
            if ws_id and ws_id in agents_by_workspace:
                res = await tunnel_request_to_agent("get_fragments_request", {
                    "story_id": story_id,
                    "chapter_id": chapter_id
                }, workspace_id=ws_id, timeout=10.0)
                if res and isinstance(res, dict) and "fragments" in res:
                    return res["fragments"]
            return []

        if story_id == "music":
            project_dir = PROJECTS_DIR / "music" / chapter_id
            segments_file = project_dir / "segments.json"
            if segments_file.exists():
                try:
                    import json
                    with open(segments_file, "r", encoding="utf-8") as f:
                        segments = json.load(f)
                        return [{"index": i, "text": seg.get("text", "") or f"Slide {i+1}"} for i, seg in enumerate(segments)]
                except Exception:
                    pass
            lines = [l.strip() for l in content.split("\n") if l.strip()]
            return [{"index": i, "text": l} for i, l in enumerate(lines)]

        chapter_dir = PROJECTS_DIR / story_id / chapter_id
        frag_dir = chapter_dir / "text" / "story_fragments"
        
        fragments_list = []
        import re
        if frag_dir.exists() and frag_dir.is_dir():
            frag_files = sorted([f for f in frag_dir.glob("*.txt")], key=lambda f: int(re.search(r'\d+', f.stem).group()) if re.search(r'\d+', f.stem) else 9999)
            if frag_files:
                for f in frag_files:
                    try:
                        t = f.read_text(encoding="utf-8").strip()
                        if t:
                            fragments_list.append(t)
                    except Exception:
                        pass

        if not fragments_list:
            prepare_chapter_structure(story_id, chapter_id, content)
            if frag_dir.exists() and frag_dir.is_dir():
                frag_files = sorted([f for f in frag_dir.glob("*.txt")], key=lambda f: int(re.search(r'\d+', f.stem).group()) if re.search(r'\d+', f.stem) else 9999)
                for f in frag_files:
                    try:
                        t = f.read_text(encoding="utf-8").strip()
                        if t:
                            fragments_list.append(t)
                    except Exception:
                        pass

        if not fragments_list:
            lines = [l.strip() for l in content.split("\n") if l.strip()]
            for line in lines:
                sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', line) if s.strip()]
                for s in sents:
                    if s:
                        fragments_list.append(s)
            if not fragments_list:
                fragments_list = lines

        # Determine configured aspect ratio for chapter_dir
        configured_aspect_ratio = None
        if (chapter_dir / "aspect_ratio.txt").exists():
            try:
                configured_aspect_ratio = (chapter_dir / "aspect_ratio.txt").read_text(encoding="utf-8").strip()
            except Exception:
                pass

        if not configured_aspect_ratio and (chapter_dir / "project_config.json").exists():
            try:
                with open(chapter_dir / "project_config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    configured_aspect_ratio = cfg.get("aspect_ratio")
            except Exception:
                pass

        if not configured_aspect_ratio and (PROJECTS_DIR / story_id / "content.json").exists():
            try:
                with open(PROJECTS_DIR / story_id / "content.json", "r", encoding="utf-8") as f:
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
            p_type = "long"
            if (PROJECTS_DIR / story_id / "content.json").exists():
                try:
                    with open(PROJECTS_DIR / story_id / "content.json", "r", encoding="utf-8") as f:
                        cd = json.load(f)
                        if isinstance(cd, dict):
                            p_type = cd.get("project_type", "long")
                except Exception:
                    pass
            configured_aspect_ratio = "16:9" if p_type in ("long", "sketch") else "9:16"

        from PIL import Image
        result = []
        img_dir = chapter_dir / "images"
        aud_dir = chapter_dir / "audio"
        vid_dir = chapter_dir / "videos"

        for i, frag in enumerate(fragments_list):
            item = {"index": i, "text": frag}
            
            img_url = None
            img_width, img_height = None, None
            aspect_mismatch = False

            if img_dir.exists() and img_dir.is_dir():
                img_stems = {f"image{i}", f"image_{i}", f"frame{i}", f"frame_{i}", str(i)}
                for f in img_dir.iterdir():
                    if f.is_file() and not f.name.startswith(".") and f.stem.lower() in img_stems:
                        img_url = f"/v1/media/{story_id}/{chapter_id}/images/{f.name}"
                        try:
                            with Image.open(f) as img:
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
                        
            aud_url = None
            if aud_dir.exists() and aud_dir.is_dir():
                aud_stems = {f"voiceover{i}", f"voiceover_{i}", f"voice{i}", f"voice_{i}", f"audio{i}", f"audio_{i}", str(i)}
                for f in aud_dir.iterdir():
                    if f.is_file() and not f.name.startswith(".") and f.stem.lower() in aud_stems:
                        aud_url = f"/v1/media/{story_id}/{chapter_id}/audio/{f.name}"
                        break

            vid_url = None
            if vid_dir.exists() and vid_dir.is_dir():
                vid_stems = {f"clip{i}", f"clip_{i}", f"video{i}", f"video_{i}", str(i)}
                for f in vid_dir.iterdir():
                    if f.is_file() and not f.name.startswith(".") and f.stem.lower() in vid_stems:
                        vid_url = f"/v1/media/{story_id}/{chapter_id}/videos/{f.name}"
                        break

            item["image_url"] = img_url
            item["image_width"] = img_width
            item["image_height"] = img_height
            item["aspect_mismatch"] = aspect_mismatch
            item["configured_aspect_ratio"] = configured_aspect_ratio
            item["audio_url"] = aud_url
            item["video_url"] = vid_url
            result.append(item)

        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Server] Error in get_project_fragments: {e}")
        return []

@app.get("/v1/media/{story_id}/{chapter_id}/{sub_dir}/{filename}")
async def get_project_media(story_id: str, chapter_id: str, sub_dir: str, filename: str):
    file_path = PROJECTS_DIR / story_id / chapter_id / sub_dir / filename
    if not file_path.exists():
        for ext in [".png", ".jpg", ".jpeg", ".wav", ".mp3", ".mp4"]:
            alt = PROJECTS_DIR / story_id / chapter_id / sub_dir / f"{filename}{ext}"
            if alt.exists():
                file_path = alt
                break
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return FileResponse(file_path)

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

@app.post("/v1/projects/{story_id}/{chapter_id}/run")
async def run_project_pipeline(request: Request, story_id: str, chapter_id: str, request_data: Optional[RunPipelineRequest] = None):
    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id) if ws_id else None
    
    project_dir = PROJECTS_DIR / story_id / chapter_id
    project_dir.mkdir(parents=True, exist_ok=True)
    story_file = project_dir / "story.txt"

    content = ""
    # Fetch content from Lore-Keeper or use provided story_text
    if story_id != "music":
        if story_file.exists() and story_file.stat().st_size > 0:
            try:
                with open(story_file, "r", encoding="utf-8") as f:
                    content = f.read()
                print(f"[Server] Preserving existing story.txt at {story_file}")
            except Exception:
                pass
        elif request_data and request_data.story_text and request_data.story_text.strip():
            content = request_data.story_text.strip()
            with open(story_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[Server] Successfully wrote custom story_text to {story_file}")
        
        if not content:
            try:
                print(f"[Server] Fetching story content for chapter_id={chapter_id} from Lore-Keeper...")
                from fastapi.concurrency import run_in_threadpool
                content = await run_in_threadpool(fetch_chapter_content, chapter_id)
                with open(story_file, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"[Server] Successfully wrote story content to {story_file}")
            except Exception as e:
                print(f"[Server] Warning: Lore-Keeper fetch failed: {e}")

        if not story_file.exists() and not agent_ws:
            raise HTTPException(status_code=404, detail="story.txt not found. Failed to write chapter content.")

    # Process voice_config if present
    voice_payload = {}
    if request_data and request_data.voice_config:
        vc = request_data.voice_config
        selected_voice_id = vc.voice_id
        
        voice_payload["provider"] = vc.provider or "omnivoice"
        voice_payload["omnivoice_mode"] = getattr(vc, "omnivoice_mode", None) or "clone"
        s_frag = request_data.start_fragment if (request_data and request_data.start_fragment is not None) else (vc.start_fragment or 1)
        e_frag = request_data.end_fragment if (request_data and request_data.end_fragment is not None) else (getattr(vc, "end_fragment", None) or 5)
        voice_payload["start_fragment"] = s_frag
        voice_payload["end_fragment"] = e_frag
        voice_payload["limit_fragments"] = max(0, e_frag - s_frag + 1)
        voice_payload["language"] = vc.language if (vc and getattr(vc, "language", None)) else "vi"
        voice_payload["speed"] = vc.speed if (vc and vc.speed is not None) else 0.85
        
        # Resolve voice profile from voices folder
        if selected_voice_id:
            voice_dir = VOICES_DIR / selected_voice_id
            ref_audio = voice_dir / "ref.wav"
            if not ref_audio.exists():
                for ext in ["mp3", "m4a", "flac", "ogg"]:
                    alt = voice_dir / f"ref.{ext}"
                    if alt.exists():
                        ref_audio = alt
                        break
            local_path_file = voice_dir / "local_path.txt"
            ref_text_file = voice_dir / "ref_text.txt"
            if not ref_text_file.exists():
                ref_text_file = voice_dir / "ref.txt"
            
            if local_path_file.exists():
                try:
                    with open(local_path_file, "r", encoding="utf-8") as f:
                        path_str = f.read().strip()
                    voice_payload["ref_audio_path"] = path_str
                    print(f"[Server] Using local audio path for voice ID: {selected_voice_id} -> {path_str}")
                except Exception as e:
                    print(f"[Server] Failed to read voice profile local path: {e}")
            elif ref_audio.exists():
                try:
                    import base64
                    with open(ref_audio, "rb") as f:
                        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                    voice_payload["ref_audio_b64"] = audio_b64
                    voice_payload["ref_audio_filename"] = ref_audio.name
                    print(f"[Server] Encoded base64 audio for voice ID: {selected_voice_id}")
                except Exception as e:
                    print(f"[Server] Failed to read/encode voice profile audio: {e}")
                    
            if ref_text_file.exists():
                try:
                    with open(ref_text_file, "r", encoding="utf-8") as f:
                        voice_payload["ref_text"] = f.read().strip()
                except Exception as e:
                    print(f"[Server] Failed to read voice profile text: {e}")

    # Initialize job state
    job_key = f"{story_id}/{chapter_id}"
    project_jobs[job_key] = {
        "status": "processing",
        "current_step": "Starting pipeline...",
        "current_fragment": 0,
        "total_fragments": 0,
        "fragment_status": {},
        "error": None
    }

    art_style = request_data.art_style if request_data else None
    effect_type = request_data.effect_type if (request_data and hasattr(request_data, 'effect_type')) else "leaves"
    use_watermark = request_data.use_watermark if (request_data and request_data.use_watermark is not None) else False
    use_waveform = request_data.use_waveform if (request_data and hasattr(request_data, 'use_waveform') and request_data.use_waveform is not None) else False
    use_subtitles = request_data.use_subtitles if (request_data and request_data.use_subtitles is not None) else True
    subtitle_preset = request_data.subtitle_preset if (request_data and hasattr(request_data, 'subtitle_preset') and request_data.subtitle_preset) else "viral-bold-yellow"
    use_whisper = request_data.use_whisper if (request_data and hasattr(request_data, 'use_whisper')) else False
    force_rerun = request_data.force_rerun if request_data else False

    # Read and encode music file if it's a music project
    music_b64 = None
    music_filename = None
    music_local_path = None
    if story_id == "music":
        local_music_path_file = project_dir / "local_music_path.txt"
        if local_music_path_file.exists():
            try:
                with open(local_music_path_file, "r", encoding="utf-8") as f:
                    music_local_path = f.read().strip()
                print(f"[Server] Using local music path: {music_local_path}")
            except Exception as e:
                print(f"[Server] Failed to read local music path file: {e}")
        else:
            music_files = list(project_dir.glob("music.*"))
            if music_files:
                music_file = music_files[0]
                try:
                    import base64
                    with open(music_file, "rb") as f:
                        music_b64 = base64.b64encode(f.read()).decode("utf-8")
                    music_filename = music_file.name
                except Exception as e:
                    print(f"[Server] Failed to read/encode music file: {e}")

    short_title = request_data.short_title.strip() if (request_data and hasattr(request_data, 'short_title') and request_data.short_title) else None
    slug = request_data.slug.strip() if (request_data and hasattr(request_data, 'slug') and request_data.slug) else None
    image_generator = request_data.image_generator if (request_data and hasattr(request_data, 'image_generator') and request_data.image_generator) else "ima2"
    rerun_mode = normalize_rerun_mode(request_data.rerun_mode if request_data else "all")

    # Determine aspect ratio
    req_aspect = request_data.aspect_ratio if (request_data and hasattr(request_data, 'aspect_ratio') and request_data.aspect_ratio) else None
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
    if not req_aspect:
        parent_content_file = project_dir.parent / "content.json"
        if parent_content_file.exists():
            try:
                with open(parent_content_file, "r", encoding="utf-8") as f:
                    p_content = json.load(f)
                if isinstance(p_content, dict):
                    req_aspect = p_content.get("aspect_ratio")
                    p_type = p_content.get("project_type", "")
                    if not req_aspect and p_type in ("long", "sketch"):
                        req_aspect = "16:9"
            except Exception:
                pass

    is_long_dir = ("long" in story_id.lower() or "videos" in story_id.lower() or "sketch" in story_id.lower())
    aspect_ratio = req_aspect or ("16:9" if is_long_dir else "9:16")

    # Save project configuration for video engine (preserving existing fields)
    config_data = {}
    cfg_path = project_dir / "project_config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass

    config_data.update({
        "art_style": art_style,
        "subtitle_preset": subtitle_preset,
        "use_watermark": use_watermark,
        "use_waveform": use_waveform,
        "use_subtitles": use_subtitles,
        "image_generator": image_generator,
        "effect_type": effect_type,
        "aspect_ratio": aspect_ratio
    })
    if short_title:
        config_data["short_title"] = short_title
    if slug:
        config_data["slug"] = slug

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        with open(project_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
            f.write(aspect_ratio)
        if short_title:
            with open(project_dir / "short_title.txt", "w", encoding="utf-8") as f:
                f.write(short_title)
        if slug:
            with open(project_dir / "slug.txt", "w", encoding="utf-8") as f:
                f.write(slug)
    except Exception as err:
        print(f"[Server] Failed to write project_config.json: {err}")

    if not agent_ws:
        try:
            import taka_agent
            project_name = f"{story_id}_{chapter_id}"
            
            class FakeWS:
                async def send(self, msg_str):
                    try:
                        data = json.loads(msg_str)
                        if data.get("type") == "pipeline_progress":
                            job_key = f"{story_id}/{chapter_id}"
                            project_jobs[job_key] = {
                                "status": data.get("status"),
                                "queue_position": data.get("queue_position", 0),
                                "total_queued": data.get("total_queued", 0),
                                "current_fragment": data.get("current_fragment", 0),
                                "total_fragments": data.get("total_fragments", 0),
                                "fragment_status": data.get("fragment_status", {}),
                                "error": data.get("error"),
                                "updated_at": data.get("updated_at")
                            }
                    except Exception:
                        pass
            
            fake_ws = FakeWS()
            res_queue = await taka_agent.enqueue_or_run_job(
                project_name, str(project_dir), fake_ws,
                voice_config=voice_payload, art_style=art_style,
                use_watermark=use_watermark, use_waveform=use_waveform,
                use_subtitles=use_subtitles, subtitle_preset=subtitle_preset,
                use_whisper=use_whisper, story_text=content, force_rerun=force_rerun,
                effect_type=effect_type, pipeline_type="music" if story_id == "music" else "story",
                music_b64=music_b64, music_filename=music_filename, music_local_path=music_local_path,
                image_generator=image_generator, rerun_mode=rerun_mode, aspect_ratio=aspect_ratio
            )
            return {
                "message": f"Pipeline run {'queued' if res_queue.get('status') == 'queued' else 'triggered'} locally on Taka-Server",
                "story_id": story_id,
                "chapter_id": chapter_id,
                "status": res_queue.get("status")
            }
        except Exception as local_err:
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Failed to trigger/queue pipeline: {str(local_err)}")

    # Send trigger message to target workspace agent
    trigger_message = {
        "type": "run_pipeline",
        "payload": {
            "project_name": f"{story_id}_{chapter_id}",
            "project_path": str(project_dir),
            "voice_config": voice_payload if voice_payload else None,
            "pipeline_type": "music" if story_id == "music" else ("dao_ly" if (story_id in ("dao-ly", "dao_ly") or story_id.startswith("dao_ly_") or story_id.startswith("dao-ly-")) else "story"),
            "art_style": art_style,
            "image_generator": image_generator,
            "effect_type": effect_type,
            "use_watermark": use_watermark,
            "use_waveform": use_waveform,
            "use_subtitles": use_subtitles,
            "subtitle_preset": subtitle_preset,
            "use_whisper": use_whisper,
            "force_rerun": force_rerun,
            "rerun_mode": rerun_mode,
            "aspect_ratio": aspect_ratio,
            "story_text": content if story_id != "music" else None,
            "music_b64": music_b64,
            "music_filename": music_filename,
            "music_local_path": music_local_path
        }
    }
    await agent_ws.send_text(json.dumps(trigger_message))
    return {"message": "Pipeline run triggered on Taka-Agent", "story_id": story_id, "chapter_id": chapter_id}

@app.post("/v1/projects/{story_id}/{chapter_id}/config")
async def save_project_config_endpoint(request: Request, story_id: str, chapter_id: str):
    ws_id = get_workspace_id_from_request(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    project_dir = PROJECTS_DIR / story_id / chapter_id
    project_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = project_dir / "project_config.json"
    
    config_data = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass

    for k in ["art_style", "subtitle_preset", "aspect_ratio", "use_watermark", "use_subtitles", "use_waveform", "image_generator", "effect_type", "voice_id", "tts_provider", "voice_speed", "start_fragment", "end_fragment"]:
        if k in body and body[k] is not None:
            config_data[k] = body[k]

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        if "aspect_ratio" in config_data:
            with open(project_dir / "aspect_ratio.txt", "w", encoding="utf-8") as f:
                f.write(str(config_data["aspect_ratio"]))
    except Exception as err:
        print(f"[Server] Error writing project_config.json: {err}")

    # Tunnel config update to active workspace agent if connected
    await tunnel_request_to_agent("save_project_config_request", {
        "story_id": story_id,
        "chapter_id": chapter_id,
        "config": config_data
    }, workspace_id=ws_id, timeout=3.0)

    return {"status": "ok", "config": config_data}

@app.post("/v1/projects/{story_id}/{chapter_id}/cancel")
async def cancel_project_pipeline(request: Request, story_id: str, chapter_id: str):
    ws_id = get_workspace_id_from_request(request)
    job_key = f"{story_id}/{chapter_id}"
    
    if job_key in project_jobs:
        project_jobs[job_key]["status"] = "idle"
        project_jobs[job_key]["current_step"] = "Canceled by user"
        
    res = await tunnel_request_to_agent("cancel_chapter_job_request", {"story_id": story_id, "chapter_id": chapter_id}, workspace_id=ws_id, timeout=5.0)
    return {"status": "ok", "message": f"Canceled pipeline for {job_key}", "agent_response": res}

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Welcome to Taka Tales</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #09090e;
                --card-bg: rgba(17, 17, 27, 0.7);
                --border: rgba(255, 255, 255, 0.08);
                --text: #e2e8f0;
                --text-muted: #94a3b8;
                --primary: #8b5cf6;
                --primary-dark: #6d28d9;
                --primary-light: #a78bfa;
                --success: #10b981;
                --danger: #ef4444;
            }

            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

            body {
                font-family: 'Outfit', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 2rem;
                background-image: 
                    radial-gradient(at 0% 0%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
                    radial-gradient(at 100% 100%, rgba(236, 72, 153, 0.1) 0px, transparent 50%);
            }

            .glass-card {
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 20px;
                padding: 3rem;
                max-width: 650px;
                width: 100%;
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
            }

            h2 {
                color: var(--primary-light);
                font-weight: 800;
                font-size: 2rem;
                margin-top: 1rem;
                margin-bottom: 0.5rem;
            }

            h3 {
                color: var(--text);
                font-weight: 600;
                font-size: 1.25rem;
                margin-bottom: 1.5rem;
            }

            h4 {
                color: var(--text);
                font-weight: 600;
                font-size: 1rem;
                margin-bottom: 0.5rem;
            }

            p {
                color: var(--text-muted);
                font-size: 0.95rem;
                line-height: 1.6;
            }

            .code-box-wrapper {
                position: relative;
                display: flex;
                align-items: center;
                background: rgba(0, 0, 0, 0.4);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 0.8rem 1rem;
                font-family: monospace;
                font-size: 0.85rem;
                color: var(--success);
                overflow-x: auto;
                white-space: nowrap;
                margin-top: 0.5rem;
            }

            code {
                flex: 1;
                margin-right: 1rem;
            }

            .copy-btn {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid var(--border);
                border-radius: 6px;
                color: var(--text);
                padding: 0.4rem 0.8rem;
                font-size: 0.75rem;
                cursor: pointer;
                transition: all 0.2s ease;
            }

            .copy-btn:hover {
                background: rgba(255, 255, 255, 0.15);
                border-color: var(--primary-light);
            }

            pre {
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 1rem;
                font-family: monospace;
                font-size: 0.85rem;
                color: var(--text);
                margin-top: 0.5rem;
                line-height: 1.5;
            }

            .status-box {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                background: rgba(239, 68, 68, 0.1);
                border: 1px solid rgba(239, 68, 68, 0.2);
                color: var(--danger);
                border-radius: 8px;
                padding: 1rem;
                font-size: 0.9rem;
                margin-top: 1.5rem;
            }

            .badge-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: var(--danger);
                box-shadow: 0 0 8px var(--danger);
                display: inline-block;
                transition: all 0.3s ease;
            }

            .btn-dashboard {
                display: inline-block;
                width: 100%;
                text-align: center;
                text-decoration: none;
                background: linear-gradient(135deg, var(--primary), var(--primary-dark));
                color: #fff;
                padding: 0.8rem;
                border-radius: 8px;
                font-weight: 600;
                font-size: 0.95rem;
                border: 1px solid rgba(255, 255, 255, 0.1);
                box-shadow: 0 4px 15px rgba(139, 92, 246, 0.3);
                margin-top: 2rem;
                transition: all 0.2s ease;
            }

            .btn-dashboard:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(139, 92, 246, 0.5);
            }
        </style>
    </head>
    <body>
        <div class="glass-card">
            <div style="text-align: center; margin-bottom: 2.5rem;">
                <span style="font-size: 3.5rem;">👋</span>
                <h2>Welcome to Taka Tales</h2>
                <p>Connect your local computing Agent to begin generating high-quality animated story videos.</p>
            </div>

            <div style="border-top: 1px solid var(--border); padding-top: 2rem;">
                <h3 style="color: var(--success); display: flex; align-items: center; gap: 0.5rem;">
                    <span>💻</span> Step-by-Step Taka Agent Installation
                </h3>

                <div style="margin-bottom: 1.5rem;">
                    <h4>Option A: macOS / Linux (Terminal)</h4>
                    <p style="font-size: 0.85rem; margin-bottom: 0.5rem;">
                        Run this command to create environment, install packages, and clone OmniVoice automatically:
                    </p>
                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-top: 0.5rem;">
                        <div class="code-box-wrapper" style="flex: 1; margin-top: 0; overflow-x: auto; background: rgba(0, 0, 0, 0.4); border: 1px solid var(--border); border-radius: 8px; padding: 0.8rem 1rem; font-family: monospace; font-size: 0.85rem; color: var(--success); white-space: nowrap;">
                            <code id="cmd-mac">curl -fsSL <span class="server-origin-placeholder"></span>/v1/system/install-agent.sh | bash</code>
                        </div>
                        <button class="copy-btn" onclick="copyCommand('cmd-mac')" style="height: 38px; padding: 0 1.2rem; white-space: nowrap;">Copy</button>
                    </div>
                </div>

                <div style="margin-bottom: 1.5rem;">
                    <h4>Option B: Windows (PowerShell - Run as Administrator)</h4>
                    <p style="font-size: 0.85rem; margin-bottom: 0.5rem;">
                        Run this command in PowerShell to automatically install all dependencies and setup OmniVoice:
                    </p>
                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-top: 0.5rem;">
                        <div class="code-box-wrapper" style="flex: 1; margin-top: 0; overflow-x: auto; background: rgba(0, 0, 0, 0.4); border: 1px solid var(--border); border-radius: 8px; padding: 0.8rem 1rem; font-family: monospace; font-size: 0.85rem; color: var(--success); white-space: nowrap;">
                            <code id="cmd-win">powershell -ExecutionPolicy Bypass -Command "Invoke-Expression (Invoke-RestMethod -Uri '<span class="server-origin-placeholder"></span>/v1/system/install-agent.ps1')"</code>
                        </div>
                        <button class="copy-btn" onclick="copyCommand('cmd-win')" style="height: 38px; padding: 0 1.2rem; white-space: nowrap;">Copy</button>
                    </div>
                </div>

                <div id="welcome-agent-status" class="status-box">
                    <span id="welcome-status-dot" class="badge-dot"></span>
                    <span id="welcome-status-text">Waiting for Taka Agent to connect...</span>
                </div>

                <a href="/" class="btn-dashboard">Go to Dashboard ➜</a>
            </div>
        </div>

        <script>
            function copyCommand(id) {
                let text = document.getElementById(id).innerText;
                navigator.clipboard.writeText(text);
                
                let btn = document.querySelector(`button[onclick="copyCommand('${id}')"]`);
                let origText = btn.innerText;
                btn.innerText = "Copied!";
                btn.style.background = "var(--success)";
                btn.style.color = "#000";
                setTimeout(() => {
                    btn.innerText = origText;
                    btn.style.background = "rgba(255, 255, 255, 0.08)";
                    btn.style.color = "var(--text)";
                }, 1500);
            }

            // Fill all placeholders with the current origin
            document.querySelectorAll(".server-origin-placeholder").forEach(el => {
                el.innerText = window.location.origin;
            });

            (function checkUrlWorkspace() {
                try {
                    let urlParams = new URLSearchParams(window.location.search);
                    let wsParam = urlParams.get("ws") || urlParams.get("workspace_id") || urlParams.get("workspace");
                    if (wsParam && wsParam.trim()) {
                        localStorage.setItem("taka_workspace_id", wsParam.trim());
                    }
                } catch(e) {}
            })();

            const originalFetch = window.fetch;
            window.fetch = async function(...args) {
                let [resource, config] = args;
                config = config || {};
                config.headers = config.headers || {};
                let wsId = localStorage.getItem("taka_workspace_id");
                if (wsId && wsId.trim()) {
                    if (config.headers instanceof Headers) {
                        config.headers.set("X-Workspace-ID", wsId.trim());
                    } else if (Array.isArray(config.headers)) {
                        config.headers.push(["X-Workspace-ID", wsId.trim()]);
                    } else {
                        config.headers["X-Workspace-ID"] = wsId.trim();
                    }
                }
                return originalFetch(resource, config);
            };

            async function updateAgentStatus() {
                try {
                    let res = await fetch("/v1/agent/status");
                    let data = await res.json();
                    let welcomeStatus = document.getElementById("welcome-agent-status");
                    let welcomeText = document.getElementById("welcome-status-text");
                    let welcomeDot = document.getElementById("welcome-status-dot");

                    if (data.connected) {
                        if (data.needs_update) {
                            if (welcomeStatus) {
                                welcomeStatus.style.background = "rgba(245, 158, 11, 0.1)";
                                welcomeStatus.style.borderColor = "rgba(245, 158, 11, 0.2)";
                                welcomeStatus.style.color = "#f59e0b";
                            }
                            if (welcomeText) {
                                welcomeText.innerHTML = `Taka Agent Connected (v${data.agent_version}) but an update is available (v${data.server_version})! Run the installer above to update.`;
                            }
                            if (welcomeDot) {
                                welcomeDot.style.background = "#f59e0b";
                                welcomeDot.style.boxShadow = "0 0 8px #f59e0b";
                            }
                        } else {
                            if (welcomeStatus) {
                                welcomeStatus.style.background = "rgba(16, 185, 129, 0.1)";
                                welcomeStatus.style.borderColor = "rgba(16, 185, 129, 0.2)";
                                welcomeStatus.style.color = "#10b981";
                            }
                            if (welcomeText) {
                                welcomeText.innerText = "Taka Agent connected successfully!";
                            }
                            if (welcomeDot) {
                                welcomeDot.style.background = "#10b981";
                                welcomeDot.style.boxShadow = "0 0 8px #10b981";
                            }
                        }
                    } else {
                        if (welcomeStatus) {
                            welcomeStatus.style.background = "rgba(239, 68, 68, 0.1)";
                            welcomeStatus.style.borderColor = "rgba(239, 68, 68, 0.2)";
                            welcomeStatus.style.color = "var(--danger)";
                        }
                        if (welcomeText) {
                            welcomeText.innerText = "Waiting for Taka Agent to connect...";
                        }
                        if (welcomeDot) {
                            welcomeDot.style.background = "var(--danger)";
                            welcomeDot.style.boxShadow = "0 0 8px var(--danger)";
                        }
                    }
                } catch(e) {}
            }

            setInterval(updateAgentStatus, 2000);
            updateAgentStatus();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(
        content=html_content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

# HTML Dashboard using rich dark glassmorphism styling
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_content = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Taka-Agent Story Studio — Google Flow</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #09090e;
            --primary: #8b5cf6;
            --primary-glow: rgba(139, 92, 246, 0.4);
            --success: #10b981;
            --success-glow: rgba(16, 185, 129, 0.3);
            --warning: #f59e0b;
            --card-bg: rgba(255, 255, 255, 0.03);
            --border: rgba(255, 255, 255, 0.08);
            --text: #f3f4f6;
            --text-muted: #9ca3af;
        }

        html {
            font-size: 13.5px;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Outfit', sans-serif;
            font-size: 0.9rem;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            background-image: radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.15) 0%, transparent 40%),
                              radial-gradient(circle at 90% 80%, rgba(16, 185, 129, 0.08) 0%, transparent 40%);
            padding: 1.2rem 1.8rem;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1rem;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .logo-container { display: flex; align-items: center; gap: 0.8rem; }

        .logo-icon {
            font-size: 2rem;
            background: linear-gradient(135deg, var(--primary), #ec4899);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800;
        }

        h1 { font-size: 1.5rem; font-weight: 800; letter-spacing: -0.05em; }

        .header-menu {
            display: flex; gap: 0.5rem; background: rgba(255, 255, 255, 0.03);
            padding: 0.3rem; border-radius: 12px; border: 1px solid var(--border);
        }

        .header-menu a {
            color: var(--text-muted); padding: 0.6rem 1.4rem; border-radius: 8px;
            font-weight: 700; font-size: 0.95rem; cursor: pointer; transition: all 0.2s ease;
            text-decoration: none;
        }

        .header-menu a:hover { color: #fff; background: rgba(255,255,255,0.05); }

        .header-menu a.active {
            background: linear-gradient(135deg, var(--primary), #7c3aed);
            color: #fff; box-shadow: 0 4px 15px var(--primary-glow);
        }

        .agent-badge {
            display: flex; align-items: center; gap: 0.5rem; padding: 0.5rem 1.2rem;
            border-radius: 50px; background: var(--card-bg); border: 1px solid var(--border);
            font-size: 0.85rem; font-weight: 600; cursor: pointer; user-select: none;
            transition: all 0.2s ease;
        }

        .agent-badge:hover { background: rgba(255,255,255,0.07); border-color: var(--primary); }

        .badge-dot { width: 8px; height: 8px; border-radius: 50%; background-color: var(--text-muted); }

        .agent-badge.connected .badge-dot {
            background-color: var(--success); box-shadow: 0 0 10px var(--success-glow);
        }

        /* Agent Dropdown Menu */
        .agent-dropdown-container { position: relative; }

        .agent-dropdown-menu {
            position: absolute;
            top: 125%;
            right: 0;
            background: rgba(18, 18, 28, 0.95);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 1.1rem;
            width: 280px;
            box-shadow: 0 16px 40px rgba(0,0,0,0.6), 0 0 0 1px rgba(139, 92, 246, 0.2);
            z-index: 1000;
        }

        .dropdown-header {
            border-bottom: 1px solid var(--border);
            padding-bottom: 0.6rem;
            margin-bottom: 0.8rem;
            font-size: 0.9rem;
            font-weight: 800;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .dropdown-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.8rem;
            margin-bottom: 0.52rem;
        }
        .dropdown-item:last-child { margin-bottom: 0; }

        .dropdown-label { color: var(--text-muted); font-weight: 600; }
        .dropdown-val { font-weight: 700; color: #fff; background: rgba(255,255,255,0.05); padding: 0.2rem 0.5rem; border-radius: 4px; border: 1px solid var(--border); }

        .cat-pill {
            background: rgba(255,255,255,0.03); border: 1px solid var(--border);
            color: var(--text-muted); padding: 0.45rem 1.2rem; border-radius: 50px;
            font-size: 0.85rem; font-weight: 700; cursor: pointer; transition: all 0.2s ease;
        }

        .cat-pill:hover, .cat-pill.active {
            background: rgba(139, 92, 246, 0.2); border-color: var(--primary); color: #fff;
            box-shadow: 0 0 12px var(--primary-glow);
        }

        /* Projects Grid (Google Flow) */
        .projects-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 1.5rem;
            margin-top: 1.5rem;
        }

        .project-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.5rem;
            cursor: pointer;
            transition: all 0.25s ease;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 200px;
            position: relative;
        }

        .project-card:hover {
            transform: translateY(-5px);
            border-color: var(--primary);
            background: rgba(139, 92, 246, 0.07);
            box-shadow: 0 12px 35px rgba(139, 92, 246, 0.2);
        }

        .proj-category-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.25rem 0.75rem;
            border-radius: 50px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .proj-category-badge.story { background: rgba(139, 92, 246, 0.2); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.4); }
        .proj-category-badge.reels { background: rgba(236, 72, 153, 0.2); color: #f472b6; border: 1px solid rgba(236, 72, 153, 0.4); }
        .proj-category-badge.long { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
        .proj-category-badge.sketch { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
        .proj-category-badge.music { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }

        .create-project-card {
            border: 2px dashed rgba(139, 92, 246, 0.4);
            background: rgba(139, 92, 246, 0.02);
            align-items: center;
            justify-content: center;
            text-align: center;
        }

        .create-project-card:hover {
            border-color: var(--primary);
            background: rgba(139, 92, 246, 0.08);
        }

        .glass-card {
            background: var(--card-bg); backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px); border: 1px solid var(--border);
            border-radius: 16px; padding: 1.2rem;
        }

        .chapter-item {
            display: flex; justify-content: space-between; align-items: center;
            padding: 0.75rem 0.9rem; border-radius: 8px; background: rgba(255, 255, 255, 0.01);
            border: 1px solid var(--border); cursor: pointer; transition: all 0.2s ease;
            margin-bottom: 0.4rem;
        }

        .chapter-item:hover {
            background: rgba(139, 92, 246, 0.08); border-color: rgba(139, 92, 246, 0.5);
            transform: translateX(4px);
        }

        .chapter-item.active {
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.25), rgba(124, 58, 237, 0.15)) !important;
            border: 1px solid var(--primary) !important;
            box-shadow: 0 0 12px rgba(139, 92, 246, 0.35) !important;
            transform: translateX(4px);
        }

        .run-btn {
            background: linear-gradient(135deg, var(--primary), #a78bfa);
            border: none; color: #fff; padding: 0.6rem 1.2rem; border-radius: 8px;
            font-weight: 700; font-size: 0.85rem; cursor: pointer;
            box-shadow: 0 4px 15px var(--primary-glow); transition: all 0.2s ease;
        }

        .run-btn:hover { transform: translateY(-2px); box-shadow: 0 6px 20px var(--primary-glow); }

        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0,0,0,0.7); backdrop-filter: blur(8px);
            display: flex; justify-content: center; align-items: center; z-index: 1000;
        }

        .cat-choice {
            background: rgba(255,255,255,0.02); border: 1px solid var(--border);
            border-radius: 10px; padding: 0.8rem; text-align: center; cursor: pointer;
            transition: all 0.2s ease;
        }

        .cat-choice:hover, .cat-choice.active {
            background: rgba(139, 92, 246, 0.15); border-color: var(--primary);
        }
    </style>
</head>
<body>
    <header id="main-app-header">
        <div class="logo-container">
            <div class="logo-icon">🌌</div>
            <div>
                <h1>Taka Tales Studio</h1>
                <p style="color: var(--text-muted); font-size: 0.85rem;">Google Flow AI Studio • Multi-Agent Pipeline</p>
            </div>
        </div>

        <nav class="header-menu" id="header-menu-nav">
            <a id="nav-home" onclick="showPage('home')" class="active">🏠 Home</a>
            <a id="nav-voices" onclick="showPage('voices')">🎙️ Voices</a>
        </nav>

        <div style="display: flex; gap: 1rem; align-items: center;">
            <!-- Agent Online Dropdown Wrapper -->
            <div class="agent-dropdown-container">
                <div class="agent-badge" id="agent-badge" onclick="toggleAgentDropdown(event)" title="Click to view Agent details">
                    <div class="badge-dot" id="badge-dot"></div>
                    <span id="agent-status">Connecting...</span>
                    <span style="font-size: 0.7rem; color: var(--text-muted); margin-left: 0.2rem;">▼</span>
                </div>

                <!-- Agent Details Dropdown Card -->
                <div id="agent-dropdown" class="agent-dropdown-menu" style="display: none;" onclick="event.stopPropagation()">
                    <div class="dropdown-header">
                        <span id="dropdown-status-title">🔴 Agent Offline</span>
                        <span style="font-size: 0.75rem; color: var(--primary); background: rgba(139,92,246,0.15); padding: 0.1rem 0.4rem; border-radius: 4px;">AGY v0.4.4</span>
                    </div>
                    <div class="dropdown-item">
                        <span class="dropdown-label">Workspace:</span>
                        <span id="dropdown-workspace-name" class="dropdown-val">huutq_d23b05</span>
                    </div>
                    <div class="dropdown-item">
                        <span class="dropdown-label">Agent Version:</span>
                        <span id="dropdown-agent-version" class="dropdown-val">v0.4.4</span>
                    </div>
                    <div class="dropdown-item">
                        <span class="dropdown-label">Hardware Acceleration:</span>
                        <span id="dropdown-hardware-info" class="dropdown-val">MPS (Apple Silicon)</span>
                    </div>
                    <div class="dropdown-item">
                        <span class="dropdown-label">OmniVoice Engine:</span>
                        <span id="dropdown-omnivoice-status" class="dropdown-val">Active (v0.1.0)</span>
                    </div>
                </div>
            </div>

            <button class="run-btn" style="background: linear-gradient(135deg, #10b981, #059669);" onclick="openNewProjectModal()">
                ✨ + New Project
            </button>
        </div>
    </header>

    <!-- 1. GOOGLE FLOW PROJECTS HOME VIEW -->
    <div id="view-projects-home">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 1rem;">
            <div>
                <h2 style="font-size: 1.5rem; font-weight: 800; color: #fff;">📂 Projects Directory</h2>
                <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem;">Select a project workspace or create a new project</p>
            </div>
            
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                <button class="cat-pill active" onclick="filterCategory('all', this)">🌐 All</button>
                <button class="cat-pill" onclick="filterCategory('story', this)">📖 Story</button>
                <button class="cat-pill" onclick="filterCategory('reels', this)">📱 Reels</button>
                <button class="cat-pill" onclick="filterCategory('long', this)">🎬 Long</button>
                <button class="cat-pill" onclick="filterCategory('sketch', this)">✏️ Sketch</button>
                <button class="cat-pill" onclick="filterCategory('music', this)">🎵 Music</button>
            </div>
        </div>

        <!-- Project Cards Grid -->
        <div class="projects-grid" id="projects-cards-container"></div>
    </div>

    <!-- 2. PROJECT WORKSPACE VIEW (Shown when opening a project) -->
    <div id="view-project-workspace" style="display: none;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem;">
            <div style="display: flex; align-items: center; gap: 1rem;">
                <button onclick="backToProjectsHome()" style="background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: #fff; padding: 0.5rem 1rem; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 0.85rem;">← Back to Projects</button>
                <div>
                    <h2 id="active-project-title" style="font-size: 1.4rem; font-weight: 800; color: #fff;">Project Workspace</h2>
                    <span id="active-project-badge" class="proj-category-badge story">📖 Story</span>
                </div>
            </div>
            <button class="run-btn" style="padding: 0.45rem 1rem; font-size: 0.85rem;" onclick="openAddItemModal()">✨ + Add Item</button>
        </div>

        <div style="display: grid; grid-template-columns: 320px 1fr; gap: 1.5rem;">
            <!-- Left Panel: Chapters/Videos inside THIS project -->
            <div class="glass-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; border-bottom: 1px solid var(--border); padding-bottom: 0.6rem;">
                    <h3 style="font-size: 1rem; color: #fff;">📋 Project Items</h3>
                    <div id="active-project-stats" style="font-size: 0.85rem; color: var(--text-muted); font-weight: 600;"></div>
                </div>
                <div id="workspace-chapters-list"></div>
            </div>

            <!-- Right Panel: Workspace details for selected chapter -->
            <div class="glass-card">
                <div id="details-panel" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem; margin-bottom: 1rem;">
                        <div>
                            <h2 id="current-chapter-title" style="font-size: 1.25rem; font-weight: 800; color: #fff;">Select a Chapter</h2>
                            <div style="display: flex; align-items: center; gap: 0.8rem; margin-top: 0.2rem;">
                                <p id="current-chapter-subtitle" style="font-size: 0.8rem; color: var(--text-muted);">Chapter Details</p>
                                <button id="btn-preview-final" style="background: linear-gradient(135deg, #10b981, #059669); border: 1px solid #059669; color: #fff; padding: 0.3rem 0.8rem; border-radius: 6px; font-size: 0.8rem; font-weight: 700; cursor: pointer; display: none; align-items: center; gap: 0.4rem; box-shadow: 0 0 10px rgba(16, 185, 129, 0.4);" onclick="openFinalVideoPreview()" title="Xem Video Final hoàn chỉnh">▶️ Xem Video Final</button>
                            </div>
                        </div>
                        <div id="status-banner" style="font-size: 0.8rem; padding: 0.3rem 0.8rem; border-radius: 6px; background: rgba(255,255,255,0.05); border: 1px solid var(--border);">Ready</div>
                    </div>

                    <!-- Voice & Art Settings -->
                    <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border); border-radius: 12px; padding: 1.2rem; margin-bottom: 1.5rem;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                            <h4 style="font-size: 0.95rem; font-weight: 700; color: var(--primary);">⚙️ Pipeline Settings</h4>
                            <span style="font-size: 0.75rem; color: var(--text-muted);">Voice, Style, Aspect Ratio, Subtitles & Rerun Controls</span>
                        </div>
                        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.2rem;">
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">🎙️ Voice Profile</label>
                                <select id="voice-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;"></select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">📢 TTS Provider</label>
                                <select id="tts-provider-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="omnivoice">OmniVoice (Local GPU Voice Clone)</option>
                                    <option value="edge">EdgeTTS (Microsoft Cloud Voice)</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">🎨 Art Style</label>
                                <select id="art-style-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="watercolor">🎨 Watercolor Painting</option>
                                    <option value="thuy_mac_blackwhite">⚫ Thủy Mặc Black & White</option>
                                    <option value="2d-stick-figure-cartoon">🧸 2D Stick Figure Cartoon</option>
                                    <option value="monochromatic_pencil_sketch">✏️ Pencil Sketch (Dark Grimdark)</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">📐 Aspect Ratio</label>
                                <select id="aspect-ratio-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="9:16">📱 Vertical 9:16 (Reels/Shorts)</option>
                                    <option value="16:9">🎬 Horizontal 16:9 (YouTube Long)</option>
                                    <option value="1:1">🔲 Square 1:1 (Post)</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">💬 Subtitle Preset</label>
                                <select id="subtitle-preset-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="viral-bold-yellow">💛 Viral Bold Yellow</option>
                                    <option value="storytelling-serif">📜 Storytelling Serif</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">✨ Visual Effect</label>
                                <select id="effect-type-select" onchange="saveCurrentChapterConfig()" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="none" selected>🚫 No Effect</option>
                                    <option value="leaves">🍃 Falling Leaves</option>
                                    <option value="snow">❄️ Falling Snow</option>
                                    <option value="rain">🌧️ Cinematic Rain</option>
                                    <option value="sparkles">✨ Light Sparkles</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">🔢 From Fragment</label>
                                <input type="number" id="frag-start-input" min="1" value="1" oninput="updateFragmentHighlights(); saveCurrentChapterConfig();" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" />
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">🔢 To Fragment</label>
                                <input type="number" id="frag-end-input" min="1" value="5" oninput="updateFragmentHighlights(); saveCurrentChapterConfig();" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" />
                            </div>
                            <div style="grid-column: span 4; display: flex; gap: 1.8rem; align-items: center; background: rgba(255,255,255,0.03); padding: 0.75rem 1rem; border-radius: 8px; border: 1px solid var(--border); margin-top: 0.3rem;">
                                <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; font-weight: 700; cursor: pointer; color: #fff;">
                                    <input type="checkbox" id="toggle-subtitles" onchange="saveCurrentChapterConfig()" checked style="width: 17px; height: 17px; accent-color: var(--primary); cursor: pointer;" />
                                    💬 Burn Subtitles
                                </label>
                                <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; font-weight: 700; cursor: pointer; color: #fff;">
                                    <input type="checkbox" id="toggle-watermark" onchange="saveCurrentChapterConfig()" style="width: 17px; height: 17px; accent-color: var(--primary); cursor: pointer;" />
                                    💧 Watermark Logo
                                </label>
                                <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; font-weight: 700; cursor: pointer; color: #fff;">
                                    <input type="checkbox" id="toggle-waveform" onchange="saveCurrentChapterConfig()" style="width: 17px; height: 17px; accent-color: var(--primary); cursor: pointer;" />
                                    🎵 Audio Waveform
                                </label>
                            </div>
                        </div>
                        <div style="display: flex; gap: 0.6rem; flex-wrap: wrap; border-top: 1px solid var(--border); padding-top: 1rem; align-items: center;">
                            <button id="btn-run-all" class="run-btn" onclick="startPipelineForActiveProject('all')">🚀 Run Full Pipeline</button>
                            <button id="btn-run-subtitles" class="run-btn" style="background: linear-gradient(135deg, #f59e0b, #d97706);" onclick="startPipelineForActiveProject('subtitles_only')">📝 Subtitles Only</button>
                            <button id="btn-run-images" class="run-btn" style="background: linear-gradient(135deg, #ec4899, #db2777);" onclick="startPipelineForActiveProject('images_only')">🎨 Images Only</button>
                            <button id="btn-run-audio" class="run-btn" style="background: linear-gradient(135deg, #3b82f6, #2563eb);" onclick="startPipelineForActiveProject('audio_only')">🎙️ Audio Only</button>
                            <button id="btn-run-video" class="run-btn" style="background: linear-gradient(135deg, #8b5cf6, #7c3aed);" onclick="startPipelineForActiveProject('video_only')">🎬 Video Render Only</button>
                            <button id="btn-cancel-job" class="run-btn" style="display: none; background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 0 12px rgba(239, 68, 68, 0.4);" onclick="cancelPipelineForActiveProject()">⛔ Hủy tiến trình</button>
                        </div>
                    </div>

                    <!-- Fragments Workspace Content -->
                    <div id="workspace-content"></div>
                </div>

                <div id="no-chapter-selected" style="text-align: center; padding: 4rem 1rem; color: var(--text-muted);">
                    <div style="font-size: 3rem; margin-bottom: 1rem;">🎬</div>
                    <h3>No Chapter Selected</h3>
                    <p style="font-size: 0.9rem; margin-top: 0.5rem;">Select a chapter from the left directory to view fragments and run video rendering.</p>
                </div>
            </div>
        </div>
    </div>

    <!-- 3. VOICES DASHBOARD VIEW -->
    <div id="view-voices" style="display: none;">
        <div class="glass-card" style="padding: 1.5rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <div>
                    <h2 style="font-size: 1.4rem; font-weight: 800; color: #fff;">🎙️ OmniVoice Voice Management</h2>
                    <p style="color: var(--text-muted); font-size: 0.85rem;">Reference voice profiles & sample preview player</p>
                </div>
            </div>
            <div id="voices-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.2rem;"></div>
        </div>
    </div>

    <!-- NEW PROJECT MODAL (Google Flow) -->
    <div id="new-project-modal" class="modal-overlay" style="display: none;">
        <div class="glass-card" style="max-width: 540px; width: 90%; margin: auto; padding: 1.8rem; border-radius: 16px; position: relative;">
            <button onclick="closeNewProjectModal()" style="position: absolute; top: 1rem; right: 1rem; background: none; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer;">&times;</button>
            <h3 style="font-size: 1.3rem; font-weight: 800; margin-bottom: 0.4rem; color: #fff;">✨ Create New Project</h3>
            <p style="color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1.2rem;">Mandatory project initialization with Category routing.</p>
            
            <form onsubmit="handleCreateProjectSubmit(event)">
                <label style="display: block; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.4rem;">1. Select Project Type *</label>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.6rem; margin-bottom: 1.2rem;">
                    <div class="cat-choice active" id="cat-choice-story" onclick="selectNewCategory('story')">
                        <div style="font-size: 1.3rem;">📖</div>
                        <div style="font-weight: 700; font-size: 0.85rem;">Story</div>
                    </div>
                    <div class="cat-choice" id="cat-choice-reels" onclick="selectNewCategory('reels')">
                        <div style="font-size: 1.3rem;">📱</div>
                        <div style="font-weight: 700; font-size: 0.85rem;">Reels</div>
                    </div>
                    <div class="cat-choice" id="cat-choice-long" onclick="selectNewCategory('long')">
                        <div style="font-size: 1.3rem;">🎬</div>
                        <div style="font-weight: 700; font-size: 0.85rem;">Long</div>
                    </div>
                    <div class="cat-choice" id="cat-choice-sketch" onclick="selectNewCategory('sketch')">
                        <div style="font-size: 1.3rem;">✏️</div>
                        <div style="font-weight: 700; font-size: 0.85rem;">Sketch</div>
                    </div>
                    <div class="cat-choice" id="cat-choice-music" onclick="selectNewCategory('music')">
                        <div style="font-size: 1.3rem;">🎵</div>
                        <div style="font-weight: 700; font-size: 0.85rem;">Music</div>
                    </div>
                </div>

                <div id="story-select-group">
                    <label style="display: block; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.4rem;">2. Select Lore-Keeper Story *</label>
                    <select id="lore-keeper-select" style="width: 100%; padding: 0.6rem; border-radius: 8px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff; margin-bottom: 1.2rem;"></select>
                </div>

                <div id="custom-name-group" style="display: none;">
                    <label style="display: block; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.4rem;">2. Project Name *</label>
                    <input type="text" id="custom-proj-input" placeholder="e.g. 5-giai-ma-khoa-hoc-thay-doi-nhan-thuc" style="width: 100%; padding: 0.6rem; border-radius: 8px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff; margin-bottom: 1rem;">
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1.2rem;">
                    <div>
                        <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">Language</label>
                        <select id="new-lang-select" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                            <option value="vi">🇻🇳 Tiếng Việt</option>
                            <option value="en">🇺🇸 English</option>
                        </select>
                    </div>
                    <div>
                        <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">Aspect Ratio</label>
                        <select id="new-aspect-select" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                            <option value="9:16">📱 9:16</option>
                            <option value="16:9">🎬 16:9</option>
                        </select>
                    </div>
                </div>

                <button type="submit" class="run-btn" style="width: 100%; padding: 0.75rem; font-size: 0.95rem;">🚀 Create Project</button>
            </form>
        </div>
    </div>

    <!-- ADD ITEM MODAL -->
    <div id="add-item-modal" class="modal-overlay" style="display: none;">
        <div class="glass-card" style="max-width: 620px; width: 92%; margin: auto; padding: 1.8rem; border-radius: 16px; position: relative; max-height: 90vh; overflow-y: auto;">
            <button onclick="closeAddItemModal()" style="position: absolute; top: 1rem; right: 1rem; background: none; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer;">&times;</button>
            <h3 style="font-size: 1.25rem; font-weight: 800; margin-bottom: 0.3rem; color: #fff;">✨ Add New Item / Video Script</h3>
            <p style="color: var(--text-muted); font-size: 0.82rem; margin-bottom: 1rem;">Nhập thông số kịch bản mới hoặc Nạp trực tiếp từ file JSON / TXT mẫu.</p>

            <div style="background: rgba(255,255,255,0.03); border: 1px dashed var(--primary); padding: 0.8rem 1rem; border-radius: 8px; margin-bottom: 1.2rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
                <div>
                    <div style="font-size: 0.85rem; font-weight: bold; color: #fff;">📂 Import từ file JSON / TXT mẫu</div>
                    <div style="font-size: 0.75rem; color: var(--text-muted);">Tự động đọc Title, Short Title, Channel, Aspect Ratio & Script Content</div>
                </div>
                <label style="background: linear-gradient(135deg, var(--primary), #7c3aed); color: #fff; padding: 0.45rem 1rem; border-radius: 6px; font-size: 0.8rem; font-weight: bold; cursor: pointer; flex-shrink: 0; display: inline-flex; align-items: center; gap: 0.4rem;">
                    📁 Chọn file
                    <input type="file" id="item-json-file-input" accept=".json,.txt" onchange="handleItemJsonFileUpload(event)" style="display: none;" />
                </label>
            </div>

            <form onsubmit="handleAddItemSubmit(event)">
                <div style="display: grid; grid-template-columns: 1fr 1.5fr; gap: 0.8rem; margin-bottom: 0.8rem;">
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Số tập (Episode #)</label>
                        <input type="number" id="new-item-episode-input" min="1" value="1" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" oninput="updateEpisodeLabelDefault()" />
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Nhãn tập (Episode Label)</label>
                        <input type="text" id="new-item-episode-label-input" placeholder="e.g. Tập 01" value="Tập 01" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" />
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1.8fr 1.2fr; gap: 0.8rem; margin-bottom: 0.8rem;">
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Tiêu đề chính (Title) *</label>
                        <input type="text" id="new-item-title-input" required placeholder="e.g. 5 Giải Mã Khoa Học Kỳ Thú..." style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" oninput="autoPopulateItemSlug()" />
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Tiêu đề ngắn (Short Title)</label>
                        <input type="text" id="new-item-short-title-input" placeholder="e.g. 5 Giải Mã Khoa Học" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" oninput="autoPopulateItemSlug()" />
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 0.8rem;">
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Mã Slug / Item ID *</label>
                        <input type="text" id="new-item-slug-input" required placeholder="e.g. 5-giai-ma-khoa-hoc" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" />
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Kênh / Channel</label>
                        <input type="text" id="new-item-channel-input" placeholder="e.g. @playnet.zone-vi" value="@playnet.zone-vi" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;" />
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 0.8rem;">
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Tỷ lệ khung hình (Aspect Ratio)</label>
                        <select id="new-item-aspect-select" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                            <option value="16:9">🎬 Horizontal 16:9 (YouTube Long)</option>
                            <option value="9:16">📱 Vertical 9:16 (Reels / Shorts)</option>
                            <option value="1:1">🔲 Square 1:1</option>
                        </select>
                    </div>
                    <div>
                        <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Ngôn ngữ (Language)</label>
                        <select id="new-item-lang-select" style="width: 100%; padding: 0.55rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                            <option value="vi">🇻🇳 Tiếng Việt</option>
                            <option value="en">🇺🇸 English</option>
                        </select>
                    </div>
                </div>

                <div style="margin-bottom: 1.2rem;">
                    <label style="display: block; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.3rem;">Nội dung kịch bản (Script Content)</label>
                    <textarea id="new-item-content-input" rows="6" placeholder="Dán nội dung kịch bản văn bản vào đây..." style="width: 100%; padding: 0.6rem; border-radius: 8px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff; font-family: inherit; font-size: 0.82rem; resize: vertical;"></textarea>
                </div>

                <button type="submit" class="run-btn" style="width: 100%; padding: 0.75rem; font-size: 0.95rem;">🚀 Tạo Item Mới</button>
            </form>
        </div>
    </div>

    <script>
        let currentCategoryFilter = 'all';
        let selectedNewCategory = 'story';
        let allProjectsList = [];
        let activeStoryId = null;
        let activeChapterId = null;
        let activeWorkspaceId = null;
        let loadedChapterConfigKey = null;

        function getWorkspaceParam() {
            let urlParams = new URLSearchParams(window.location.search);
            let ws = urlParams.get("ws") || urlParams.get("workspace_id") || activeWorkspaceId;
            return ws ? `?ws=${encodeURIComponent(ws)}` : "";
        }

        function toggleAgentDropdown(e) {
            if (e) e.stopPropagation();
            let menu = document.getElementById("agent-dropdown");
            if (menu) {
                menu.style.display = menu.style.display === "none" ? "block" : "none";
            }
        }

        document.addEventListener("click", () => {
            let menu = document.getElementById("agent-dropdown");
            if (menu) menu.style.display = "none";
        });

        function showPage(pageId) {
            let mainHeader = document.getElementById('main-app-header');
            if (mainHeader) mainHeader.style.display = 'flex';
            let menuNav = document.getElementById('header-menu-nav');
            if (menuNav) menuNav.style.display = 'flex';
            document.getElementById('nav-home').classList.remove('active');
            document.getElementById('nav-voices').classList.remove('active');

            if (pageId === 'home') {
                document.getElementById('nav-home').classList.add('active');
                backToProjectsHome();
            } else if (pageId === 'voices') {
                document.getElementById('nav-voices').classList.add('active');
                document.getElementById('view-projects-home').style.display = 'none';
                document.getElementById('view-project-workspace').style.display = 'none';
                document.getElementById('view-voices').style.display = 'block';
                loadVoicesDashboard();
            }
        }

        function filterCategory(cat, el) {
            currentCategoryFilter = cat;
            document.querySelectorAll('.cat-pill').forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');
            renderProjectsCards();
        }

        async function updateAgentStatus() {
            try {
                let res = await fetch("/v1/agent/status");
                let data = await res.json();
                let badge = document.getElementById("agent-badge");
                let status = document.getElementById("agent-status");
                
                let wsName = document.getElementById("dropdown-workspace-name");
                let agentVer = document.getElementById("dropdown-agent-version");
                let hwInfo = document.getElementById("dropdown-hardware-info");
                let omniStatus = document.getElementById("dropdown-omnivoice-status");
                let dropTitle = document.getElementById("dropdown-status-title");

                if (data.workspace_id) activeWorkspaceId = data.workspace_id;
                if (data.connected) {
                    badge.className = "agent-badge connected";
                    status.innerText = "Agent Online";
                    if (dropTitle) dropTitle.innerHTML = `<span style="color: var(--success);">🟢 Agent Online</span>`;
                    
                    let agentMeta = data.agents ? data.agents[data.workspace_id] : null;
                    if (wsName) wsName.innerText = data.workspace_id || "huutq_d23b05";
                    if (agentVer) agentVer.innerText = "v" + (data.agent_version || "0.4.4");
                    
                    if (agentMeta) {
                        if (hwInfo) hwInfo.innerText = agentMeta.mps_available ? "MPS (Apple Silicon)" : agentMeta.cuda_available ? "CUDA GPU" : "CPU";
                        if (omniStatus) omniStatus.innerText = agentMeta.omnivoice_installed ? "Active (v0.1.0)" : "Not Installed";
                    }
                } else {
                    badge.className = "agent-badge";
                    status.innerText = "Agent Offline";
                    if (dropTitle) dropTitle.innerHTML = `<span style="color: var(--text-muted);">🔴 Agent Offline</span>`;
                    if (wsName) wsName.innerText = "--";
                    if (agentVer) agentVer.innerText = "v" + (data.agent_version || "0.4.4");
                }
                if (document.getElementById("view-project-workspace").style.display !== "none") {
                    updateCurrentChapterStatusBanner();
                }
            } catch(e) {}
        }

        async function loadProjects() {
            try {
                let res = await fetch("/v1/projects");
                allProjectsList = await res.json();
                renderProjectsCards();
            } catch(e) {
                console.error("loadProjects error:", e);
            }
        }

        function renderProjectsCards() {
            let container = document.getElementById("projects-cards-container");
            if (!container) return;
            container.innerHTML = "";

            // New Project Button Card
            let newCard = document.createElement("div");
            newCard.className = "project-card create-project-card";
            newCard.onclick = openNewProjectModal;
            newCard.innerHTML = `
                <div style="font-size: 2.5rem; margin-bottom: 0.5rem; color: var(--primary);">✨</div>
                <h3 style="color: #fff; font-size: 1.1rem; margin-bottom: 0.3rem;">Create New Project</h3>
                <p style="color: var(--text-muted); font-size: 0.8rem;">Story, Reels, Long, Sketch, or Music</p>
            `;
            container.appendChild(newCard);

            if (!Array.isArray(allProjectsList)) return;

            let filtered = allProjectsList.filter(p => {
                if (currentCategoryFilter === 'all') return true;
                let pType = p.project_type || p.story_id;
                if (currentCategoryFilter === 'story' && (pType === 'story' || p.story_id === 'kien-thuc' || p.story_id === 'monochromatic_pencil_sketch')) return true;
                if (currentCategoryFilter === 'reels' && (pType === 'reels' || p.story_id === 'dao-ly')) return true;
                return pType === currentCategoryFilter;
            });

            filtered.forEach(p => {
                let card = document.createElement("div");
                card.className = "project-card";
                card.onclick = () => openProjectWorkspace(p.story_id);

                let pType = p.project_type || p.story_id;
                let badgeClass = "story";
                let badgeIcon = "📖 Story";
                if (pType === "reels" || p.story_id === "dao-ly") { badgeClass = "reels"; badgeIcon = "📱 Reels"; }
                else if (pType === "long") { badgeClass = "long"; badgeIcon = "🎬 Long"; }
                else if (pType === "sketch") { badgeClass = "sketch"; badgeIcon = "✏️ Sketch"; }
                else if (pType === "music") { badgeClass = "music"; badgeIcon = "🎵 Music"; }

                let completedCount = p.chapters ? p.chapters.filter(c => c.status === "completed").length : 0;
                let totalCount = p.chapters ? p.chapters.length : 0;

                card.innerHTML = `
                    <div>
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem;">
                            <span class="proj-category-badge ${badgeClass}">${badgeIcon}</span>
                            <span style="font-size: 0.75rem; color: var(--text-muted);">${completedCount}/${totalCount} Rendered</span>
                        </div>
                        <h3 style="font-size: 1.2rem; font-weight: 800; color: #fff; margin-bottom: 0.5rem;">${p.title || p.story_id}</h3>
                        <p style="font-size: 0.8rem; color: var(--text-muted);">ID: ${p.story_id}</p>
                    </div>
                    <div style="margin-top: 1.2rem; border-top: 1px solid var(--border); padding-top: 0.8rem; display: flex; justify-content: space-between; align-items: center;">
                        <div style="display: flex; align-items: center; gap: 0.6rem;">
                            <span style="font-size: 0.8rem; color: var(--primary); font-weight: 600;">${totalCount} items</span>
                            <button onclick="deleteProject(event, '${p.story_id}', '${p.title || p.story_id}')" style="background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: #f87171; padding: 0.3rem 0.6rem; border-radius: 6px; font-weight: bold; font-size: 0.75rem; cursor: pointer; transition: all 0.2s;" title="Xóa Project">🗑️ Xóa</button>
                        </div>
                        <button style="background: linear-gradient(135deg, var(--primary), #7c3aed); border: none; color: #fff; padding: 0.4rem 0.9rem; border-radius: 6px; font-weight: bold; font-size: 0.8rem; cursor: pointer;">Open Project →</button>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function openProjectWorkspace(storyId) {
            activeStoryId = storyId;
            let proj = allProjectsList.find(p => p.story_id === storyId);
            if (!proj) return;

            let mainHeader = document.getElementById('main-app-header');
            if (mainHeader) mainHeader.style.display = 'none';

            let menuNav = document.getElementById('header-menu-nav');
            if (menuNav) menuNav.style.display = 'none';

            document.getElementById("view-projects-home").style.display = "none";
            document.getElementById("view-project-workspace").style.display = "block";
            document.getElementById("view-voices").style.display = "none";

            document.getElementById("active-project-title").innerText = proj.title || storyId;
            let badge = document.getElementById("active-project-badge");
            let pType = proj.project_type || storyId;
            badge.className = "proj-category-badge " + (pType === "reels" ? "reels" : pType === "long" ? "long" : pType === "sketch" ? "sketch" : pType === "music" ? "music" : "story");
            badge.innerText = pType.toUpperCase();

            document.getElementById("active-project-stats").innerText = `${proj.chapters ? proj.chapters.length : 0} items`;

            let chList = document.getElementById("workspace-chapters-list");
            chList.innerHTML = "";

            if (!proj.chapters || proj.chapters.length === 0) {
                activeChapterId = null;
                chList.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">No chapters found in this project.</p>`;
                document.getElementById("no-chapter-selected").style.display = "block";
                document.getElementById("no-chapter-selected").innerHTML = `
                    <div style="font-size: 2.5rem; margin-bottom: 0.8rem;">📭</div>
                    <h3 style="font-size: 1.1rem; color: #fff; margin-bottom: 0.5rem;">Chưa có Item nào trong Project này</h3>
                    <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 1.2rem;">Hãy bấm nút <strong>"+ Thêm Item mới"</strong> ở góc trên danh sách bên trái để bắt đầu.</p>
                `;
                document.getElementById("details-panel").style.display = "none";
                let wsContent = document.getElementById("workspace-content");
                if (wsContent) wsContent.innerHTML = "";
                let btnFinal = document.getElementById("btn-preview-final");
                if (btnFinal) btnFinal.style.display = "none";
                return;
            }

            proj.chapters.forEach((c, idx) => {
                let item = document.createElement("div");
                if (idx === 0 && (!activeChapterId || !proj.chapters.some(x => x.id === activeChapterId))) {
                    activeChapterId = c.id;
                }
                let isActive = (c.id === activeChapterId);
                item.className = "chapter-item" + (isActive ? " active" : "");
                item.dataset.chapterId = c.id;
                item.onclick = () => selectChapterInWorkspace(proj.story_id, c.id, c.title);
                
                let statusColor = c.status === "completed" ? "#10b981" : c.status === "processing" ? "#f59e0b" : "var(--text-muted)";
                let statusIcon = c.status === "completed" ? "● completed" : c.status === "processing" ? "⚡ processing" : "● idle";
                item.innerHTML = `
                    <div style="flex: 1; min-width: 0; margin-right: 0.4rem;">
                        <div style="font-weight: 600; font-size: 0.85rem; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${c.title}</div>
                        <div style="font-size: 0.7rem; color: var(--text-muted);">ID: ${c.id}</div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.3rem;">
                        <span class="ch-status-badge" style="font-size: 0.7rem; color: ${statusColor}; font-weight: bold;">${statusIcon}</span>
                        <button onclick="openFolder(event, '${proj.story_id}', '${c.id}')" style="background: rgba(255,255,255,0.08); border: 1px solid var(--border); color: #fff; width: 26px; height: 26px; border-radius: 6px; font-size: 0.75rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s;" title="Mở thư mục Chapter này">📁</button>
                        <button onclick="deleteChapter(event, '${proj.story_id}', '${c.id}', '${c.title}')" style="background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: #f87171; width: 26px; height: 26px; border-radius: 6px; font-size: 0.75rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s;" title="Xóa Item này">🗑️</button>
                    </div>
                `;
                chList.appendChild(item);
            });

            if (proj.chapters.length > 0) {
                let targetCh = proj.chapters.find(c => c.id === activeChapterId) || proj.chapters[0];
                selectChapterInWorkspace(storyId, targetCh.id, targetCh.title);
            }
        }

        function backToProjectsHome() {
            activeStoryId = null;
            activeChapterId = null;
            let mainHeader = document.getElementById('main-app-header');
            if (mainHeader) mainHeader.style.display = 'flex';
            let menuNav = document.getElementById('header-menu-nav');
            if (menuNav) menuNav.style.display = 'flex';
            document.getElementById("view-projects-home").style.display = "block";
            document.getElementById("view-project-workspace").style.display = "none";
            document.getElementById("view-voices").style.display = "none";
            loadProjects();
        }

        let isConfigLoading = false;

        async function selectChapterInWorkspace(storyId, chapterId, title) {
            isConfigLoading = true;
            activeStoryId = storyId;
            activeChapterId = chapterId;
            loadedChapterConfigKey = null;

            document.querySelectorAll("#workspace-chapters-list .chapter-item").forEach(el => {
                if (el.dataset.chapterId === chapterId) {
                    el.classList.add("active");
                } else {
                    el.classList.remove("active");
                }
            });

            document.getElementById("no-chapter-selected").style.display = "none";
            document.getElementById("details-panel").style.display = "block";
            document.getElementById("current-chapter-title").innerText = title || chapterId;
            document.getElementById("current-chapter-subtitle").innerText = `Project: ${storyId} • ID: ${chapterId}`;
            
            updateCurrentChapterStatusBanner();
            loadVoicesSelect();
            loadFragments(storyId, chapterId);
        }

        async function saveCurrentChapterConfig() {
            if (!activeStoryId || !activeChapterId || isConfigLoading) return;
            let artStyle = document.getElementById("art-style-select") ? document.getElementById("art-style-select").value : null;
            let aspectRatio = document.getElementById("aspect-ratio-select") ? document.getElementById("aspect-ratio-select").value : null;
            let subtitlePreset = document.getElementById("subtitle-preset-select") ? document.getElementById("subtitle-preset-select").value : null;
            let effectType = document.getElementById("effect-type-select") ? document.getElementById("effect-type-select").value : null;
            let voiceId = document.getElementById("voice-select") ? document.getElementById("voice-select").value : null;
            let ttsProvider = document.getElementById("tts-provider-select") ? document.getElementById("tts-provider-select").value : null;
            let useWatermark = document.getElementById("toggle-watermark") ? document.getElementById("toggle-watermark").checked : undefined;
            let useSubtitles = document.getElementById("toggle-subtitles") ? document.getElementById("toggle-subtitles").checked : undefined;
            let useWaveform = document.getElementById("toggle-waveform") ? document.getElementById("toggle-waveform").checked : undefined;

            let fragStartEl = document.getElementById("frag-start-input");
            let fragEndEl = document.getElementById("frag-end-input");
            let startFrag = fragStartEl ? (parseInt(fragStartEl.value) || 1) : 1;
            let endFrag = fragEndEl ? (parseInt(fragEndEl.value) || 1) : 1;

            let payload = {
                art_style: artStyle,
                aspect_ratio: aspectRatio,
                subtitle_preset: subtitlePreset,
                effect_type: effectType,
                voice_id: voiceId,
                tts_provider: ttsProvider,
                use_watermark: useWatermark,
                use_subtitles: useSubtitles,
                use_waveform: useWaveform,
                start_fragment: startFrag,
                end_fragment: endFrag
            };

            try {
                await fetch(`/v1/projects/${encodeURIComponent(activeStoryId)}/${encodeURIComponent(activeChapterId)}/config`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
            } catch(e) {
                console.error("Failed to save chapter config:", e);
            }
        }

        async function updateCurrentChapterStatusBanner() {
            if (!activeStoryId || !activeChapterId) return;
            let banner = document.getElementById("status-banner");
            if (!banner) return;
            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(activeStoryId)}/${encodeURIComponent(activeChapterId)}/status`);
                let stData = await res.json();
                let st = stData.status || "idle";
                let isBusy = (st === "processing" || st === "running" || st === "queued" || st === "starting");
                
                let hasImages = !!stData.has_images;
                let hasAudio = !!stData.has_audio;
                let hasVideo = !!stData.has_video;

                let currentKey = activeStoryId + "/" + activeChapterId;
                if (loadedChapterConfigKey !== currentKey) {
                    window.activeItemConfig = stData;
                    if (stData.art_style && document.getElementById("art-style-select")) {
                        document.getElementById("art-style-select").value = stData.art_style;
                    }
                    if (stData.subtitle_preset && document.getElementById("subtitle-preset-select")) {
                        document.getElementById("subtitle-preset-select").value = stData.subtitle_preset;
                    }
                    if (stData.aspect_ratio && document.getElementById("aspect-ratio-select")) {
                        document.getElementById("aspect-ratio-select").value = stData.aspect_ratio;
                    }
                    if (stData.use_watermark !== undefined && document.getElementById("toggle-watermark")) {
                        document.getElementById("toggle-watermark").checked = !!stData.use_watermark;
                    }
                    if (stData.use_subtitles !== undefined && document.getElementById("toggle-subtitles")) {
                        document.getElementById("toggle-subtitles").checked = !!stData.use_subtitles;
                    }
                    if (stData.use_waveform !== undefined && document.getElementById("toggle-waveform")) {
                        document.getElementById("toggle-waveform").checked = !!stData.use_waveform;
                    }

                    let sEl = document.getElementById("frag-start-input");
                    let eEl = document.getElementById("frag-end-input");
                    let fragItems = document.querySelectorAll("#fragments-list-container .frag-item");
                    let totalFrags = fragItems.length;
                    if (sEl && eEl && totalFrags > 0) {
                        let sVal = stData.start_fragment !== undefined ? parseInt(stData.start_fragment) : 1;
                        let eVal = stData.end_fragment !== undefined ? parseInt(stData.end_fragment) : totalFrags;
                        sEl.setAttribute("max", totalFrags);
                        eEl.setAttribute("max", totalFrags);
                        sEl.value = Math.max(1, Math.min(sVal, totalFrags));
                        eEl.value = Math.max(sEl.value, Math.min(eVal, totalFrags));
                        updateFragmentHighlights();
                    }

                    loadedChapterConfigKey = currentKey;
                    isConfigLoading = false;
                }

                let btnAll = document.getElementById("btn-run-all");
                let btnImages = document.getElementById("btn-run-images");
                let btnAudio = document.getElementById("btn-run-audio");
                let btnSub = document.getElementById("btn-run-subtitles");
                let btnVideo = document.getElementById("btn-run-video");
                let btnCancel = document.getElementById("btn-cancel-job");

                if (isBusy) {
                    [btnAll, btnImages, btnAudio, btnSub, btnVideo].forEach(b => {
                        if (b) {
                            b.disabled = true;
                            b.style.opacity = "0.45";
                            b.style.cursor = "not-allowed";
                            b.title = "⚠️ Pipeline job is currently running...";
                        }
                    });
                    if (btnCancel) {
                        btnCancel.style.display = "inline-flex";
                        btnCancel.innerText = "⛔ Hủy tiến trình";
                    }
                } else {
                    if (btnCancel) {
                        btnCancel.style.display = "none";
                    }
                    if (btnAll) { btnAll.disabled = false; btnAll.style.opacity = "1"; btnAll.style.cursor = "pointer"; btnAll.title = ""; }
                    if (btnImages) { btnImages.disabled = false; btnImages.style.opacity = "1"; btnImages.style.cursor = "pointer"; btnImages.title = ""; }
                    if (btnAudio) { btnAudio.disabled = false; btnAudio.style.opacity = "1"; btnAudio.style.cursor = "pointer"; btnAudio.title = ""; }

                    if (btnSub) {
                        if (!hasAudio) {
                            btnSub.disabled = true;
                            btnSub.style.opacity = "0.45";
                            btnSub.style.cursor = "not-allowed";
                            btnSub.title = "⚠️ Requires generated audio clips first";
                        } else {
                            btnSub.disabled = false;
                            btnSub.style.opacity = "1";
                            btnSub.style.cursor = "pointer";
                            btnSub.title = "Run Whisper subtitle alignment on audio clips";
                        }
                    }

                    if (btnVideo) {
                        if (!hasImages && !hasAudio) {
                            btnVideo.disabled = true;
                            btnVideo.style.opacity = "0.45";
                            btnVideo.style.cursor = "not-allowed";
                            btnVideo.title = "⚠️ Requires both images and audio clips generated first";
                        } else if (!hasImages) {
                            btnVideo.disabled = true;
                            btnVideo.style.opacity = "0.45";
                            btnVideo.style.cursor = "not-allowed";
                            btnVideo.title = "⚠️ Requires generated story images first";
                        } else if (!hasAudio) {
                            btnVideo.disabled = true;
                            btnVideo.style.opacity = "0.45";
                            btnVideo.style.cursor = "not-allowed";
                            btnVideo.title = "⚠️ Requires generated audio clips first";
                        } else {
                            btnVideo.disabled = false;
                            btnVideo.style.opacity = "1";
                            btnVideo.style.cursor = "pointer";
                            btnVideo.title = "Render final video with images, audio & subtitles";
                        }
                    }

                    let btnPreviewFinal = document.getElementById("btn-preview-final");
                    if (btnPreviewFinal) {
                        if (hasVideo || st === "completed") {
                            btnPreviewFinal.style.display = "inline-flex";
                        } else {
                            btnPreviewFinal.style.display = "none";
                        }
                    }
                }

                let itemEl = document.querySelector(`.chapter-item[data-chapter-id="${CSS.escape(activeChapterId)}"]`);
                if (itemEl) {
                    let badgeEl = itemEl.querySelector(".ch-status-badge");
                    if (badgeEl) {
                        let color = (st === "completed") ? "#10b981" : (st === "processing" || st === "running" || st === "queued") ? "#f59e0b" : "var(--text-muted)";
                        let icon = (st === "completed") ? "● completed" : (st === "processing" || st === "running") ? "⚡ processing" : (st === "queued") ? "⏳ queued" : "● idle";
                        badgeEl.style.color = color;
                        badgeEl.innerText = icon;
                    }
                }

                if (st === "processing" || st === "running") {
                    let step = stData.current_step || "Processing...";
                    let pct = (stData.progress_percent !== undefined && stData.progress_percent !== null) ? ` (${stData.progress_percent}%)` : "";
                    banner.innerHTML = `<span style="color: #f59e0b; font-weight: bold;">⚡ ${step}${pct}</span>`;
                    banner.style.borderColor = "rgba(245, 158, 11, 0.4)";
                    banner.style.background = "rgba(245, 158, 11, 0.15)";
                } else if (st === "queued") {
                    banner.innerHTML = `<span style="color: #a855f7; font-weight: bold;">⏳ Queued</span>`;
                    banner.style.borderColor = "rgba(168, 85, 247, 0.4)";
                    banner.style.background = "rgba(168, 85, 247, 0.15)";
                } else if (st === "completed") {
                    banner.innerHTML = `<span style="color: #10b981; font-weight: bold;">✅ Completed</span>`;
                    banner.style.borderColor = "rgba(16, 185, 129, 0.4)";
                    banner.style.background = "rgba(16, 185, 129, 0.15)";
                } else {
                    banner.innerHTML = `<span style="color: var(--text-muted);">Ready</span>`;
                    banner.style.borderColor = "var(--border)";
                    banner.style.background = "rgba(255, 255, 255, 0.05)";
                }

                let currentFragIdx = stData.current_fragment || 0;
                let fragStep = stData.fragment_status ? (stData.fragment_status.step || '') : '';
                let fragStateKey = `${currentFragIdx}_${fragStep}_${st}`;
                if (window.lastFragStateKey !== fragStateKey) {
                    window.lastFragStateKey = fragStateKey;
                    if (typeof loadFragments === "function" && activeStoryId && activeChapterId) {
                        loadFragments(activeStoryId, activeChapterId);
                    }
                }
            } catch(e) {}
        }

        async function loadVoicesSelect() {
            try {
                let res = await fetch("/v1/voices");
                let voices = await res.json();
                let select = document.getElementById("voice-select");
                select.innerHTML = "";
                voices.forEach(v => {
                    let opt = document.createElement("option");
                    opt.value = v.id;
                    opt.innerText = (v.is_protected ? "🔒 " : "") + (v.name || v.id);
                    select.appendChild(opt);
                });
            } catch(e) {}
        }

        async function loadFragments(storyId, chapterId) {
            let container = document.getElementById("workspace-content");
            container.innerHTML = `<p style="color: var(--text-muted);">Loading fragments...</p>`;
            try {
                let wsParam = getWorkspaceParam();
                let res = await fetch(`/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/fragments${wsParam}`);
                let frags = await res.json();
                if (!Array.isArray(frags) || frags.length === 0) {
                    container.innerHTML = `<p style="color: var(--text-muted);">No fragments found for this chapter.</p>`;
                    return;
                }
                
                let html = `
                    <div style="display: flex; justify-content: space-between; align-items: center; cursor: pointer; user-select: none; margin-bottom: 0.8rem; background: rgba(0,0,0,0.25); padding: 0.8rem 1rem; border-radius: 8px; border: 1px solid var(--border);" onclick="toggleFragmentsAccordion()">
                        <div>
                            <h4 style="font-size: 0.95rem; margin: 0 0 0.25rem 0; color: var(--primary); font-weight: 700; display: flex; align-items: center; gap: 0.4rem;">
                                📝 Fragments & Audio Clips (${frags.length})
                            </h4>
                            <div id="fragments-subtitle-info" style="font-size: 0.78rem; color: #a855f7; font-weight: 600;">
                                Selected Range: Fragment #1 → #5 (5 clips included in pipeline)
                            </div>
                        </div>
                        <span id="fragments-arrow" style="font-size: 0.85rem; color: var(--text-muted); transition: transform 0.2s ease;">▼</span>
                    </div>
                    <div id="fragments-list-container" style="display: flex; flex-direction: column; gap: 0.6rem;">
                `;
                frags.forEach(f => {
                    let itemNum = f.index + 1;
                    
                    let isMismatch = !!f.aspect_mismatch;
                    let warningBadge = isMismatch 
                        ? `<span style="position: absolute; top: -6px; right: -6px; background: #eab308; color: #000; border-radius: 50%; width: 18px; height: 18px; font-size: 11px; font-weight: bold; display: flex; align-items: center; justify-content: center; box-shadow: 0 2px 6px rgba(0,0,0,0.6); z-index: 2;" title="⚠️ Cảnh báo: Kích thước ảnh (${f.image_width || ''}x${f.image_height || ''}) không khớp với tỉ lệ ${f.configured_aspect_ratio || ''} đã cấu hình!">⚠️</span>` 
                        : ``;

                    let borderStyle = isMismatch 
                        ? `border: 2px solid #eab308; box-shadow: 0 0 8px rgba(234, 179, 8, 0.5);` 
                        : `border: 1.5px solid var(--primary);`;

                    let imgTitle = isMismatch 
                        ? `⚠️ Cảnh báo: Ảnh (${f.image_width || ''}x${f.image_height || ''}) không đúng tỉ lệ ${f.configured_aspect_ratio || ''} đã chọn!` 
                        : `Click to view image (${f.image_width || ''}x${f.image_height || ''})`;

                    let imgHtml = f.image_url 
                        ? `<div style="position: relative; display: inline-block;">
                             <img src="${f.image_url}" onclick="openMediaPreviewModal('${f.image_url}', 'image', 'Fragment #${itemNum} Image', ${isMismatch}, '${f.image_width || ''}', '${f.image_height || ''}', '${f.configured_aspect_ratio || ''}')" style="width: 36px; height: 36px; object-fit: cover; border-radius: 6px; cursor: pointer; ${borderStyle} transition: transform 0.2s;" onmouseover="this.style.transform='scale(1.1)'" onmouseout="this.style.transform='scale(1)'" title="${imgTitle}" />
                             ${warningBadge}
                           </div>` 
                        : `<span style="font-size: 0.7rem; color: rgba(255,255,255,0.25); padding: 0.25rem 0.4rem; border-radius: 4px; border: 1px dashed rgba(255,255,255,0.15);" title="No image generated">🎨 No Image</span>`;

                    let audHtml = f.audio_url 
                        ? `<button onclick="playFragmentAudio('${f.audio_url}', this)" style="background: rgba(59, 130, 246, 0.15); border: 1px solid #3b82f6; color: #60a5fa; padding: 0.3rem 0.6rem; border-radius: 6px; font-size: 0.72rem; font-weight: bold; cursor: pointer; display: inline-flex; align-items: center; gap: 0.3rem;" title="Play TTS Audio">▶️ Audio</button>` 
                        : `<span style="font-size: 0.7rem; color: rgba(255,255,255,0.25); padding: 0.25rem 0.4rem; border-radius: 4px; border: 1px dashed rgba(255,255,255,0.15);" title="No audio generated">🎙️ No Audio</span>`;

                    let vidHtml = f.video_url 
                        ? `<button onclick="openMediaPreviewModal('${f.video_url}', 'video', 'Fragment #${itemNum} Video Clip')" style="background: rgba(139, 92, 246, 0.15); border: 1px solid #8b5cf6; color: #c084fc; padding: 0.3rem 0.6rem; border-radius: 6px; font-size: 0.72rem; font-weight: bold; cursor: pointer; display: inline-flex; align-items: center; gap: 0.3rem;" title="Preview Video Clip">🎬 Video</button>` 
                        : ``;

                    html += `
                        <div class="frag-item" data-index="${itemNum}" style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); padding: 0.8rem; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; gap: 1rem; transition: all 0.2s ease;">
                            <div style="flex: 1;">
                                <span class="frag-badge" style="font-size: 0.75rem; color: var(--primary); font-weight: bold; margin-right: 0.5rem;">#${itemNum}</span>
                                <span style="font-size: 0.85rem; color: #eee;">${f.text}</span>
                            </div>
                            <div style="display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0;">
                                ${imgHtml}
                                ${audHtml}
                                ${vidHtml}
                            </div>
                        </div>
                    `;
                });
                html += `</div>`;
                container.innerHTML = html;
                updateFragmentHighlights();
                setTimeout(convertFragmentThumbnailsToLocalBlobs, 50);
            } catch(e) {
                container.innerHTML = `<p style="color: var(--danger);">Error loading fragments: ${e.message}</p>`;
            }
        }

        let localWsClient = null;
        let localWsReady = false;
        let localWsPending = {};
        let lastHeaderMap = {};

        function initLocalWsClient() {
            try {
                let ws = new WebSocket("ws://127.0.0.1:8767");
                ws.binaryType = "arraybuffer";
                ws.onopen = () => { localWsReady = true; };
                let pendingReqId = null;
                ws.onmessage = (e) => {
                    if (typeof e.data === "string") {
                        try {
                            let data = JSON.parse(e.data);
                            let reqId = data.request_id;
                            if (data.exists === false) {
                                let cb = localWsPending[reqId];
                                if (cb) {
                                    delete localWsPending[reqId];
                                    cb(null);
                                }
                            } else if (data.exists === true) {
                                pendingReqId = reqId;
                                lastHeaderMap[reqId] = data;
                            }
                        } catch(err) {}
                    } else if (e.data instanceof ArrayBuffer) {
                        if (pendingReqId && localWsPending[pendingReqId]) {
                            let header = lastHeaderMap[pendingReqId] || {};
                            let reqId = pendingReqId;
                            pendingReqId = null;
                            delete lastHeaderMap[reqId];
                            let cb = localWsPending[reqId];
                            delete localWsPending[reqId];
                            try {
                                let blob = new Blob([e.data], { type: header.content_type || "application/octet-stream" });
                                let blobUrl = URL.createObjectURL(blob);
                                cb(blobUrl);
                            } catch(err) {
                                cb(null);
                            }
                        }
                    }
                };
                ws.onerror = ws.onclose = () => {
                    localWsReady = false;
                    setTimeout(initLocalWsClient, 3000);
                };
                localWsClient = ws;
            } catch(e) {}
        }
        initLocalWsClient();

        async function fetchMediaViaLocalWs(url) {
            if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") return null;
            if (!localWsReady || !localWsClient || !url || (!url.includes('/v1/media/') && !url.includes('/media/'))) return null;
            let clean = url.replace(/^https?:\/\/[^\/]+/, '').replace('/v1/media/', '').replace('/media/', '').split('?')[0];
            let parts = clean.split('/');
            if (parts.length < 3) return null;
            let storyId = parts[0];
            let chapterId = parts[1];
            let filePath = parts.slice(2).join('/');
            
            return new Promise((resolve) => {
                let reqId = 'req_' + Math.random().toString(36).substring(2);
                localWsPending[reqId] = resolve;
                try {
                    localWsClient.send(JSON.stringify({
                        type: "get_local_media",
                        request_id: reqId,
                        story_id: storyId,
                        chapter_id: chapterId,
                        file_path: filePath
                    }));
                } catch(e) {
                    delete localWsPending[reqId];
                    resolve(null);
                    return;
                }
                setTimeout(() => {
                    if (localWsPending[reqId]) {
                        delete localWsPending[reqId];
                        resolve(null);
                    }
                }, 8000);
            });
        }

        async function convertFragmentThumbnailsToLocalBlobs() {
            let images = document.querySelectorAll("#fragments-list-container img[src]");
            for (let img of images) {
                let originalUrl = img.getAttribute("src");
                if (originalUrl && (originalUrl.includes("/v1/media/") || originalUrl.includes("/media/")) && !originalUrl.startsWith("blob:")) {
                    try {
                        let blobUrl = await fetchMediaViaLocalWs(originalUrl);
                        if (blobUrl) {
                            img.src = blobUrl;
                        }
                    } catch(err) {}
                }
            }
        }

        let currentAudioPlayer = null;
        async function playFragmentAudio(url, btn) {
            let finalUrl = url;
            let blobUrl = await fetchMediaViaLocalWs(url);
            if (blobUrl) {
                finalUrl = blobUrl;
            }

            if (currentAudioPlayer) {
                currentAudioPlayer.pause();
                currentAudioPlayer = null;
                document.querySelectorAll(".frag-item button").forEach(b => {
                    if (b.innerText.includes("Playing...")) b.innerHTML = "▶️ Audio";
                });
            }
            let audio = new Audio(finalUrl);
            currentAudioPlayer = audio;
            btn.innerHTML = "⏸️ Playing...";
            audio.play();
            audio.onended = () => {
                btn.innerHTML = "▶️ Audio";
                currentAudioPlayer = null;
            };
            audio.onerror = () => {
                alert("Unable to play audio file.");
                btn.innerHTML = "▶️ Audio";
                currentAudioPlayer = null;
            };
        }

        async function openMediaPreviewModal(url, type, title, isMismatch, w, h, targetRatio) {
            let modal = document.getElementById("media-preview-modal");
            if (!modal) {
                modal = document.createElement("div");
                modal.id = "media-preview-modal";
                modal.style.cssText = "position: fixed; inset: 0; background: rgba(0,0,0,0.85); backdrop-filter: blur(8px); display: flex; flex-direction: column; justify-content: center; align-items: center; z-index: 10000; padding: 2rem;";
                document.body.appendChild(modal);
            }

            let warningHeader = (type === "image" && isMismatch)
                ? `<div style="background: rgba(245, 158, 11, 0.15); border: 1px solid #f59e0b; color: #fde047; padding: 0.65rem 1rem; border-radius: 8px; margin-bottom: 1rem; font-size: 0.83rem; font-weight: 600; display: flex; align-items: center; gap: 0.6rem; width: 100%; box-sizing: border-box;">
                        <span style="font-size: 1.2rem;">⚠️</span>
                        <span>Cảnh báo tỉ lệ ảnh: Kích thước hiện tại (<strong>${w}x${h}</strong>) không khớp với tỉ lệ <strong>${targetRatio}</strong> đã cấu hình cho project item này!</span>
                   </div>`
                : ``;

            modal.innerHTML = `
                <div style="background: rgba(20,20,30,0.95); border: 1px solid ${isMismatch ? '#f59e0b' : 'var(--border)'}; border-radius: 16px; padding: 1.5rem; max-width: 95vw; display: flex; flex-direction: column; align-items: center; position: relative;">
                    <button onclick="closeMediaPreviewModal()" style="position: absolute; top: 1rem; right: 1rem; background: rgba(255,255,255,0.1); border: none; color: #fff; width: 32px; height: 32px; border-radius: 50%; cursor: pointer; font-size: 1.1rem; display: flex; justify-content: center; align-items: center;">✕</button>
                    <h3 style="font-size: 1rem; font-weight: 700; color: ${isMismatch ? '#f59e0b' : 'var(--primary)'}; margin-bottom: 1rem;">${title || 'Media Preview'}</h3>
                    ${warningHeader}
                    <div id="media-modal-container" style="min-width: 320px; min-height: 180px; display: flex; justify-content: center; align-items: center;">
                        <p style="color: var(--text-muted); font-size: 0.9rem;">⚡ Loading local media...</p>
                    </div>
                </div>
            `;
            modal.style.display = "flex";
            modal.onclick = (e) => {
                if (e.target === modal) closeMediaPreviewModal();
            };

            let mediaContainer = document.getElementById("media-modal-container");
            let finalUrl = url;
            try {
                let blobUrl = await fetchMediaViaLocalWs(url);
                if (blobUrl) {
                    finalUrl = blobUrl;
                }
            } catch(e) {}

            if (mediaContainer) {
                if (type === "image") {
                    mediaContainer.innerHTML = `<img src="${finalUrl}" style="max-width: 85vw; max-height: 75vh; border-radius: 12px; border: 1px solid ${isMismatch ? '#f59e0b' : 'var(--border)'}; box-shadow: 0 10px 40px rgba(0,0,0,0.8);" />`;
                } else {
                    mediaContainer.innerHTML = `<video src="${finalUrl}" controls autoplay style="max-width: 85vw; max-height: 75vh; border-radius: 12px; border: 1px solid var(--border); box-shadow: 0 10px 40px rgba(0,0,0,0.8);"></video>`;
                }
            }
        }

        function closeMediaPreviewModal() {
            let modal = document.getElementById("media-preview-modal");
            if (modal) {
                modal.style.display = "none";
                modal.innerHTML = "";
            }
        }

        function openFinalVideoPreview() {
            if (!activeStoryId || !activeChapterId) return;
            let currentWs = (typeof activeWsId !== 'undefined' && activeWsId) ? activeWsId : (typeof wsId !== 'undefined' && wsId ? wsId : '');
            let wsParam = currentWs ? `&ws=${encodeURIComponent(currentWs)}` : '';
            let videoUrl = `/v1/media/${encodeURIComponent(activeStoryId)}/${encodeURIComponent(activeChapterId)}/final.mp4?t=${Date.now()}${wsParam}`;
            openMediaPreviewModal(videoUrl, 'video', `🎬 Final Video - ${activeStoryId} / ${activeChapterId}`);
        }

        function updateFragmentHighlights() {
            let sEl = document.getElementById("frag-start-input");
            let eEl = document.getElementById("frag-end-input");
            if (!sEl || !eEl) return;
            
            let fragItems = document.querySelectorAll("#fragments-list-container .frag-item");
            let totalFrags = fragItems.length || 999;

            let startNum = Math.max(1, Math.min(parseInt(sEl.value) || 1, totalFrags));
            let endNum = Math.max(startNum, Math.min(parseInt(eEl.value) || totalFrags, totalFrags));

            sEl.value = startNum;
            eEl.value = endNum;
            sEl.setAttribute("max", totalFrags);
            eEl.setAttribute("max", totalFrags);
            
            let subtitleEl = document.getElementById("fragments-subtitle-info");
            if (subtitleEl) {
                let count = Math.max(0, endNum - startNum + 1);
                subtitleEl.innerText = `Selected Range: Fragment #${startNum} → #${endNum} (${count} clips included in pipeline)`;
            }
            
            fragItems.forEach(item => {
                let idx = parseInt(item.dataset.index);
                let isSelected = (idx >= startNum && idx <= endNum);
                if (isSelected) {
                    item.style.background = "rgba(139, 92, 246, 0.14)";
                    item.style.borderColor = "#8b5cf6";
                    item.style.boxShadow = "0 0 10px rgba(139, 92, 246, 0.2)";
                    item.style.opacity = "1";
                    let badge = item.querySelector(".frag-badge");
                    if (badge) {
                        badge.style.color = "#c084fc";
                        badge.innerText = `🎯 #${idx}`;
                    }
                } else {
                    item.style.background = "rgba(255,255,255,0.02)";
                    item.style.borderColor = "var(--border)";
                    item.style.boxShadow = "none";
                    item.style.opacity = "0.55";
                    let badge = item.querySelector(".frag-badge");
                    if (badge) {
                        badge.style.color = "var(--text-muted)";
                        badge.innerText = `#${idx}`;
                    }
                }
            });
        }

        function toggleFragmentsAccordion() {
            let listContainer = document.getElementById("fragments-list-container");
            let arrow = document.getElementById("fragments-arrow");
            if (!listContainer) return;
            if (listContainer.style.display === "none") {
                listContainer.style.display = "flex";
                if (arrow) arrow.innerText = "▼";
            } else {
                listContainer.style.display = "none";
                if (arrow) arrow.innerText = "▶";
            }
        }

        async function startPipelineForActiveProject(mode) {
            if (!activeStoryId || !activeChapterId) {
                alert("Please select a chapter first!");
                return;
            }
            let btnAll = document.getElementById("btn-run-all");
            if (btnAll && btnAll.disabled && btnAll.title.includes("running")) {
                alert("⚠️ A pipeline job is already in progress. Please wait or cancel the current job.");
                return;
            }

            if (mode === "subtitles_only" && document.getElementById("btn-run-subtitles") && document.getElementById("btn-run-subtitles").disabled) {
                alert("⚠️ Cannot run Subtitles Only: Please generate Audio clips first!");
                return;
            }
            if (mode === "video_only" && document.getElementById("btn-run-video") && document.getElementById("btn-run-video").disabled) {
                alert("⚠️ Cannot run Video Render Only: Please generate both Images and Audio clips first!");
                return;
            }

            // Immediately lock UI buttons and show cancel button
            [document.getElementById("btn-run-all"), document.getElementById("btn-run-subtitles"), document.getElementById("btn-run-images"), document.getElementById("btn-run-audio"), document.getElementById("btn-run-video")].forEach(b => {
                if (b) {
                    b.disabled = true;
                    b.style.opacity = "0.45";
                    b.style.cursor = "not-allowed";
                }
            });
            let cancelBtn = document.getElementById("btn-cancel-job");
            if (cancelBtn) cancelBtn.style.display = "inline-flex";

            let itemEl = document.querySelector(`.chapter-item[data-chapter-id="${CSS.escape(activeChapterId)}"]`);
            if (itemEl) {
                let badgeEl = itemEl.querySelector(".ch-status-badge");
                if (badgeEl) {
                    badgeEl.style.color = "#f59e0b";
                    badgeEl.innerText = "⚡ processing";
                }
            }

            let voiceId = document.getElementById("voice-select") ? document.getElementById("voice-select").value : null;
            let ttsProvider = document.getElementById("tts-provider-select") ? document.getElementById("tts-provider-select").value : "omnivoice";
            let artStyle = document.getElementById("art-style-select") ? document.getElementById("art-style-select").value : "comic";
            let aspectRatio = document.getElementById("aspect-ratio-select") ? document.getElementById("aspect-ratio-select").value : "9:16";
            let subtitlePreset = document.getElementById("subtitle-preset-select") ? document.getElementById("subtitle-preset-select").value : "viral-bold-yellow";
            let effectType = document.getElementById("effect-type-select") ? document.getElementById("effect-type-select").value : "leaves";
            
            let fragStartEl = document.getElementById("frag-start-input");
            let fragEndEl = document.getElementById("frag-end-input");
            let fragStart = fragStartEl ? (parseInt(fragStartEl.value) || 1) : 1;
            let fragEnd = fragEndEl ? (parseInt(fragEndEl.value) || 5) : 5;

            let useWatermark = document.getElementById("toggle-watermark") ? document.getElementById("toggle-watermark").checked : false;
            let useSubtitles = document.getElementById("toggle-subtitles") ? document.getElementById("toggle-subtitles").checked : true;
            let useWaveform = document.getElementById("toggle-waveform") ? document.getElementById("toggle-waveform").checked : false;

            let url = `/v1/projects/${encodeURIComponent(activeStoryId)}/${encodeURIComponent(activeChapterId)}/run`;
            let banner = document.getElementById("status-banner");
            if (banner) {
                banner.innerHTML = `<span style="color: #f59e0b; font-weight: bold;">⚡ Launching Pipeline (${mode}, Frags ${fragStart}-${fragEnd})...</span>`;
                banner.style.borderColor = "rgba(245, 158, 11, 0.4)";
                banner.style.background = "rgba(245, 158, 11, 0.15)";
            }
            try {
                let res = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        voice_config: { 
                            voice_id: voiceId, 
                            provider: ttsProvider,
                            start_fragment: fragStart,
                            end_fragment: fragEnd,
                            limit_fragments: Math.max(0, fragEnd - fragStart + 1)
                        },
                        art_style: artStyle,
                        aspect_ratio: aspectRatio,
                        subtitle_preset: subtitlePreset,
                        effect_type: effectType,
                        use_watermark: useWatermark,
                        use_subtitles: useSubtitles,
                        use_waveform: useWaveform,
                        rerun_mode: mode,
                        start_fragment: fragStart,
                        end_fragment: fragEnd
                    })
                });
                if (res.ok) {
                    alert(`🚀 Pipeline launched (${mode}, Fragments ${fragStart} to ${fragEnd})! Agent is rendering in background.`);
                    updateCurrentChapterStatusBanner();
                } else {
                    let err = await res.json();
                    alert("Failed: " + (err.detail || "Error launching pipeline"));
                    updateCurrentChapterStatusBanner();
                }
            } catch(e) {
                alert("Error: " + e.message);
                updateCurrentChapterStatusBanner();
            }
        }

        async function cancelPipelineForActiveProject() {
            if (!activeStoryId || !activeChapterId) return;
            if (!confirm(`⛔ Bạn có chắc chắn muốn HỦY TIẾN TRÌNH cho ${activeChapterId}? (Sẽ dừng toàn bộ Render & các Job ima2-gen đang chạy)`)) return;

            let banner = document.getElementById("status-banner");
            if (banner) {
                banner.innerHTML = `<span style="color: #ef4444; font-weight: bold;">⏳ Đang gửi lệnh HỦY TIẾN TRÌNH & ima2-gen...</span>`;
                banner.style.borderColor = "rgba(239, 68, 68, 0.4)";
                banner.style.background = "rgba(239, 68, 68, 0.15)";
            }

            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(activeStoryId)}/${encodeURIComponent(activeChapterId)}/cancel`, {
                    method: "POST"
                });
                if (res.ok) {
                    let data = await res.json();
                    alert("⛔ Đã HỦY THÀNH CÔNG toàn bộ tiến trình và các job ima2-gen!");
                    updateCurrentChapterStatusBanner();
                } else {
                    let err = await res.json();
                    alert("Lỗi khi hủy: " + (err.detail || "Không thể hủy"));
                    updateCurrentChapterStatusBanner();
                }
            } catch(e) {
                alert("Lỗi kết nối: " + e.message);
                updateCurrentChapterStatusBanner();
            }
        }

        async function loadVoicesDashboard() {
            let grid = document.getElementById("voices-grid");
            grid.innerHTML = `<p style="color: var(--text-muted);">Loading OmniVoice profiles...</p>`;
            try {
                let res = await fetch("/v1/voices");
                let voices = await res.json();
                grid.innerHTML = "";
                voices.forEach(v => {
                    let card = document.createElement("div");
                    card.style.cssText = "background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 12px; padding: 1.2rem;";
                    let isProtected = v.is_protected || v.id === 'nam-dao-ly' || v.id === 'nu-doc-truyen';
                    let protectedTag = isProtected ? `<span style="background: rgba(245,158,11,0.15); border: 1px solid #f59e0b; color: #f59e0b; font-size: 0.7rem; padding: 0.2rem 0.5rem; border-radius: 50px; font-weight: bold;">🔒 Protected</span>` : "";
                    
                    card.innerHTML = `
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem;">
                            <h4 style="font-size: 1.05rem; font-weight: 700; color: #fff;">🎙️ ${v.name || v.id}</h4>
                            ${protectedTag}
                        </div>
                        <p style="font-size: 0.8rem; color: var(--text-muted); background: rgba(0,0,0,0.2); padding: 0.6rem; border-radius: 6px; margin-bottom: 1rem;">
                            "${v.ref_text || 'Xin chào, đây là giọng đọc mẫu từ gờ o gờ chấm zone...'}"
                        </p>
                        <button onclick="playVoicePreview('${v.id}')" style="background: rgba(16,185,129,0.2); border: 1px solid #10b981; color: #10b981; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem;">▶ Play Sample Audio</button>
                    `;
                    grid.appendChild(card);
                });
            } catch(e) {
                grid.innerHTML = `<p style="color: var(--danger);">Failed to load voices.</p>`;
            }
        }

        function playVoicePreview(voiceId) {
            let url = `/v1/voices/${encodeURIComponent(voiceId)}/ref.wav`;
            let a = new Audio(url);
            a.play().catch(e => alert("Audio playback error: " + e.message));
        }

        function openNewProjectModal() {
            document.getElementById("new-project-modal").style.display = "flex";
            loadLoreKeeperStories();
        }

        function closeNewProjectModal() {
            document.getElementById("new-project-modal").style.display = "none";
        }

        function selectNewCategory(cat) {
            selectedNewCategory = cat;
            document.querySelectorAll('.cat-choice').forEach(c => c.classList.remove('active'));
            document.getElementById('cat-choice-' + cat).classList.add('active');
            
            if (cat === 'story') {
                document.getElementById('story-select-group').style.display = 'block';
                document.getElementById('custom-name-group').style.display = 'none';
            } else {
                document.getElementById('story-select-group').style.display = 'none';
                document.getElementById('custom-name-group').style.display = 'block';
            }
        }

        async function loadLoreKeeperStories() {
            let select = document.getElementById("lore-keeper-select");
            select.innerHTML = `<option value="">Loading stories from Lore-Keeper...</option>`;
            try {
                let res = await fetch("/v1/lore-keeper/stories");
                let stories = await res.json();
                select.innerHTML = "";
                stories.forEach(s => {
                    let opt = document.createElement("option");
                    opt.value = s.id;
                    opt.innerText = s.title || s.id;
                    select.appendChild(opt);
                });
            } catch(e) {
                select.innerHTML = `<option value="bang">Băng (Fallback Default)</option>`;
            }
        }

        async function handleCreateProjectSubmit(e) {
            e.preventDefault();
            let storyId = document.getElementById("lore-keeper-select").value;
            let customName = document.getElementById("custom-proj-input").value.trim();
            let lang = document.getElementById("new-lang-select").value;
            let aspect = document.getElementById("new-aspect-select").value;

            let payload = {
                project_type: selectedNewCategory,
                story_id: selectedNewCategory === 'story' ? storyId : null,
                project_name: selectedNewCategory !== 'story' ? customName : null,
                language: lang,
                aspect_ratio: aspect
            };

            try {
                let res = await fetch("/v1/projects/create", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                let data = {};
                try { data = await res.json(); } catch(e) {}
                if (res.ok) {
                    closeNewProjectModal();
                    alert("🎉 Project created successfully!");
                    await loadProjects();
                    openProjectWorkspace(data.project_name || storyId || customName);
                } else {
                    alert("Error (" + res.status + "): " + (data.detail || "Creation failed"));
                }
            } catch(err) {
                alert("Creation error: " + err.message);
            }
        }

        async function deleteProject(e, storyId, projectTitle) {
            if (e) e.stopPropagation();
            let confirmMsg = `Bạn có chắc chắn muốn xóa Project "${projectTitle || storyId}"? (Thao tác này không thể hoàn tác)`;
            if (!confirm(confirmMsg)) return;

            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(storyId)}`, {
                    method: "DELETE"
                });
                if (res.ok) {
                    alert("🗑️ Đã xóa project thành công!");
                    loadProjects();
                } else {
                    let data = {};
                    try { data = await res.json(); } catch(err) {}
                    alert("Lỗi khi xóa: " + (data.detail || "Không thể xóa project"));
                }
            } catch(err) {
                alert("Lỗi khi xóa project: " + err.message);
            }
        }

        function updateEpisodeLabelDefault() {
            let epVal = parseInt(document.getElementById("new-item-episode-input").value) || 1;
            let lang = document.getElementById("new-item-lang-select") ? document.getElementById("new-item-lang-select").value : "vi";
            let epStr = epVal < 10 ? "0" + epVal : epVal;
            document.getElementById("new-item-episode-label-input").value = (lang === "vi" ? `Tập ${epStr}` : `Episode ${epStr}`);
        }

        function openAddItemModal() {
            if (!activeStoryId) {
                alert("Please select or open a project first!");
                return;
            }
            document.getElementById("new-item-title-input").value = "";
            document.getElementById("new-item-short-title-input").value = "";
            document.getElementById("new-item-slug-input").value = "";
            document.getElementById("new-item-content-input").value = "";
            
            let proj = allProjectsList.find(p => p.story_id === activeStoryId);
            let nextEp = (proj && proj.chapters) ? proj.chapters.length + 1 : 1;
            document.getElementById("new-item-episode-input").value = nextEp;
            updateEpisodeLabelDefault();

            if (document.getElementById("item-json-file-input")) document.getElementById("item-json-file-input").value = "";

            let defaultAspect = (proj && (proj.project_type === "reels" || proj.story_id.includes("reels"))) ? "9:16" : "16:9";
            if (document.getElementById("new-item-aspect-select")) {
                document.getElementById("new-item-aspect-select").value = defaultAspect;
            }

            document.getElementById("add-item-modal").style.display = "flex";
        }

        function closeAddItemModal() {
            document.getElementById("add-item-modal").style.display = "none";
        }

        function autoPopulateItemSlug() {
            let shortTitle = document.getElementById("new-item-short-title-input").value.trim();
            let mainTitle = document.getElementById("new-item-title-input").value.trim();
            let src = shortTitle || mainTitle;
            if (src) {
                let slug = src.toLowerCase()
                    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
                    .replace(/[đĐ]/g, "d")
                    .replace(/[^a-z0-9\s-]/g, "")
                    .trim().replace(/\s+/g, "-");
                document.getElementById("new-item-slug-input").value = slug;
            }
        }

        function handleItemJsonFileUpload(event) {
            let file = event.target.files[0];
            if (!file) return;
            let reader = new FileReader();
            reader.onload = function(e) {
                try {
                    let text = e.target.result;
                    if (file.name.endsWith(".json")) {
                        let data = JSON.parse(text);
                        if (data.episode !== undefined) document.getElementById("new-item-episode-input").value = data.episode;
                        if (data.episode_label) document.getElementById("new-item-episode-label-input").value = data.episode_label;
                        if (data.title) document.getElementById("new-item-title-input").value = data.title;
                        if (data.short_title) document.getElementById("new-item-short-title-input").value = data.short_title;
                        if (data.slug) document.getElementById("new-item-slug-input").value = data.slug;
                        else autoPopulateItemSlug();
                        if (data.channel) document.getElementById("new-item-channel-input").value = data.channel;
                        if (data.aspect_ratio) document.getElementById("new-item-aspect-select").value = data.aspect_ratio;
                        if (data.language) document.getElementById("new-item-lang-select").value = data.language;
                        if (data.content) document.getElementById("new-item-content-input").value = data.content;
                    } else {
                        document.getElementById("new-item-content-input").value = text;
                        if (!document.getElementById("new-item-title-input").value) {
                            let titleGuess = file.name.replace(/\.[^/.]+$/, "").replace(/[-_]/g, " ");
                            document.getElementById("new-item-title-input").value = titleGuess;
                            autoPopulateItemSlug();
                        }
                    }
                } catch(err) {
                    alert("Lỗi khi đọc file JSON: " + err.message);
                }
            };
            reader.readAsText(file);
        }

        async function handleAddItemSubmit(e) {
            e.preventDefault();
            if (!activeStoryId) return;

            let episode = parseInt(document.getElementById("new-item-episode-input").value) || 1;
            let episodeLabel = document.getElementById("new-item-episode-label-input").value.trim();
            let title = document.getElementById("new-item-title-input").value.trim();
            let shortTitle = document.getElementById("new-item-short-title-input").value.trim();
            let slug = document.getElementById("new-item-slug-input").value.trim();
            let channel = document.getElementById("new-item-channel-input").value.trim();
            let aspectRatio = document.getElementById("new-item-aspect-select").value;
            let language = document.getElementById("new-item-lang-select").value;
            let content = document.getElementById("new-item-content-input").value.trim();

            if (!title || !slug) {
                alert("Vui lòng nhập đầy đủ Tiêu đề và Slug ID!");
                return;
            }

            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(activeStoryId)}/items/add`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        episode: episode,
                        episode_label: episodeLabel,
                        title: title,
                        short_title: shortTitle,
                        slug: slug,
                        item_id: slug,
                        channel: channel,
                        aspect_ratio: aspectRatio,
                        language: language,
                        content: content
                    })
                });
                let data = {};
                try { data = await res.json(); } catch(err) {}
                if (res.ok) {
                    closeAddItemModal();
                    alert("🎉 Item added successfully!");
                    let projRes = await fetch("/v1/projects");
                    allProjectsList = await projRes.json();
                    openProjectWorkspace(activeStoryId);
                    selectChapterInWorkspace(activeStoryId, data.item_id, data.title);
                } else {
                    alert("Error: " + (data.detail || "Failed to add item"));
                }
            } catch(err) {
                alert("Creation error: " + err.message);
            }
        }

        async function openFolder(event, storyId, chapterId) {
            if (event) event.stopPropagation();
            let url = chapterId ? `/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/open-folder` : `/v1/projects/${encodeURIComponent(storyId)}/open-folder`;
            try {
                let res = await fetch(url, { method: "POST" });
                if (!res.ok) {
                    let err = await res.json();
                    alert("Failed to open folder: " + (err.detail || "Unknown error"));
                }
            } catch(e) {
                alert("Error opening folder: " + e.message);
            }
        }

        async function deleteChapter(event, storyId, chapterId, title) {
            if (event) event.stopPropagation();
            if (!confirm(`Bạn có chắc chắn muốn xóa item '${title || chapterId}'?`)) return;
            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}`, { method: "DELETE" });
                if (res.ok) {
                    if (activeChapterId === chapterId) activeChapterId = null;
                    let projRes = await fetch("/v1/projects");
                    allProjectsList = await projRes.json();
                    openProjectWorkspace(storyId);
                } else {
                    let err = await res.json();
                    alert("Failed to delete item: " + (err.detail || "Unknown error"));
                }
            } catch(e) {
                alert("Error deleting item: " + e.message);
            }
        }

        setInterval(updateAgentStatus, 3000);
        updateAgentStatus();
        loadProjects();
    </script>
</body>
</html>"""
    return HTMLResponse(
        content=html_content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)

