import asyncio
import json
import os
import pathlib
from typing import Dict, List, Set, Optional
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
import requests
import shutil

app = FastAPI(title="Taka Coordinator Server", version="0.1.0")
AGENT_VERSION = "0.2.8"

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

# In-memory stores
active_agents: Set[WebSocket] = set()
agent_status: Dict[str, dict] = {}
project_jobs: Dict[str, dict] = {}  # project_name -> job state
pending_file_selects: Dict[str, dict] = {}
pending_agent_requests: Dict[str, dict] = {}

async def tunnel_request_to_agent(message_type: str, payload: dict, timeout: float = 10.0) -> Optional[dict]:
    if not active_agents:
        return None
    import uuid
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    pending_agent_requests[request_id] = {"event": event, "result": None}
    
    agent_ws = list(active_agents)[0]
    # Keep request_id at the root for easier parsing
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
        print(f"[Server] Tunnel request {message_type} failed: {e}")
        return None
    finally:
        pending_agent_requests.pop(request_id, None)

# Resolve Postgres connection URI (Prioritize env variable, then config fallback)
import configparser
_CONFIG_PATH = BASE_DIR / "config.ini"
config = configparser.ConfigParser()
if _CONFIG_PATH.exists():
    config.read(_CONFIG_PATH, encoding="utf-8")
POSTGRES_URI = os.getenv("POSTGRES_URI") or config.get("LORE_KEEPER", "POSTGRES_URI", fallback=None)

def fetch_postgres_document(chapter_id: str) -> str:
    """Queries Postgres directly, and falls back to Lore-Keeper API if Postgres is not connected/fails."""
    if POSTGRES_URI:
        try:
            import psycopg2
            conn = psycopg2.connect(POSTGRES_URI)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.content FROM agent_documents ad "
                    "JOIN documents d ON ad.document_id = d.id "
                    "WHERE ad.id::text = %s",
                    (chapter_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        except Exception as e:
            print(f"[Server] Direct Postgres query failed: {e}. Falling back to Lore-Keeper API...")

    # Fallback to Lore-Keeper REST API
    try:
        url = f"https://lore-keeper.taka.zone/api/chapters/{chapter_id}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok") and "chapter" in data:
            return data["chapter"]["content"]
        else:
            raise ValueError("Invalid response format from Lore-Keeper API")
    except Exception as api_err:
        raise RuntimeError(f"Failed to fetch chapter content from both Postgres and Lore-Keeper API: {api_err}")

def fetch_story_chapters(story_id: str) -> list:
    """Queries Postgres directly, and falls back to Lore-Keeper API if Postgres is not connected/fails."""
    if POSTGRES_URI:
        try:
            import psycopg2
            conn = psycopg2.connect(POSTGRES_URI)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ad.id, d.title FROM agent_documents ad "
                    "JOIN documents d ON ad.document_id = d.id "
                    "WHERE ad.story_id::text = %s ORDER BY ad.id ASC",
                    (story_id,)
                )
                rows = cur.fetchall()
                if rows:
                    return [{"id": str(r[0]), "title": r[1]} for r in rows]
        except Exception as e:
            print(f"[Server] Direct Postgres chapters query failed: {e}. Falling back to Lore-Keeper API...")

    # Fallback to Lore-Keeper REST API
    try:
        url = f"https://lore-keeper.taka.zone/api/stories/{story_id}/chapters"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok") and "chapters" in data:
            return [{"id": ch["id"], "title": ch["title"]} for ch in data["chapters"]]
        else:
            raise ValueError("Invalid response format from Lore-Keeper API")
    except Exception as api_err:
        print(f"[Server] Failed to fetch story chapters from both Postgres and Lore-Keeper API: {api_err}")
        return [
            {"id": f"chap_{story_id}_1", "title": f"Chương 1 (Mẫu - Lỗi kết nối: {str(api_err)[:20]})"},
            {"id": f"chap_{story_id}_2", "title": f"Chương 2 (Mẫu)"}
        ]

# Serve output videos and media
@app.get("/media/{story_id}/{chapter_id}/final.mp4")
async def get_final_video(story_id: str, chapter_id: str):
    video_path = PROJECTS_DIR / story_id / chapter_id / "final.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Final video not found")
    return FileResponse(str(video_path))

@app.get("/media/{story_id}/{chapter_id}/images/{image_name}")
async def get_project_image(story_id: str, chapter_id: str, image_name: str):
    image_path = PROJECTS_DIR / story_id / chapter_id / "images" / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(image_path))

# WebSocket endpoint for agent connection
@app.websocket("/v1/system/agent/ws")
async def agent_ws_endpoint(websocket: WebSocket, workspace_id: str = "default"):
    await websocket.accept()
    active_agents.add(websocket)
    print(f"[Server] Taka-Agent connected. Workspace: {workspace_id}")
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
                if project_name:
                    # Translate story_id_chapter_id back to story_id/chapter_id
                    job_key = project_name.replace("_", "/", 1)
                    project_jobs[job_key] = {
                        "status": data.get("status"),
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
        active_agents.remove(websocket)
        agent_status.pop(workspace_id, None)

@app.get("/v1/agent/status")
async def get_agent_status():
    connected = len(active_agents) > 0
    needs_update = False
    agent_ver = None
    if connected and agent_status:
        first_agent = list(agent_status.values())[0]
        agent_ver = first_agent.get("agent_version")
        if agent_ver != AGENT_VERSION:
            needs_update = True
            
    return {
        "connected": connected,
        "agents": agent_status,
        "server_version": AGENT_VERSION,
        "needs_update": needs_update,
        "agent_version": agent_ver
    }

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
curl -fsSL "$SERVER_URL/v1/system/agent/files/core/characters_descriptions.ini" -o core/characters_descriptions.ini

# 3. Configure config.ini with SERVER_URL and WORKSPACE_ID
echo "[3/6] Configuring config.ini..."
python3 -c "
import configparser, uuid, hashlib, socket

mac = uuid.getnode()
hostname = socket.gethostname()
device_hash = hashlib.md5((str(mac) + '-' + hostname).encode()).hexdigest()[:12]
default_ws = 'device_' + device_hash

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')
if not config.has_section('TAKA_AGENT'):
    config.add_section('TAKA_AGENT')
config.set('TAKA_AGENT', 'SERVER_URL', '$SERVER_URL')

ws_id = '$WORKSPACE_ID'
if ws_id == 'default_workspace' or not ws_id:
    ws_id = default_ws
config.set('TAKA_AGENT', 'WORKSPACE_ID', ws_id)

with open('config.ini', 'w', encoding='utf-8') as f:
    config.write(f)
"

# 4. Set up virtual environment
echo "[4/6] Setting up Python virtual environment..."
python3 -m venv env
source env/bin/activate

# 5. Install PyTorch and dependencies
echo "[5/6] Installing dependencies..."
# Simple platform detection for PyTorch
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "macOS detected. Installing default PyTorch..."
    pip3 install torch torchvision torchaudio
else
    echo "Linux/Other detected. Installing PyTorch with CUDA support..."
    pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
fi

pip install -r requirements.txt
pip install psycopg2-binary || echo "psycopg2-binary not installed, continuing..."

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
echo "🎉 Taka Agent Installation Complete!"
echo "============================================="
echo "Starting Taka Agent in the background..."
nohup python -u taka_agent.py > agent.log 2>&1 &
echo "Agent is running. You can check logs in ~/.taka-agent/agent.log"
echo "============================================="
"""
    return PlainTextResponse(content=script_content, media_type="text/x-shellscript")

@app.get("/v1/system/install-agent.ps1", response_class=PlainTextResponse)
async def get_install_script_ps1(request: Request, workspace_id: str = "default_workspace"):
    server_url = str(request.base_url).rstrip('/')
    
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")
    ollama_model = config.get("IMAGE_PROMPT", "OLLAMA_MODEL", fallback="qwen2.5-coder:14b")
    
    script_content = f"""
$SERVER_URL = "{server_url}"
$WORKSPACE_ID = "{workspace_id}"

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "   Taka Agent Installer v{AGENT_VERSION} (Windows PowerShell) " -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "Coordinator Server: $SERVER_URL"
Write-Host "Workspace ID:       $WORKSPACE_ID"
Write-Host "Agent Version:      {AGENT_VERSION}"
Write-Host "====================================================="

# 1. Create and change to agent directory
Write-Host "[1/6] Creating directory '$HOME\.taka-agent'..." -ForegroundColor Green
New-Item -ItemType Directory -Force -Path "$HOME\.taka-agent" | Out-Null
Set-Location -Path "$HOME\.taka-agent"

# 2. Download agent files from Server
Write-Host "[2/6] Downloading agent files from server..." -ForegroundColor Green
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/requirements-agent.txt" -OutFile "requirements.txt"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/taka_agent.py" -OutFile "taka_agent.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/config.ini" -OutFile "config.ini"

New-Item -ItemType Directory -Force -Path "core" | Out-Null
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/__init__.py" -OutFile "core/__init__.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/video_engine.py" -OutFile "core/video_engine.py"
Invoke-RestMethod -Uri "$SERVER_URL/v1/system/agent/files/core/characters_descriptions.ini" -OutFile "core/characters_descriptions.ini"

# 3. Configure config.ini with SERVER_URL and WORKSPACE_ID
Write-Host "[3/6] Configuring config.ini..." -ForegroundColor Green
# Find python command
$PYTHON_CMD = "python"
try {{
    $version = & py --version 2>$null
    if ($LASTEXITCODE -eq 0) {{ $PYTHON_CMD = "py" }}
}} catch {{}}

& $PYTHON_CMD -c "
import configparser, uuid, hashlib, socket

mac = uuid.getnode()
hostname = socket.gethostname()
device_hash = hashlib.md5((str(mac) + '-' + hostname).encode()).hexdigest()[:12]
default_ws = 'device_' + device_hash

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')
if not config.has_section('TAKA_AGENT'):
    config.add_section('TAKA_AGENT')
config.set('TAKA_AGENT', 'SERVER_URL', '$SERVER_URL')

ws_id = '$WORKSPACE_ID'
if ws_id == 'default_workspace' or not ws_id:
    ws_id = default_ws
config.set('TAKA_AGENT', 'WORKSPACE_ID', ws_id)

with open('config.ini', 'w', encoding='utf-8') as f:
    config.write(f)
"

# 4. Set up virtual environment
Write-Host "[4/6] Setting up Python virtual environment..." -ForegroundColor Green
& $PYTHON_CMD -m venv env

# 5. Install PyTorch and dependencies
Write-Host "[5/6] Installing dependencies..." -ForegroundColor Green
Write-Host "Installing PyTorch with CUDA support..." -ForegroundColor Yellow
env\\Scripts\\pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

env\\Scripts\\pip install -r requirements.txt
env\\Scripts\\pip install psycopg2-binary

# 6. Setup OmniVoice (Vietnamese Voice Cloning Tool)
Write-Host "[6/6] Pre-installing OmniVoice tool..." -ForegroundColor Green
if (-not (Test-Path "tools\OmniVoice")) {{
    Write-Host "Cloning OmniVoice repository..." -ForegroundColor Yellow
    & git clone https://github.com/k2-fsa/OmniVoice tools/OmniVoice
    if (Test-Path "tools\OmniVoice\requirements.txt") {{
        Write-Host "Installing OmniVoice requirements..." -ForegroundColor Yellow
        & pip install -r tools/OmniVoice/requirements.txt
    }}
}} else {{
    Write-Host "OmniVoice is already pre-installed."
}}

Write-Host "Pre-downloading AI models and NLTK assets (this may take a few minutes)..." -ForegroundColor Yellow
try {{
    & env\Scripts\python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); from huggingface_hub import snapshot_download; snapshot_download(repo_id='k2-fsa/OmniVoice'); snapshot_download(repo_id='openai/whisper-small'); from keybert import KeyBERT; KeyBERT()"
}} catch {{
    Write-Host "Warning: Failed to pre-download some models, they will download on first run." -ForegroundColor Gray
}}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "🎉 Taka Agent Installation Complete!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Starting Taka Agent in the background..." -ForegroundColor Yellow
Start-Process -FilePath "$HOME\.taka-agent\env\Scripts\python.exe" -ArgumentList "-u", "taka_agent.py" -WindowStyle Hidden -WorkingDirectory "$HOME\.taka-agent" -RedirectStandardOutput "$HOME\.taka-agent\agent.log" -RedirectStandardError "$HOME\.taka-agent\agent.log"
Write-Host "Agent is running. You can check logs in $HOME\.taka-agent\agent.log"
Write-Host "=============================================" -ForegroundColor Cyan
"""
    return PlainTextResponse(content=script_content, media_type="text/plain")

@app.get("/v1/system/agent/files/{filepath:path}")
async def get_agent_file(filepath: str):
    allowed_files = [
        "taka_agent.py",
        "config.ini",
        "requirements-agent.txt",
        "core/__init__.py",
        "core/video_engine.py",
        "core/characters_descriptions.ini"
    ]
    if filepath not in allowed_files:
        raise HTTPException(status_code=403, detail="Access denied")
    
    file_path = BASE_DIR / filepath
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(str(file_path))


@app.get("/v1/system/select-file")
async def select_local_file(prompt: str = "Select a file"):
    # If there is a connected local agent, route the request to it
    if len(active_agents) > 0:
        import uuid
        request_id = str(uuid.uuid4())
        event = asyncio.Event()
        pending_file_selects[request_id] = {"event": event, "path": ""}
        
        msg = {
            "type": "select_file_request",
            "request_id": request_id,
            "payload": {"prompt": prompt}
        }
        
        # Send request to the first connected agent
        agent_ws = list(active_agents)[0]
        try:
            await agent_ws.send_text(json.dumps(msg))
            # Wait for response with a 60-second timeout
            await asyncio.wait_for(event.wait(), timeout=60.0)
            result = pending_file_selects.pop(request_id, {"path": ""})
            return {"path": result.get("path", "")}
        except asyncio.TimeoutError:
            pending_file_selects.pop(request_id, None)
            raise HTTPException(status_code=504, detail="Timeout waiting for agent to select file")
        except Exception as ex:
            pending_file_selects.pop(request_id, None)
            raise HTTPException(status_code=500, detail=f"Agent error selecting file: {str(ex)}")

    # Fallback: run local osascript (only works if server is on macOS)
    import subprocess
    script = f'POSIX path of (choose file with prompt "{prompt}")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True
        )
        file_path = proc.stdout.strip()
        return {"path": file_path}
    except Exception as e:
        # AppleScript returns exit code 1 if user cancels
        if isinstance(e, subprocess.CalledProcessError):
            if "User canceled" in e.stderr or "User canceled" in e.stdout or e.returncode == 1:
                return {"path": ""}
        raise HTTPException(status_code=500, detail=f"Failed to open file dialog: {str(e)}")


@app.post("/v1/projects")
async def create_project(story_id: str):
    if not story_id.strip():
        raise HTTPException(status_code=400, detail="story_id cannot be empty")
    # Sanitize story_id to prevent path traversal
    clean_id = "".join(c for c in story_id if c.isalnum() or c in ("-", "_")).strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Invalid story_id format")
    story_dir = PROJECTS_DIR / clean_id
    story_dir.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "story_id": clean_id}


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
async def list_projects():
    stories = []
    story_ids = []
    agent_files = {}
    
    if active_agents:
        res = await tunnel_request_to_agent("list_projects_request", {}, timeout=5.0)
        if res:
            story_ids = res.get("story_folders", [])
            agent_files = res.get("local_files", {})
            print(f"[Server] Fetched project folders from Agent: {story_ids}")
            
    if not active_agents or not story_ids:
        # Fallback to local server Projects directory
        if PROJECTS_DIR.exists():
            story_ids = [item.name for item in PROJECTS_DIR.iterdir() if item.is_dir() and not item.name.startswith(".") and item.name != "test_project_1"]
            for s_id in story_ids:
                s_dir = PROJECTS_DIR / s_id
                for ch_dir in s_dir.iterdir():
                    if ch_dir.is_dir() and not ch_dir.name.startswith("."):
                        ch_id = ch_dir.name
                        key = f"{s_id}/{ch_id}"
                        agent_files[key] = {
                            "has_story": (ch_dir / "story.txt").exists(),
                            "has_video": (ch_dir / "final.mp4").exists() or (ch_dir / f"{s_id}_{ch_id}.mp4").exists()
                        }

    for story_id in story_ids:
        if story_id == "music":
            chapters = []
            music_keys = [k for k in agent_files.keys() if k.startswith("music/")]
            for key in music_keys:
                ch_id = key.split("/", 1)[1]
                ch_title = ch_id.replace("-", " ").replace("_", " ").title()
                
                job_key = f"music/{ch_id}"
                job_state = project_jobs.get(job_key, {"status": "idle"})
                
                has_story = agent_files[key].get("has_story", False)
                has_video = agent_files[key].get("has_video", False)
                if has_video and job_state.get("status") == "idle":
                    job_state["status"] = "completed"
                    
                chapters.append({
                    "id": ch_id,
                    "title": ch_title,
                    "has_story": has_story,
                    "has_video": has_video,
                    "status": job_state.get("status", "idle"),
                    "progress": job_state,
                    "is_music": True
                })
            stories.append({
                "story_id": "music",
                "chapters": sorted(chapters, key=lambda x: x["id"])
            })
            continue
            
        db_chapters = fetch_story_chapters(story_id)
        chapters = []
        for ch in db_chapters:
            ch_id = ch["id"]
            ch_title = ch["title"]
            key = f"{story_id}/{ch_id}"
            
            job_key = f"{story_id}/{ch_id}"
            job_state = project_jobs.get(job_key, {"status": "idle"})
            
            has_story = agent_files.get(key, {}).get("has_story", False)
            has_video = agent_files.get(key, {}).get("has_video", False)
            
            if has_video and job_state.get("status") == "idle":
                job_state["status"] = "completed"
                
            chapters.append({
                "id": ch_id,
                "title": ch_title,
                "has_story": has_story,
                "has_video": has_video,
                "status": job_state.get("status", "idle"),
                "progress": job_state
            })
        stories.append({
            "story_id": story_id,
            "chapters": chapters
        })
        
    return stories

@app.get("/v1/projects/{story_id}/{chapter_id}/status")
async def get_project_status(story_id: str, chapter_id: str):
    job_key = f"{story_id}/{chapter_id}"
    job_state = project_jobs.get(job_key, {"status": "idle"})
    final_file = PROJECTS_DIR / story_id / chapter_id / "final.mp4"
    if final_file.exists() and job_state.get("status") == "idle":
        job_state["status"] = "completed"
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
    start_fragment: Optional[int] = 0
    limit_fragments: Optional[int] = 0

class RunPipelineRequest(BaseModel):
    voice_config: Optional[VoiceConfig] = None
    art_style: Optional[str] = None
    use_watermark: Optional[bool] = True
    use_subtitles: Optional[bool] = True
    use_whisper: Optional[bool] = False

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

@app.get("/v1/voices")
async def list_voices():
    voices_list = []
    if active_agents:
        res = await tunnel_request_to_agent("list_voices_request", {}, timeout=5.0)
        if res and "voices" in res:
            voices_list = res["voices"]
            print(f"[Server] Fetched voices list from Agent: {[v['id'] for v in voices_list]}")
            return sorted(voices_list, key=lambda x: x["id"])
            
    # Fallback to local server folder
    if VOICES_DIR.exists():
        for item in VOICES_DIR.iterdir():
            if item.is_dir():
                voice_id = item.name
                has_audio = (item / "ref.wav").exists() or (item / "local_path.txt").exists()
                has_text = (item / "ref_text.txt").exists()
                voices_list.append({
                    "id": voice_id,
                    "name": voice_id,
                    "has_audio": has_audio,
                    "has_text": has_text
                })
    return sorted(voices_list, key=lambda x: x["id"])

@app.post("/v1/voices")
async def create_voice(
    voice_id: str = Form(...),
    ref_text: str = Form(""),
    local_path: str = Form(""),
    file: Optional[UploadFile] = File(None)
):
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
    if active_agents:
        tunnel_payload = {
            "voice_id": clean_id,
            "ref_text": ref_text,
            "local_path": local_path,
            "ref_audio_b64": file_b64
        }
        res = await tunnel_request_to_agent("save_voice_request", tunnel_payload, timeout=10.0)
        print(f"[Server] Saved voice profile on Agent: {res}")
        
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
        with open(voice_dir / "local_path.txt", "w", encoding="utf-8") as f:
            f.write(local_path.strip())
        ref_audio = voice_dir / "ref.wav"
        if ref_audio.exists():
            ref_audio.unlink()
            
    if ref_text.strip():
        with open(voice_dir / "ref_text.txt", "w", encoding="utf-8") as f:
            f.write(ref_text.strip())
    else:
        ref_text_file = voice_dir / "ref_text.txt"
        if ref_text_file.exists():
            ref_text_file.unlink()
            
    return {"ok": True, "voice_id": clean_id}

@app.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str):
    clean_id = "".join(c for c in voice_id if c.isalnum() or c in ("-", "_")).strip()
    
    if active_agents:
        await tunnel_request_to_agent("delete_voice_request", {"voice_id": clean_id})
        
    voice_dir = VOICES_DIR / clean_id
    if voice_dir.exists() and voice_dir.is_dir():
        try:
            shutil.rmtree(voice_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete voice profile: {str(e)}")
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Voice profile not found")

@app.get("/v1/projects/{story_id}/{chapter_id}/fragments")
async def get_project_fragments(story_id: str, chapter_id: str):
    content = ""
    if story_id == "music":
        project_dir = PROJECTS_DIR / "music" / chapter_id
        story_file = project_dir / "story.txt"
        if story_file.exists():
            content = story_file.read_text(encoding="utf-8")
        else:
            # Fallback to source story file in downloaded_albums
            music_story_dir = PROJECTS_DIR.parent / "downloaded_albums/music"
            if music_story_dir.exists():
                for p in music_story_dir.glob("*.txt"):
                    if chapter_id.replace("_", " ").replace("-", " ").lower() in p.name.lower():
                        content = p.read_text(encoding="utf-8")
                        break
    else:
        try:
            content = fetch_postgres_document(chapter_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch content from database: {str(e)}")

    if not content.strip():
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
        
        # Split by lines as fallback
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        return [{"index": i, "text": l} for i, l in enumerate(lines)]

    # For story projects: tokenize and group
    try:
        import nltk
        try:
            nltk.data.find('tokenizers/punkt')
        except LookupError:
            nltk.download('punkt', quiet=True)
        from nltk.tokenize import sent_tokenize
    except Exception:
        # Fallback sent_tokenize if nltk is not available
        import re
        def sent_tokenize(text):
            return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    import re
    text = re.sub(r'\s+', ' ', content).strip()
    sentences = sent_tokenize(text)
    
    punctuation_list = [',', ';', ':']
    new_sentences = []
    frag_len = 12
    try:
        frag_len = config.getint("STABLE_DIFFUSION", "FRAGMENT_LENGTH", fallback=12)
    except Exception:
        pass
        
    for sent in sentences:
        words = sent.split()
        if len(words) <= frag_len:
            new_sentences.append(sent)
        else:
            part = []
            for word in words:
                part.append(word)
                if word[-1] in punctuation_list and len(part) > 3 * frag_len:
                    new_sentences.append(' '.join(part))
                    part = []
            if part:
                new_sentences.append(" ".join(part))
                
    fragments = []
    current_words = []
    for sent in new_sentences:
        current_words.extend(sent.split())
        if len(current_words) > frag_len:
            fragments.append(" ".join(current_words))
            current_words = []
    if current_words:
        fragments.append(" ".join(current_words))
        
    return [{"index": i, "text": f} for i, f in enumerate(fragments)]

@app.post("/v1/projects/{story_id}/{chapter_id}/run")
async def run_project_pipeline(story_id: str, chapter_id: str, request_data: Optional[RunPipelineRequest] = None):
    if not active_agents:
        raise HTTPException(status_code=400, detail="No active Taka-Agent connected. Please start the agent first.")
    
    project_dir = PROJECTS_DIR / story_id / chapter_id
    project_dir.mkdir(parents=True, exist_ok=True)
    story_file = project_dir / "story.txt"

    # Fetch content from Postgres/Lore-Keeper (Only if not a music project)
    if story_id != "music":
        try:
            print(f"[Server] Fetching story content for chapter_id={chapter_id} from Postgres...")
            content = fetch_postgres_document(chapter_id)
            with open(story_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[Server] Successfully wrote story content to {story_file}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch content from Postgres: {str(e)}")

        if not story_file.exists():
            raise HTTPException(status_code=404, detail="story.txt not found. Failed to write chapter content.")

    # Process voice_config if present
    voice_payload = {}
    if request_data and request_data.voice_config:
        vc = request_data.voice_config
        selected_voice_id = vc.voice_id
        
        # Hardcode provider and mode for OmniVoice cloning
        voice_payload["provider"] = "omnivoice"
        voice_payload["omnivoice_mode"] = "clone"
        voice_payload["voice_id"] = selected_voice_id
        voice_payload["start_fragment"] = vc.start_fragment or 0
        voice_payload["limit_fragments"] = vc.limit_fragments or 0
        voice_payload["language"] = "vi"
        
        # Resolve voice profile from voices folder
        if selected_voice_id:
            voice_dir = VOICES_DIR / selected_voice_id
            ref_audio = voice_dir / "ref.wav"
            local_path_file = voice_dir / "local_path.txt"
            ref_text_file = voice_dir / "ref_text.txt"
            
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
                    voice_payload["ref_audio_filename"] = "ref.wav"
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
        "status": "starting",
        "current_fragment": 0,
        "total_fragments": 0,
        "fragment_status": {},
        "error": None
    }

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

    print(f"[Server] Prepared voice config payload to agent: { {k: (v[:30]+'...' if isinstance(v, str) and len(v) > 30 else v) for k, v in voice_payload.items()} }")
    # Send trigger message to the first available agent
    agent_ws = list(active_agents)[0]
    trigger_message = {
        "type": "run_pipeline",
        "payload": {
            "project_name": f"{story_id}_{chapter_id}",
            "project_path": str(project_dir),
            "voice_config": voice_payload if voice_payload else None,
            "pipeline_type": "music" if story_id == "music" else "story",
            "art_style": request_data.art_style if request_data else None,
            "use_watermark": request_data.use_watermark if request_data else True,
            "use_subtitles": request_data.use_subtitles if request_data else True,
            "use_whisper": request_data.use_whisper if request_data else False,
            "story_text": content if story_id != "music" else None,
            "music_b64": music_b64,
            "music_filename": music_filename,
            "music_local_path": music_local_path
        }
    }
    print(f"[Server] Triggering pipeline with message type: {trigger_message['type']} - payload keys: {list(trigger_message['payload'].keys())}")
    await agent_ws.send_text(json.dumps(trigger_message))
    return {"message": "Pipeline run triggered on Taka-Agent", "story_id": story_id, "chapter_id": chapter_id}

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
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Taka-Agent Story Studio</title>
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
                overflow-x: hidden;
                background-image: radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.15) 0%, transparent 40%),
                                  radial-gradient(circle at 90% 80%, rgba(16, 185, 129, 0.08) 0%, transparent 40%);
                padding: 2rem;
            }

            header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 3rem;
                border-bottom: 1px solid var(--border);
                padding-bottom: 1.5rem;
            }

            .logo-container {
                display: flex;
                align-items: center;
                gap: 1rem;
            }

            .logo-icon {
                font-size: 2.5rem;
                background: linear-gradient(135deg, var(--primary), #ec4899);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                font-weight: 800;
            }

            h1 {
                font-size: 2rem;
                font-weight: 800;
                letter-spacing: -0.05em;
            }

            .agent-badge {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                padding: 0.5rem 1.2rem;
                border-radius: 50px;
                background: var(--card-bg);
                border: 1px solid var(--border);
                font-size: 0.9rem;
                font-weight: 600;
                transition: all 0.3s ease;
            }

            .badge-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background-color: var(--text-muted);
            }

            .agent-badge.connected .badge-dot {
                background-color: var(--success);
                box-shadow: 0 0 10px var(--success-glow);
            }

            .grid {
                display: grid;
                grid-template-columns: 1fr 2fr;
                gap: 2rem;
            }

            @media (max-width: 900px) {
                .grid {
                    grid-template-columns: 1fr;
                }
            }

            .glass-card {
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid var(--border);
                border-radius: 20px;
                padding: 2rem;
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
                transition: transform 0.3s ease, border-color 0.3s ease;
            }

            .glass-card:hover {
                border-color: rgba(139, 92, 246, 0.2);
            }

            .card-title {
                font-size: 1.3rem;
                font-weight: 600;
                margin-bottom: 1.5rem;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }

            .project-list {
                display: flex;
                flex-direction: column;
                gap: 1rem;
            }

            .new-story-btn {
                background: rgba(139, 92, 246, 0.15);
                border: 1px dashed var(--primary);
                color: #fff;
                width: 32px;
                height: 32px;
                border-radius: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                font-weight: 800;
                font-size: 1.2rem;
                transition: all 0.2s ease;
            }

            .new-story-btn:hover {
                background: var(--primary);
                box-shadow: 0 0 10px var(--primary-glow);
                transform: scale(1.05);
            }

            .story-section {
                margin-bottom: 1.5rem;
                border-bottom: 1px solid rgba(255, 255, 255, 0.03);
                padding-bottom: 1rem;
            }

            .story-section:last-child {
                border-bottom: none;
                padding-bottom: 0;
            }

            .story-header-title {
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--primary);
                text-transform: uppercase;
                letter-spacing: 1px;
                margin-bottom: 0.8rem;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }

            .chapter-list {
                display: flex;
                flex-direction: column;
                gap: 0.6rem;
                padding-left: 0.5rem;
            }

            .chapter-item {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 0.9rem 1.1rem;
                border-radius: 10px;
                background: rgba(255, 255, 255, 0.01);
                border: 1px solid var(--border);
                cursor: pointer;
                transition: all 0.2s ease;
            }

            .chapter-item:hover, .chapter-item.active {
                background: rgba(139, 92, 246, 0.04);
                border-color: var(--primary);
                transform: translateX(4px);
            }

            .chapter-info h4 {
                font-size: 0.95rem;
                font-weight: 600;
                margin-bottom: 0.1rem;
            }

            .chapter-info p {
                font-size: 0.75rem;
                color: var(--text-muted);
            }

            .run-btn {
                background: linear-gradient(135deg, var(--primary), #a78bfa);
                border: none;
                color: #fff;
                padding: 0.5rem 1rem;
                border-radius: 8px;
                font-weight: 600;
                font-size: 0.85rem;
                cursor: pointer;
                box-shadow: 0 4px 15px var(--primary-glow);
                transition: all 0.2s ease;
            }

            .run-btn:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px var(--primary-glow);
            }

            .run-btn:disabled {
                background: var(--text-muted);
                cursor: not-allowed;
                box-shadow: none;
                transform: none;
            }

            /* Detail Section */
            .details-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--border);
                padding-bottom: 1rem;
                margin-bottom: 1.5rem;
            }

            .status-banner {
                font-size: 0.9rem;
                padding: 0.4rem 1rem;
                border-radius: 8px;
                font-weight: 600;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border);
                display: inline-block;
            }

            .status-banner.processing {
                background: rgba(245, 158, 11, 0.1);
                color: var(--warning);
                border-color: rgba(245, 158, 11, 0.2);
            }

            .status-banner.completed {
                background: rgba(16, 185, 129, 0.1);
                color: var(--success);
                border-color: rgba(16, 185, 129, 0.2);
            }

            .progress-container {
                margin: 2rem 0;
            }

            .progress-bar-wrapper {
                height: 12px;
                border-radius: 6px;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border);
                overflow: hidden;
                position: relative;
                margin-bottom: 0.8rem;
            }

            .progress-bar-fill {
                height: 100%;
                width: 0%;
                background: linear-gradient(90deg, var(--primary), #ec4899);
                border-radius: 6px;
                transition: width 0.4s ease;
                box-shadow: 0 0 10px var(--primary-glow);
            }

            .progress-text {
                display: flex;
                justify-content: space-between;
                font-size: 0.9rem;
                color: var(--text-muted);
            }

            .fragments-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
                gap: 1rem;
                margin-top: 1.5rem;
            }

            .fragment-card {
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 1rem;
                text-align: center;
                transition: all 0.2s ease;
            }

            .fragment-card.active {
                border-color: var(--primary);
                background: rgba(139, 92, 246, 0.03);
            }

            .fragment-card h4 {
                font-size: 0.85rem;
                color: var(--text-muted);
                margin-bottom: 0.5rem;
            }

            .step-indicator {
                display: flex;
                justify-content: center;
                gap: 0.4rem;
                margin-top: 0.6rem;
            }

            .step-dot {
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.1);
                border: 1px solid var(--border);
            }

            .step-dot.active {
                background: var(--primary);
                box-shadow: 0 0 5px var(--primary-glow);
            }

            .step-dot.done {
                background: var(--success);
            }

            .video-preview {
                margin-top: 2rem;
                text-align: center;
            }

            video {
                width: 100%;
                max-width: 640px;
                border-radius: 12px;
                border: 1px solid var(--border);
                outline: none;
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
            }

            /* Modal / Dialog styling */
            dialog {
                background: rgba(15, 15, 25, 0.95);
                border: 1px solid var(--border);
                border-radius: 16px;
                padding: 2rem;
                color: var(--text-color);
                width: 90%;
                max-width: 500px;
                box-shadow: 0 16px 48px rgba(0, 0, 0, 0.8), 0 0 0 1px rgba(255, 255, 255, 0.05);
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
            }
            dialog::backdrop {
                background: rgba(0, 0, 0, 0.7);
                backdrop-filter: blur(4px);
                -webkit-backdrop-filter: blur(4px);
            }
            dialog h3 {
                margin-top: 0;
                margin-bottom: 1.5rem;
                font-weight: 600;
                color: var(--text-color);
                font-size: 1.25rem;
            }
            .form-group {
                margin-bottom: 1.25rem;
            }
            .form-group label {
                display: block;
                font-size: 0.85rem;
                color: var(--text-muted);
                margin-bottom: 0.5rem;
                font-weight: 500;
            }
            .form-group select, .form-group input, .form-group textarea {
                width: 100%;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 0.6rem 0.8rem;
                color: var(--text-color);
                font-family: inherit;
                font-size: 0.9rem;
                transition: all 0.2s ease;
                box-sizing: border-box;
            }
            .form-group select:focus, .form-group input:focus, .form-group textarea:focus {
                border-color: var(--primary);
                box-shadow: 0 0 0 2px var(--primary-glow);
                outline: none;
            }
            .dialog-actions {
                display: flex;
                justify-content: flex-end;
                gap: 1rem;
                margin-top: 2rem;
            }
            .dialog-actions button {
                padding: 0.6rem 1.2rem;
                font-size: 0.9rem;
                border-radius: 8px;
                cursor: pointer;
                font-weight: 600;
                transition: all 0.2s ease;
            }
            .btn-cancel {
                background: transparent;
                border: 1px solid var(--border);
                color: var(--text-color);
            }
            .btn-cancel:hover {
                background: rgba(255, 255, 255, 0.05);
            }
            .btn-submit {
                background: var(--primary);
                border: 1px solid var(--primary);
                color: #fff;
            }
            .btn-submit:hover {
                background: #7c3aed;
                box-shadow: 0 0 15px var(--primary-glow);
            }
            .header-menu {
                display: flex;
                gap: 1.5rem;
                align-items: center;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border);
                border-radius: 30px;
                padding: 0.4rem 1.5rem;
            }
            .header-menu a {
                color: var(--text-muted);
                text-decoration: none;
                font-weight: 600;
                font-size: 0.95rem;
                transition: color 0.2s ease, text-shadow 0.2s ease;
                cursor: pointer;
            }
            .header-menu a:hover, .header-menu a.active {
                color: var(--text);
                text-shadow: 0 0 10px rgba(255,255,255,0.4);
            }
        </style>
    </head>
    <body>
        <header>
            <div class="logo-container">
                <span class="logo-icon">🔊</span>
                <h1>Taka Tales</h1>
            </div>
            <nav class="header-menu">
                <a id="nav-home" onclick="showPage('home')" class="active">Home</a>
                <a id="nav-voices" onclick="showPage('voices')">Voices</a>
                <a id="nav-music" onclick="openMusicDialog()">Music</a>
            </nav>
            <div id="agent-badge" class="agent-badge">
                <span class="badge-dot"></span>
                <span id="agent-text">Agent Offline</span>
            </div>
        </header>

        <div class="grid" id="main-grid">
            <!-- Sidebar: Project Lists -->
            <div class="glass-card">
                <div class="card-title">
                    <span>Stories & Chapters</span>
                    <div style="display: flex; gap: 0.5rem;">
                        <button class="new-story-btn" onclick="addNewStory()" title="Add New Story">+</button>
                        <button class="new-story-btn" onclick="openMusicDialog()" title="Convert Music to Video" style="background: rgba(16, 185, 129, 0.15); border-color: var(--success); color: var(--success);">🎵</button>
                    </div>
                </div>
                <div id="project-list" class="project-list">
                    <p style="color: var(--text-muted);">Loading stories...</p>
                </div>
            </div>

            <!-- Main Panel: Project details and real-time generation tracking -->
            <div class="glass-card" id="details-panel" style="display: flex; flex-direction: column; min-height: 500px; padding: 2rem;">
                <!-- Placeholder screen by default -->
                <div id="details-placeholder" style="display: flex; flex-direction: column; align-items: center; justify-content: center; flex: 1; text-align: center; padding: 2rem;">
                    <span style="font-size: 3.5rem; margin-bottom: 1.5rem; filter: drop-shadow(0 0 10px rgba(168,85,247,0.4));">🎬</span>
                    <h2 style="color: var(--primary-light); margin-bottom: 0.5rem;">Ready to produce stories</h2>
                    <p style="color: var(--text-muted); font-size: 0.95rem; max-width: 400px; line-height: 1.5; margin: 0 auto;">
                        Select a chapter from the stories on the left to configure art style, voice settings, and start generating video.
                    </p>
                    <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 1.5rem;">
                        First time running? Get help setting up in the <a href="/welcome" style="color: var(--primary-light); text-decoration: underline;">Taka Agent Setup Guide</a>.
                    </p>
                </div>

                <!-- Actual details content (hidden by default) -->
                <div id="details-content" style="display: none; width: 100%;">
                    <div class="details-header" style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 1.5rem; margin-bottom: 1.5rem;">
                        <div>
                            <h2 id="current-project-title">Project Name</h2>
                            <p id="current-project-desc" style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem;">Pipeline Status</p>
                        </div>
                        <div style="display: flex; align-items: center; gap: 1rem;">
                            <span id="status-banner" class="status-banner">Idle</span>
                            <button id="details-run-btn" class="run-btn" style="padding: 0.4rem 1rem; font-size: 0.85rem;">Run</button>
                        </div>
                    </div>

                    <div class="progress-container" style="margin-bottom: 1.5rem;">
                        <div class="progress-bar-wrapper">
                            <div id="progress-bar" class="progress-bar-fill"></div>
                        </div>
                        <div class="progress-text" style="display: flex; justify-content: space-between; font-size: 0.85rem; color: var(--text-muted); margin-top: 0.5rem;">
                            <span id="progress-percentage">0%</span>
                            <span id="progress-fraction">0 / 0 Fragments</span>
                        </div>
                    </div>

                    <div id="video-preview-container" class="video-preview" style="display: none; margin-bottom: 2rem;">
                        <h3 style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center;">
                            <span>🎬 Final Output Video</span>
                            <a id="download-video-btn" href="" download class="btn-submit" style="font-size: 0.8rem; padding: 0.4rem 0.9rem; text-decoration: none; display: inline-flex; align-items: center; gap: 0.4rem; border-radius: 6px; font-weight: 600;">
                                📥 Tải Video
                            </a>
                        </h3>
                        <video id="final-video" controls style="width: 100%; border-radius: 8px; border: 1px solid var(--border); background: #000;">
                            <source src="" type="video/video/mp4">
                            Your browser does not support the video tag.
                        </video>
                    </div>

                    <h3 style="margin-top: 2rem; margin-bottom: 1rem;">Fragments Processing State</h3>
                    <div id="fragments-grid" class="fragments-grid">
                        <!-- Dynamic fragments status -->
                    </div>
                </div>
            </div>
        </div>
        <!-- Voice Configuration Dialog -->
        <dialog id="voice-config-dialog">
            <h3 style="display:flex; justify-content:space-between; align-items:center; margin-top:0;">
                <span>🔊 Voice Configuration</span>
                <span style="font-size:0.8rem; opacity:0.6;" id="dialog-chapter-id"></span>
            </h3>
            <form id="voice-config-form" onsubmit="submitVoiceConfig(event)">
                <div class="form-group">
                    <label for="art-style-story">Visual Art Style (Phong Cách Vẽ)</label>
                    <select id="art-style-story" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; margin-bottom: 0.5rem;">
                        <option value="watercolor">Tranh minh họa màu nước cổ điển (Watercolor)</option>
                        <option value="dong_ho">Tranh dân gian Đông Hồ (Dong Ho folk art)</option>
                        <option value="son_mai">Tranh Sơn mài Việt Nam (Lacquer art)</option>
                        <option value="woodblock">Tranh khắc gỗ mộc mạc (Woodblock print)</option>
                        <option value="thuy_mac">Tranh thủy mặc / mực nho hoài cổ (Ink wash)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="vc-voice-id" style="display: flex; justify-content: space-between; align-items: center;">
                        <span>Voice ID (Giọng đọc)</span>
                        <a href="javascript:void(0)" onclick="openVoiceManagement()" style="font-size: 0.85rem; color: var(--primary-light); text-decoration: none; font-weight: 600;">➕ Quản lý giọng đọc</a>
                    </label>
                    <select id="vc-voice-id" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; margin-bottom: 0.5rem;" required>
                        <option value="">-- Select Voice Profile --</option>
                    </select>
                </div>
                
                <!-- Fragment Subset Selection -->
                <div style="border-top: 1px solid rgba(255,255,255,0.15); margin-top: 1.5rem; padding-top: 1rem;">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                        <div class="form-group">
                            <label for="vc-start-fragment">Start Fragment Index</label>
                            <input type="number" id="vc-start-fragment" min="0" value="0">
                        </div>
                        <div class="form-group">
                            <label for="vc-limit-fragments">Limit Fragments (0 = All)</label>
                            <input type="number" id="vc-limit-fragments" min="0" value="0" placeholder="e.g. 10 to 15">
                        </div>
                    </div>
                    <div style="font-size: 0.85rem; opacity: 0.75; margin-top: 0.2rem; margin-bottom: 0.8rem; color: #7ad1ff;">
                        💡 Tip: Set Limit to 10-15 to render a short 1-2 minute preview video of this chapter.
                    </div>
                </div>

                <!-- Fragment Selection UI -->
                <div style="margin-top: 1rem; border-top: 1px solid rgba(255,255,255,0.15); padding-top: 1rem;">
                    <label style="font-weight: 600; margin-bottom: 0.5rem; display: block; color: var(--text); font-size: 0.9rem;">🔎 Search & Click to Set Start Index</label>
                    <input type="text" id="story-frag-search" placeholder="Type keyword to filter fragments..." oninput="filterStoryFragments()" style="width: 100%; padding: 0.6rem; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); border-radius: 6px; margin-bottom: 0.5rem; outline: none; font-size: 0.85rem;">
                    <div id="story-frag-list" style="max-height: 180px; overflow-y: auto; background: rgba(0,0,0,0.4); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem; display: flex; flex-direction: column; gap: 0.4rem;">
                        <p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">Loading fragments...</p>
                    </div>
                </div>

                <!-- Watermark and Subtitle Toggles -->
                <div class="form-group" style="display: flex; gap: 1.5rem; margin-top: 1.2rem; margin-bottom: 1.5rem; flex-wrap: wrap;">
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="story-use-watermark" checked style="width: auto; margin-bottom: 0;"> Gắn Watermark
                    </label>
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="story-use-subtitles" checked style="width: auto; margin-bottom: 0;"> Hiển thị Phụ đề
                    </label>
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--danger); font-weight: bold;">
                        <input type="checkbox" id="story-force-rerun" style="width: auto; margin-bottom: 0;"> Chạy lại từ đầu (Xóa Cache)
                    </label>
                </div>

                <div class="dialog-actions">
                    <button type="button" class="btn-cancel" onclick="closeVoiceConfig()">Cancel</button>
                    <button type="submit" class="btn-submit">Start Pipeline</button>
                </div>
            </form>
        </dialog>

        <!-- Music to Video Dialog -->
        <dialog id="music-dialog">
            <h3 style="display:flex; justify-content:space-between; align-items:center; margin-top:0;">
                <span>🎵 Convert Music to Video</span>
                <span style="font-size:0.8rem; opacity:0.6;">Music Pipeline</span>
            </h3>
            <form id="music-form" onsubmit="submitMusicProject(event)">
                <div class="form-group">
                    <label for="music-project-name">Project Name (no spaces)</label>
                    <input type="text" id="music-project-name" required placeholder="e.g. my-favorite-song">
                </div>
                <div class="form-group">
                    <label>Music/Audio File (.mp3 / .wav / .m4a)</label>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <input type="text" id="music-path" readonly placeholder="No file selected..." style="flex: 1; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; font-size: 0.85rem;">
                        <button type="button" onclick="browseLocalFileForMusic()" style="background: var(--primary); border: 1px solid var(--primary); color: white; padding: 0.6rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.85rem;">Browse...</button>
                    </div>
                    <input type="file" id="music-file" accept="audio/*" style="display: none;">
                    <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;">
                        Or <a href="javascript:void(0)" onclick="document.getElementById('music-file').click();" style="color: var(--primary-light); text-decoration: underline;">upload file manually</a> if needed.
                    </div>
                </div>
                <div class="form-group">
                    <label for="art-style-music">Visual Art Style (Phong Cảnh Vẽ)</label>
                    <select id="art-style-music" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; margin-bottom: 0.5rem;">
                        <option value="watercolor">Tranh minh họa màu nước cổ điển (Watercolor)</option>
                        <option value="dong_ho">Tranh dân gian Đông Hồ (Dong Ho folk art)</option>
                        <option value="son_mai">Tranh Sơn mài Việt Nam (Lacquer art)</option>
                        <option value="woodblock">Tranh khắc gỗ mộc mạc (Woodblock print)</option>
                        <option value="thuy_mac">Tranh thủy mặc / mực nho hoài cổ (Ink wash)</option>
                    </select>
                </div>

                <!-- Watermark, Subtitle, and Whisper Toggles -->
                <div class="form-group" style="display: flex; gap: 1.5rem; margin-top: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap;">
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="music-use-watermark" style="width: auto; margin-bottom: 0;"> Gắn Watermark
                    </label>
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="music-use-subtitles" style="width: auto; margin-bottom: 0;"> Hiển thị Phụ đề
                    </label>
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="music-use-whisper" style="width: auto; margin-bottom: 0;"> Nhận diện Whisper
                    </label>
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: normal; cursor: pointer; color: var(--danger); font-weight: bold;">
                        <input type="checkbox" id="music-force-rerun" style="width: auto; margin-bottom: 0;"> Chạy lại từ đầu (Xóa Cache)
                    </label>
                </div>

                <div class="dialog-actions">
                    <button type="button" class="btn-cancel" onclick="closeMusicDialog()">Cancel</button>
                    <button type="submit" class="btn-submit">Upload & Start</button>
                </div>
            </form>
        </dialog>

        <!-- Voices Page Layout (Hidden by default) -->
        <div id="voices-page" style="display: none;" class="glass-card">
            <h2 style="color: var(--primary-light); margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.5rem; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem;">
                <span>🎙️ OmniVoice Voice Management</span>
            </h2>
            
            <div style="display: grid; grid-template-columns: 1.2fr 1fr; gap: 2rem;">
                <!-- Existing Voices -->
                <div>
                    <h3 style="margin-top: 0; color: var(--text); border-bottom: 1px solid var(--border); padding-bottom: 0.8rem; font-size: 1.1rem;">Cloned Voices List</h3>
                    <div id="voices-page-list" style="margin-top: 1rem; display: flex; flex-direction: column; gap: 0.8rem; max-height: 450px; overflow-y: auto; padding-right: 0.5rem;">
                        <p style="color: var(--text-muted); font-size: 0.9rem; text-align: center; padding: 2rem;">Loading voices...</p>
                    </div>
                </div>
                
                <!-- Create Voice Form -->
                <div style="border-left: 1px solid var(--border); padding-left: 2rem;">
                    <h3 style="margin-top: 0; color: var(--success); border-bottom: 1px solid var(--border); padding-bottom: 0.8rem; font-size: 1.1rem;">Clone New Voice Profile</h3>
                    <form id="create-voice-form-page" onsubmit="submitCreateVoicePage(event)" style="display: flex; flex-direction: column; gap: 1rem; margin-top: 1rem;">
                        <div class="form-group">
                            <label for="new-voice-id-page">Voice ID (No spaces, lowercase, numbers, hyphens)</label>
                            <input type="text" id="new-voice-id-page" required placeholder="e.g. giong-nu-mientay">
                        </div>
                        <div class="form-group">
                            <label>Reference Audio File (.wav / .mp3)</label>
                            <div style="display: flex; gap: 0.5rem; align-items: center;">
                                <input type="text" id="new-voice-path-page" readonly placeholder="No file selected..." style="flex: 1; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; font-size: 0.85rem;">
                                <button type="button" onclick="browseLocalFileForVoice()" style="background: var(--primary); border: 1px solid var(--primary); color: white; padding: 0.6rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.85rem;">Browse...</button>
                            </div>
                            <input type="file" id="new-voice-file-page" accept="audio/wav, audio/mpeg, audio/mp3" style="display: none;">
                            <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;">
                                Or <a href="javascript:void(0)" onclick="document.getElementById('new-voice-file-page').click();" style="color: var(--primary-light); text-decoration: underline;">upload file manually</a> if needed.
                            </div>
                        </div>
                        <div class="form-group">
                            <label for="new-voice-text-page">Reference Transcription (Text spoken in audio)</label>
                            <textarea id="new-voice-text-page" rows="4" placeholder="Optional. If left blank, local Whisper ASR will auto-transcribe it." style="resize: none;"></textarea>
                        </div>
                        <button type="submit" class="btn-submit" style="background: var(--success); border-color: var(--success); padding: 0.8rem; font-size: 0.95rem; margin-top: 0.5rem;">💾 Save Cloned Voice Profile</button>
                    </form>
                </div>
            </div>
        </div>

        <script>
            if (!localStorage.getItem("taka_visited_before")) {
                localStorage.setItem("taka_visited_before", "true");
                window.location.href = "/welcome";
            }

            async function browseLocalFileForMusic() {
                try {
                    let res = await fetch("/v1/system/select-file?prompt=Chọn tệp nhạc (audio file)");
                    let data = await res.json();
                    if (data.path) {
                        document.getElementById("music-path").value = data.path;
                        document.getElementById("music-file").value = "";
                    }
                } catch(e) {
                    alert("Không thể mở hộp thoại chọn file: " + e);
                }
            }

            async function browseLocalFileForVoice() {
                try {
                    let res = await fetch("/v1/system/select-file?prompt=Chọn file âm thanh giọng mẫu");
                    let data = await res.json();
                    if (data.path) {
                        document.getElementById("new-voice-path-page").value = data.path;
                        document.getElementById("new-voice-file-page").value = "";
                    }
                } catch(e) {
                    alert("Không thể mở hộp thoại chọn file: " + e);
                }
            }

            // Register change listeners for manual upload fallbacks after DOM content load
            window.addEventListener('DOMContentLoaded', (event) => {
                const musicFile = document.getElementById('music-file');
                if (musicFile) {
                    musicFile.addEventListener('change', function(e) {
                        if (e.target.files.length > 0) {
                            document.getElementById('music-path').value = "Staged Upload: " + e.target.files[0].name;
                        }
                    });
                }
                const voiceFile = document.getElementById('new-voice-file-page');
                if (voiceFile) {
                    voiceFile.addEventListener('change', function(e) {
                        if (e.target.files.length > 0) {
                            document.getElementById('new-voice-path-page').value = "Staged Upload: " + e.target.files[0].name;
                        }
                    });
                }
            });

            let currentStory = "";
            let currentChapter = "";
            let timerId = null;
            let storyFragments = [];

            async function updateAgentStatus() {
                try {
                    let res = await fetch("/v1/agent/status");
                    let data = await res.json();
                    let badge = document.getElementById("agent-badge");
                    let text = document.getElementById("agent-text");
                    let welcomeStatus = document.getElementById("welcome-agent-status");
                    let welcomeText = document.getElementById("welcome-status-text");
                    let welcomeDot = document.getElementById("welcome-status-dot");

                    if (data.connected) {
                        badge.classList.add("connected");
                        let info = Object.values(data.agents)[0] || {};
                        let version = info.agent_version || "";
                        
                        if (data.needs_update) {
                            text.innerHTML = `Agent Connected ${version} <span style="background: #f59e0b; color: #000; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; font-weight: bold; margin-left: 0.5rem; display: inline-block;">Update Available (v${data.server_version})</span>`;
                            
                            if (welcomeStatus) {
                                welcomeStatus.style.background = "rgba(245, 158, 11, 0.1)";
                                welcomeStatus.style.borderColor = "rgba(245, 158, 11, 0.2)";
                                welcomeStatus.style.color = "#f59e0b";
                            }
                            if (welcomeText) {
                                welcomeText.innerHTML = `Taka Agent Connected (v${version}) but an update is available (v${data.server_version})! <a href="/v1/system/install-agent.sh" style="color: #f59e0b; font-weight: bold; text-decoration: underline;">Update Now</a>`;
                            }
                            if (welcomeDot) {
                                welcomeDot.style.background = "#f59e0b";
                                welcomeDot.style.boxShadow = "0 0 8px #f59e0b";
                            }
                        } else {
                            text.innerText = "Agent Connected " + version;
                            
                            if (welcomeStatus) {
                                welcomeStatus.style.background = "rgba(16, 185, 129, 0.1)";
                                welcomeStatus.style.borderColor = "rgba(16, 185, 129, 0.2)";
                                welcomeStatus.style.color = "#10b981";
                            }
                            if (welcomeText) {
                                welcomeText.innerText = "Taka Agent connected successfully! Select a story chapter from the list on the left to start.";
                            }
                            if (welcomeDot) {
                                welcomeDot.style.background = "#10b981";
                                welcomeDot.style.boxShadow = "0 0 8px #10b981";
                            }
                        }
                    } else {
                        badge.classList.remove("connected");
                        text.innerText = "Agent Offline";
                        
                        if (welcomeStatus) {
                            welcomeStatus.style.background = "rgba(239, 68, 68, 0.1)";
                            welcomeStatus.style.borderColor = "rgba(239, 68, 68, 0.2)";
                            welcomeStatus.style.color = "#ef4444";
                        }
                        if (welcomeText) {
                            welcomeText.innerText = "Waiting for Taka Agent to connect...";
                        }
                        if (welcomeDot) {
                            welcomeDot.style.background = "#ef4444";
                            welcomeDot.style.boxShadow = "0 0 8px #ef4444";
                        }
                    }
                } catch(e) {}
            }

            async function addNewStory() {
                let storyId = prompt("Nhập Story ID mới:");
                if (!storyId || !storyId.trim()) return;
                try {
                    let res = await fetch(`/v1/projects?story_id=${encodeURIComponent(storyId.trim())}`, { method: "POST" });
                    if (res.ok) {
                        loadProjects();
                    } else {
                        let err = await res.json();
                        alert(err.detail || "Không thể tạo story");
                    }
                } catch (e) {
                    alert("Error creating story: " + e);
                }
            }

            async function loadProjects() {
                try {
                    let res = await fetch("/v1/projects");
                    let stories = await res.json();
                    let list = document.getElementById("project-list");
                    list.innerHTML = "";
                    
                    if (stories.length === 0) {
                        list.innerHTML = `<p style="color: var(--text-muted); font-size: 0.9rem; padding: 1rem;">No stories loaded yet. Click '+' to load one.</p>`;
                        return;
                    }
                    
                    stories.forEach(s => {
                        let sec = document.createElement("div");
                        sec.className = "story-section";
                        
                        let header = document.createElement("div");
                        header.className = "story-header-title";
                        if (s.story_id === "music") {
                            header.innerHTML = `🎵 Music Projects`;
                            header.style.color = "var(--success)";
                        } else {
                            header.innerHTML = `📖 Story: ${s.story_id}`;
                        }
                        sec.appendChild(header);
                        
                        let chList = document.createElement("div");
                        chList.className = "chapter-list";
                        
                        s.chapters.forEach(c => {
                            let activeClass = (s.story_id === currentStory && c.id === currentChapter) ? "active" : "";
                            let item = document.createElement("div");
                            item.className = "chapter-item " + activeClass;
                            item.onclick = () => selectChapter(s.story_id, c.id, c.title);
                            
                            let btnId = `btn-${s.story_id}-${c.id}`;
                            let isRunning = (c.status !== 'idle' && c.status !== 'completed');
                            
                            item.innerHTML = `
                                <div class="chapter-info">
                                    <h4>${c.title}</h4>
                                    <p>${c.has_video ? "🎬 Video completed" : "No video output yet"}</p>
                                </div>
                            `;
                            chList.appendChild(item);
                        });
                        
                        if (s.chapters.length === 0) {
                            chList.innerHTML = `<p style="color: var(--text-muted); font-size: 0.8rem; padding-left: 0.5rem;">No chapters found</p>`;
                        }
                        
                        sec.appendChild(chList);
                        list.appendChild(sec);
                    });
                } catch(e) {}
            }

            async function selectChapter(storyId, chapterId, title) {
                currentStory = storyId;
                currentChapter = chapterId;
                
                document.querySelectorAll(".chapter-item").forEach(item => {
                    item.classList.remove("active");
                });
                loadProjects();
                
                let placeholder = document.getElementById("details-placeholder");
                let content = document.getElementById("details-content");
                if (placeholder) placeholder.style.display = "none";
                if (content) content.style.display = "block";
                document.getElementById("current-project-title").innerText = `${storyId} - ${title}`;
                
                let runBtn = document.getElementById("details-run-btn");
                if (runBtn) {
                    runBtn.disabled = false;
                    if (storyId === "music") {
                        runBtn.onclick = (event) => runChapter(event, storyId, chapterId);
                    } else {
                        runBtn.onclick = (event) => openVoiceConfig(storyId, chapterId);
                    }
                }

                if (timerId) clearInterval(timerId);
                timerId = setInterval(() => pollChapterStatus(storyId, chapterId), 1000);
                pollChapterStatus(storyId, chapterId);
            }



            let dialogStoryId = "";
            let dialogChapterId = "";

            async function openVoiceConfig(storyId, chapterId) {
                dialogStoryId = storyId;
                dialogChapterId = chapterId;
                document.getElementById("dialog-chapter-id").innerText = chapterId;
                
                // Load voices dropdown
                await loadVoicesDropdown();
                
                try {
                    let res = await fetch("/v1/voice/defaults");
                    let defaults = await res.json();
                    
                    document.getElementById("vc-voice-id").value = defaults.voice_id;
                    document.getElementById("vc-start-fragment").value = 0;
                    document.getElementById("vc-limit-fragments").value = 0;
                } catch(e) {
                    console.error("Failed to load voice defaults: ", e);
                }

                // Fetch fragments
                storyFragments = [];
                document.getElementById("story-frag-search").value = "";
                let listContainer = document.getElementById("story-frag-list");
                listContainer.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">Loading fragments...</p>`;
                
                fetch(`/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/fragments`)
                    .then(res => res.json())
                    .then(frags => {
                        storyFragments = frags;
                        renderStoryFragments();
                    })
                    .catch(err => {
                        listContainer.innerHTML = `<p style="color: #ff6b6b; font-size: 0.85rem; padding: 0.5rem;">Failed to load fragments: ${err}</p>`;
                    });
                
                document.getElementById("voice-config-dialog").showModal();
            }

            function closeVoiceConfig() {
                document.getElementById("voice-config-dialog").close();
            }

            function renderStoryFragments(filterKeyword = "") {
                let listContainer = document.getElementById("story-frag-list");
                listContainer.innerHTML = "";
                
                let filtered = storyFragments;
                if (filterKeyword.trim()) {
                    let kw = filterKeyword.toLowerCase();
                    filtered = storyFragments.filter(f => f.text.toLowerCase().includes(kw));
                }
                
                if (filtered.length === 0) {
                    listContainer.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">No fragments found.</p>`;
                    return;
                }
                
                filtered.forEach(f => {
                    let item = document.createElement("div");
                    item.style.padding = "0.5rem 0.6rem";
                    item.style.borderRadius = "6px";
                    item.style.cursor = "pointer";
                    item.style.fontSize = "0.85rem";
                    item.style.color = "var(--text)";
                    item.style.background = "rgba(255,255,255,0.03)";
                    item.style.border = "1px solid rgba(255,255,255,0.05)";
                    item.style.transition = "all 0.2s ease";
                    item.style.display = "flex";
                    item.style.gap = "0.5rem";
                    
                    // Check if this is the currently selected start fragment
                    let startVal = parseInt(document.getElementById("vc-start-fragment").value) || 0;
                    if (f.index === startVal) {
                        item.style.background = "rgba(122, 209, 255, 0.15)";
                        item.style.borderColor = "#7ad1ff";
                        item.style.boxShadow = "0 0 8px rgba(122, 209, 255, 0.2)";
                    }
                    
                    // Hover effects
                    item.onmouseover = () => {
                        if (f.index !== parseInt(document.getElementById("vc-start-fragment").value)) {
                            item.style.background = "rgba(255,255,255,0.08)";
                        }
                    };
                    item.onmouseout = () => {
                        if (f.index !== parseInt(document.getElementById("vc-start-fragment").value)) {
                            item.style.background = "rgba(255,255,255,0.03)";
                        }
                    };
                    
                    item.onclick = () => {
                        document.getElementById("vc-start-fragment").value = f.index;
                        renderStoryFragments(document.getElementById("story-frag-search").value);
                    };
                    
                    item.innerHTML = `
                        <span style="color:#7ad1ff; font-weight:bold; min-width:24px;">#${f.index}</span>
                        <span style="flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${f.text.replace(/"/g, '&quot;')}">${f.text}</span>
                    `;
                    
                    listContainer.appendChild(item);
                });
            }

            function filterStoryFragments() {
                let kw = document.getElementById("story-frag-search").value;
                renderStoryFragments(kw);
            }

             async function submitVoiceConfig(event) {
                event.preventDefault();
                closeVoiceConfig();
                
                let artStyle = document.getElementById("art-style-story").value;
                let voiceId = document.getElementById("vc-voice-id").value;
                let startFragment = parseInt(document.getElementById("vc-start-fragment").value) || 0;
                let limitFragments = parseInt(document.getElementById("vc-limit-fragments").value) || 0;
                let useWatermark = document.getElementById("story-use-watermark").checked;
                let useSubtitles = document.getElementById("story-use-subtitles").checked;
                let forceRerun = document.getElementById("story-force-rerun") ? document.getElementById("story-force-rerun").checked : false;

                let voiceConfig = {
                    provider: "omnivoice",
                    voice_id: voiceId,
                    omnivoice_mode: "clone",
                    start_fragment: startFragment,
                    limit_fragments: limitFragments
                };

                let btnId = `btn-${dialogStoryId}-${dialogChapterId}`;
                let btn = document.getElementById(btnId);
                if (btn) {
                    btn.disabled = true;
                    btn.innerText = "Starting...";
                }
                let detailsBtn = document.getElementById("details-run-btn");
                if (detailsBtn) {
                    detailsBtn.disabled = true;
                    detailsBtn.innerText = "Starting...";
                }

                let url = `/v1/projects/${encodeURIComponent(dialogStoryId)}/${encodeURIComponent(dialogChapterId)}/run`;
                try {
                    let res = await fetch(url, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({
                            voice_config: voiceConfig,
                            art_style: artStyle,
                            use_watermark: useWatermark,
                            use_subtitles: useSubtitles,
                            force_rerun: forceRerun
                        })
                    });
                    if (!res.ok) {
                        let err = await res.json();
                        alert(err.detail || "Failed to start run");
                    }
                    selectChapter(dialogStoryId, dialogChapterId, dialogChapterId);
                } catch(e) {
                    alert("Error: " + e);
                }
            }

            // Voice Management JS Helpers
            function openVoiceManagement() {
                closeVoiceConfig();
                showPage('voices');
            }

            function closeVoiceManagement() {
                showPage('home');
            }

            function showPage(pageId) {
                document.getElementById("nav-home").classList.remove("active");
                document.getElementById("nav-voices").classList.remove("active");
                
                if (pageId === 'home') {
                    document.getElementById("nav-home").classList.add("active");
                    document.getElementById("main-grid").style.display = "grid";
                    document.getElementById("voices-page").style.display = "none";
                } else if (pageId === 'voices') {
                    document.getElementById("nav-voices").classList.add("active");
                    document.getElementById("main-grid").style.display = "none";
                    document.getElementById("voices-page").style.display = "block";
                    loadVoicesList();
                }
            }

            async function loadVoicesDropdown() {
                try {
                    let res = await fetch("/v1/voices");
                    let voices = await res.json();
                    let select = document.getElementById("vc-voice-id");
                    let currentValue = select.value;
                    select.innerHTML = '<option value="">-- Select Voice Profile --</option>';
                    voices.forEach(v => {
                        let opt = document.createElement("option");
                        opt.value = v.id;
                        opt.textContent = `${v.name} (${v.has_audio ? "Ref Audio Present" : "Missing Audio"})`;
                        select.appendChild(opt);
                    });
                    if (currentValue) {
                        select.value = currentValue;
                    }
                } catch (e) {
                    console.error("Failed to load voices: " + e);
                }
            }

            async function loadVoicesList() {
                let container = document.getElementById("voices-page-list");
                container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem; text-align: center;">Loading...</p>';
                try {
                    let res = await fetch("/v1/voices");
                    let voices = await res.json();
                    container.innerHTML = "";
                    if (voices.length === 0) {
                        container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem; text-align: center;">No voices cloned yet.</p>';
                        return;
                    }
                    voices.forEach(v => {
                        let card = document.createElement("div");
                        card.style.background = "rgba(255,255,255,0.03)";
                        card.style.border = "1px solid var(--border)";
                        card.style.borderRadius = "8px";
                        card.style.padding = "0.8rem 1rem";
                        card.style.display = "flex";
                        card.style.justifyContent = "space-between";
                        card.style.alignItems = "center";
                        
                        let info = document.createElement("div");
                        info.innerHTML = `
                            <div style="font-weight: 600; font-size: 0.95rem; color: var(--text);">${v.name}</div>
                            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.3rem;">
                                🔊 ${v.has_audio ? "Audio File Present" : "No Audio File"} | 📝 ${v.has_text ? "Transcription Present" : "No Transcription"}
                            </div>
                        `;
                        
                        let delBtn = document.createElement("button");
                        delBtn.innerHTML = "🗑️ Delete";
                        delBtn.style.background = "rgba(239, 68, 68, 0.15)";
                        delBtn.style.border = "1px solid var(--danger)";
                        delBtn.style.color = "var(--danger)";
                        delBtn.style.borderRadius = "6px";
                        delBtn.style.padding = "0.4rem 0.8rem";
                        delBtn.style.cursor = "pointer";
                        delBtn.style.fontSize = "0.85rem";
                        delBtn.onclick = () => deleteVoiceProfile(v.id);
                        
                        card.appendChild(info);
                        card.appendChild(delBtn);
                        container.appendChild(card);
                    });
                } catch(e) {
                    container.innerHTML = `<p style="color: #ff6b6b; font-size: 0.85rem; padding: 0.5rem; text-align: center;">Error: ${e}</p>`;
                }
            }

            async function submitCreateVoicePage(event) {
                event.preventDefault();
                let voiceIdInput = document.getElementById("new-voice-id-page");
                let pathInput = document.getElementById("new-voice-path-page");
                let fileInput = document.getElementById("new-voice-file-page");
                let textInput = document.getElementById("new-voice-text-page");
                
                let voiceId = voiceIdInput.value.trim();
                let file = fileInput.files[0];
                let localPath = pathInput.value.trim();
                let refText = textInput.value.trim();
                
                let isUpload = file && localPath.startsWith("Staged Upload:");
                
                if (!voiceId) {
                    alert("Voice ID is required!");
                    return;
                }
                if (!isUpload && !localPath) {
                    alert("Please select a file via Browse or choose upload manual!");
                    return;
                }
                
                let formData = new FormData();
                formData.append("voice_id", voiceId);
                formData.append("ref_text", refText);
                if (isUpload) {
                    formData.append("file", file);
                } else {
                    formData.append("local_path", localPath);
                }
                
                let submitBtn = event.target.querySelector("button[type='submit']");
                submitBtn.disabled = true;
                submitBtn.textContent = "Cloning...";
                
                let url = "/v1/voices";
                
                try {
                    let res = await fetch(url, {
                        method: "POST",
                        body: formData
                    });
                    if (!res.ok) {
                        let err = await res.json();
                        alert(err.detail || "Failed to create voice profile");
                    } else {
                        voiceIdInput.value = "";
                        pathInput.value = "";
                        fileInput.value = "";
                        textInput.value = "";
                        loadVoicesList();
                    }
                } catch(e) {
                    alert("Error creating voice profile: " + e);
                } finally {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "💾 Save Cloned Voice Profile";
                }
            }

            async function deleteVoiceProfile(voiceId) {
                if (!confirm(`Are you sure you want to delete voice profile "${voiceId}"?`)) {
                    return;
                }
                try {
                    let res = await fetch(`/v1/voices/${encodeURIComponent(voiceId)}`, {
                        method: "DELETE"
                    });
                    if (!res.ok) {
                        let err = await res.json();
                        alert(err.detail || "Failed to delete voice profile");
                    } else {
                        loadVoicesList();
                    }
                } catch(e) {
                    alert("Error: " + e);
                }
            }

            async function runChapter(event, storyId, chapterId) {
                if (event) event.stopPropagation();
                let btnId = `btn-${storyId}-${chapterId}`;
                let btn = document.getElementById(btnId);
                if (btn) {
                    btn.disabled = true;
                    btn.innerText = "Starting...";
                }
                let detailsBtn = document.getElementById("details-run-btn");
                if (detailsBtn) {
                    detailsBtn.disabled = true;
                    detailsBtn.innerText = "Starting...";
                }
                
                let url = `/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/run`;
                try {
                    let res = await fetch(url, { method: "POST" });
                    if (!res.ok) {
                        let err = await res.json();
                        alert(err.detail || "Failed to start run");
                    }
                    selectChapter(storyId, chapterId, chapterId);
                } catch(e) {
                    alert("Error: " + e);
                }
            }

            async function pollChapterStatus(storyId, chapterId) {
                if (currentStory !== storyId || currentChapter !== chapterId) return;
                try {
                    let res = await fetch(`/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/status`);
                    let status = await res.json();
                    
                    let banner = document.getElementById("status-banner");
                    banner.innerText = status.status || "Idle";
                    banner.className = "status-banner " + (status.status === 'completed' ? 'completed' : (status.status !== 'idle' ? 'processing' : ''));

                    let detailsBtn = document.getElementById("details-run-btn");
                    if (detailsBtn) {
                        let isRunning = (status.status !== 'idle' && status.status !== 'completed');
                        detailsBtn.innerText = isRunning ? "Restart Pipeline" : "Run";
                        detailsBtn.disabled = false;
                    }

                    document.getElementById("current-project-desc").innerText = "Pipeline step: " + (status.status || "idle");

                    let progressFill = document.getElementById("progress-bar");
                    let pctText = document.getElementById("progress-percentage");
                    let fracText = document.getElementById("progress-fraction");
                    
                    let total = status.total_fragments || 0;
                    let current = status.current_fragment || 0;
                    
                    let percentage = total > 0 ? Math.round((current / total) * 100) : 0;
                    if (status.status === "completed") {
                        percentage = 100;
                    }
                    progressFill.style.width = percentage + "%";
                    pctText.innerText = percentage + "%";
                    fracText.innerText = current + " / " + total + " Fragments";

                    // Dynamic fragments
                    let grid = document.getElementById("fragments-grid");
                    grid.innerHTML = "";
                    
                    for (let i = 0; i < total; i++) {
                        let card = document.createElement("div");
                        card.className = "fragment-card";
                        if (i === current) card.classList.add("active");
                        
                        let voiceDone = false;
                        let imgDone = false;
                        let clipDone = false;
                        
                        let voiceActive = false;
                        let imgActive = false;
                        let clipActive = false;

                        let runStatus = status.status;

                        if (runStatus === "generating_audio") {
                            voiceDone = i < current;
                            voiceActive = (i === current);
                        } else if (runStatus === "generating_images") {
                            voiceDone = true;
                            imgDone = i < current;
                            imgActive = (i === current);
                        } else if (runStatus === "compiling_clips") {
                            voiceDone = true;
                            imgDone = true;
                            clipDone = i < current;
                            clipActive = (i === current);
                        } else if (runStatus === "assembling_final_video" || runStatus === "completed") {
                            voiceDone = true;
                            imgDone = true;
                            clipDone = true;
                        }

                        card.innerHTML = `
                            <h4>Frag #${i}</h4>
                            <div class="step-indicator">
                                <span class="step-dot ${voiceDone ? 'done' : (voiceActive ? 'active' : '')}" title="TTS (Voice)"></span>
                                <span class="step-dot ${imgDone ? 'done' : (imgActive ? 'active' : '')}" title="Image Gen"></span>
                                <span class="step-dot ${clipDone ? 'done' : (clipActive ? 'active' : '')}" title="Stitch Clip"></span>
                            </div>
                        `;
                        grid.appendChild(card);
                    }

                    // Video Output Preview
                    let videoContainer = document.getElementById("video-preview-container");
                    let videoElement = document.getElementById("final-video");
                    if (status.status === "completed" || status.status === "idle") {
                        let videoUrl = `/media/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/final.mp4`;
                        let check = await fetch(videoUrl, { method: "HEAD" });
                        if (check.ok) {
                            videoContainer.style.display = "block";
                            let downloadBtn = document.getElementById("download-video-btn");
                            if (downloadBtn) {
                                downloadBtn.href = videoUrl;
                            }
                            if (videoElement.src !== window.location.origin + videoUrl) {
                                videoElement.src = videoUrl;
                                videoElement.load();
                            }
                        } else {
                            videoContainer.style.display = "none";
                        }
                    } else {
                        videoContainer.style.display = "none";
                    }

                } catch(e) {}
            }

            function copyCommand(id) {
                let text = document.getElementById(id).innerText;
                navigator.clipboard.writeText(text);
                
                let btn = document.querySelector(`button[onclick="copyCommand('${id}')"]`);
                let origText = btn.innerText;
                btn.innerText = "Copied!";
                btn.style.background = "var(--success)";
                setTimeout(() => {
                    btn.innerText = origText;
                    btn.style.background = "rgba(255,255,255,0.1)";
                }, 1500);
            }

            // Fill all placeholders with the current origin
            document.querySelectorAll(".server-origin-placeholder").forEach(el => {
                el.innerText = window.location.origin;
            });

            setInterval(updateAgentStatus, 3000);
            setInterval(loadProjects, 3000);
            updateAgentStatus();
            loadProjects();

            function openMusicDialog() {
                document.getElementById("music-project-name").value = "";
                document.getElementById("music-path").value = "";
                document.getElementById("music-file").value = "";
                document.getElementById("music-dialog").showModal();
            }

            function closeMusicDialog() {
                document.getElementById("music-dialog").close();
            }

             async function submitMusicProject(event) {
                event.preventDefault();
                let projectName = document.getElementById("music-project-name").value.trim();
                let pathInput = document.getElementById("music-path");
                let musicFile = document.getElementById("music-file").files[0];
                let artStyle = document.getElementById("art-style-music").value;
                let useWatermark = document.getElementById("music-use-watermark").checked;
                let useSubtitles = document.getElementById("music-use-subtitles").checked;
                let useWhisper = document.getElementById("music-use-whisper").checked;
                let forceRerun = document.getElementById("music-force-rerun") ? document.getElementById("music-force-rerun").checked : false;
                
                let localPath = pathInput.value.trim();
                let isUpload = musicFile && localPath.startsWith("Staged Upload:");

                if (!projectName) {
                    alert("Project Name is required!");
                    return;
                }
                if (!isUpload && !localPath) {
                    alert("Please select a music file via Browse or choose upload manual!");
                    return;
                }

                closeMusicDialog();

                let formData = new FormData();
                if (isUpload) {
                    formData.append("file", musicFile);
                }

                let detailsPanel = document.getElementById("details-panel");
                detailsPanel.style.display = "block";
                document.getElementById("current-project-title").innerText = isUpload ? `Uploading: ${projectName}` : `Starting Music: ${projectName}`;
                document.getElementById("status-banner").innerText = isUpload ? "Uploading..." : "Starting...";
                
                let url = "/v1/projects/music?project_name=" + encodeURIComponent(projectName);
                if (!isUpload) {
                    url += `&local_path=${encodeURIComponent(localPath)}`;
                }

                try {
                    let res = await fetch(url, {
                        method: "POST",
                        body: formData
                    });
                    if (res.ok) {
                        let runRes = await fetch(`/v1/projects/music/${encodeURIComponent(projectName)}/run`, {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json"
                            },
                            body: JSON.stringify({
                                art_style: artStyle,
                                use_watermark: useWatermark,
                                use_subtitles: useSubtitles,
                                use_whisper: useWhisper,
                                force_rerun: forceRerun
                            })
                        });
                        if (runRes.ok) {
                            selectChapter("music", projectName, projectName);
                        } else {
                            let err = await runRes.json();
                            alert("Failed to start music pipeline: " + (err.detail || "Unknown error"));
                        }
                    } else {
                        let err = await res.json();
                        alert("Creation failed: " + (err.detail || "Unknown error"));
                    }
                } catch (e) {
                    alert("Error: " + e);
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(
        content=html_content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
