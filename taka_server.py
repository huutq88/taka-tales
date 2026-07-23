import asyncio
import json
import os
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
AGENT_VERSION = "0.3.0"

LORE_KEEPER_URL = os.environ.get("LORE_KEEPER_URL") or os.environ.get("LORE_KEEPER_API") or "http://lore-keeper:8080"
LORE_KEEPER_URL = LORE_KEEPER_URL.rstrip("/")

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
agents_by_workspace: Dict[str, WebSocket] = {}  # workspace_id -> websocket
agent_status: Dict[str, dict] = {}              # workspace_id -> status dict
project_jobs: Dict[str, dict] = {}              # project_name -> job state
pending_file_selects: Dict[str, dict] = {}
pending_agent_requests: Dict[str, dict] = {}

def get_workspace_id_from_request(request: Request) -> str:
    ws_id = request.headers.get("x-workspace-id") or request.query_params.get("workspace_id")
    if not ws_id or ws_id == "null" or ws_id == "undefined":
        ws_id = ""
    else:
        ws_id = ws_id.strip()
    if (not ws_id or ws_id not in agents_by_workspace) and len(agents_by_workspace) > 0:
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

# Serve output videos and media
@app.api_route("/media/{story_id}/{chapter_id}/{file_path:path}", methods=["GET", "HEAD"])
async def get_project_media(request: Request, story_id: str, chapter_id: str, file_path: str):
    base_dir = (PROJECTS_DIR / story_id / chapter_id).resolve()
    target_file = (base_dir / file_path).resolve()
    
    try:
        target_file.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
        
    found_local = None
    if target_file.exists() and target_file.is_file():
        found_local = target_file
    else:
        # Check local disk fallbacks for image extensions & final video
        p = pathlib.Path(file_path)
        if p.parent.name == "images" or "images/" in file_path:
            stem = p.stem
            parent = base_dir / p.parent
            for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                alt_img = parent / f"{stem}{ext}"
                if alt_img.exists() and alt_img.is_file():
                    found_local = alt_img
                    break
        if not found_local and file_path == "final.mp4":
            alt_video = base_dir / f"{story_id}_{chapter_id}.mp4"
            if alt_video.exists() and alt_video.is_file():
                found_local = alt_video

    # If file not found on server disk, tunnel request to connected WebSocket agent
    ws_id = get_workspace_id_from_request(request)
    if not found_local:
        res = await tunnel_request_to_agent("get_media_file_request", {"story_id": story_id, "chapter_id": chapter_id, "file_path": file_path}, workspace_id=ws_id, timeout=15.0)
        if res and isinstance(res, dict) and res.get("exists") and res.get("content_b64"):
            import base64
            content_bytes = base64.b64decode(res["content_b64"])
            content_type = res.get("content_type", "application/octet-stream")
            
            # Pure in-memory streaming - DO NOT WRITE OR SAVE ANY FILE TO RAILWAY DISK!
            file_size = len(content_bytes)
            if request.method == "HEAD":
                return Response(status_code=200, headers={
                    "Content-Type": content_type,
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
                        end = min(end, file_size - 1)
                        length = end - start + 1
                        chunk = content_bytes[start:start+length]
                        return Response(
                            content=chunk,
                            status_code=206,
                            headers={
                                "Content-Type": content_type,
                                "Content-Range": f"bytes {start}-{end}/{file_size}",
                                "Content-Length": str(length),
                                "Accept-Ranges": "bytes"
                            }
                        )
                except Exception:
                    pass

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
        agents_by_workspace.pop(workspace_id, None)
        agent_status.pop(workspace_id, None)

@app.get("/v1/agent/workspaces")
async def list_active_workspaces():
    return {
        "active_workspaces": list(agents_by_workspace.keys()),
        "agent_status": agent_status
    }

@app.get("/v1/agent/status")
async def get_agent_status(request: Request):
    ws_id = get_workspace_id_from_request(request)
    if (not ws_id or ws_id not in agents_by_workspace) and len(agents_by_workspace) > 0:
        ws_id = list(agents_by_workspace.keys())[0]
    agent_ws = agents_by_workspace.get(ws_id)
    connected = agent_ws is not None
    st = agent_status.get(ws_id, {})
    agent_ver = st.get("agent_version")
    needs_update = (agent_ver != AGENT_VERSION) if connected else False
    return JSONResponse(
        content={
            "connected": connected,
            "workspace_id": ws_id,
            "active_workspaces": list(agents_by_workspace.keys()),
            "agents": {ws_id: st} if st else {},
            "server_version": AGENT_VERSION,
            "needs_update": needs_update,
            "agent_version": agent_ver
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
    
    script_content = f"""
$SERVER_URL = "{server_url}"
$WORKSPACE_ID = "{workspace_id}"
if (-not $WORKSPACE_ID -or $WORKSPACE_ID -eq "default_workspace") {{
    $uName = $env:USERNAME.ToLower() -replace '[^a-zA-Z0-9_-]', ''
    if (-not $uName) {{ $uName = "user" }}
    $hName = $env:COMPUTERNAME
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $hashBytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$hName-$uName"))
    $devHash = ([BitConverter]::ToString($hashBytes).Replace("-","").ToLower()).Substring(0, 6)
    $WORKSPACE_ID = "${{uName}}_${{devHash}}"
}}

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
if (-not (Test-Path "env\Scripts\python.exe")) {{
    & $PYTHON_EXE -m venv env
}}

$ENV_PYTHON = "$HOME\.taka-agent\env\Scripts\python.exe"
$ENV_PIP = "$HOME\.taka-agent\env\Scripts\pip.exe"

# 5. Install PyTorch and dependencies
Write-Host "[5/6] Installing dependencies..." -ForegroundColor Green
Write-Host "Installing PyTorch with CUDA support..." -ForegroundColor Yellow
& $ENV_PIP install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
& $ENV_PIP install -r requirements.txt
& $ENV_PIP install psycopg2-binary

# 6. Setup OmniVoice (Vietnamese Voice Cloning Tool)
Write-Host "[6/6] Pre-installing OmniVoice tool..." -ForegroundColor Green
if (-not (Test-Path "tools\OmniVoice")) {{
    New-Item -ItemType Directory -Force -Path "tools" | Out-Null
    if (Get-Command git -ErrorAction SilentlyContinue) {{
        Write-Host "Cloning OmniVoice repository via Git..." -ForegroundColor Yellow
        & git clone https://github.com/k2-fsa/OmniVoice tools/OmniVoice
    }} else {{
        Write-Host "Git not found. Downloading OmniVoice zip archive..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://github.com/k2-fsa/OmniVoice/archive/refs/heads/main.zip" -OutFile "tools\omnivoice.zip"
        Expand-Archive -Path "tools\omnivoice.zip" -DestinationPath "tools" -Force
        if (Test-Path "tools\OmniVoice-main") {{
            Rename-Item -Path "tools\OmniVoice-main" -NewName "OmniVoice" -Force
        }}
        Remove-Item -Path "tools\omnivoice.zip" -Force -ErrorAction SilentlyContinue
    }}

    if (Test-Path "tools\OmniVoice\requirements.txt") {{
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
Write-Host "🎉 Taka Agent Installation Complete!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Starting Taka Agent in the background..." -ForegroundColor Yellow
Start-Process -FilePath $ENV_PYTHON -ArgumentList "-u", "taka_agent.py" -WindowStyle Hidden -WorkingDirectory "$HOME\.taka-agent" -RedirectStandardOutput "$HOME\.taka-agent\agent.log" -RedirectStandardError "$HOME\.taka-agent\agent_err.log"
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

@app.delete("/v1/projects/{story_id}")
@app.delete("/v1/projects/{story_id}/{chapter_id}")
async def delete_project(request: Request, story_id: str, chapter_id: Optional[str] = None):
    clean_story = story_id.strip()
    if not clean_story or "/" in clean_story or ".." in clean_story:
        raise HTTPException(status_code=400, detail="Invalid story_id format")

    ws_id = get_workspace_id_from_request(request)
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        try:
            await tunnel_request_to_agent("delete_project_request", {
                "story_id": clean_story,
                "chapter_id": chapter_id
            }, workspace_id=ws_id, timeout=5.0)
        except Exception as e:
            print(f"[Server] Warning: delete_project_request to Agent failed: {e}")

    # Remove matching job state
    target_pattern = f"{clean_story}/{chapter_id}" if (chapter_id and chapter_id != "story") else clean_story
    keys_to_del = [k for k in project_jobs.keys() if k == target_pattern or k.startswith(f"{target_pattern}/") or k == clean_story or k.startswith(f"{clean_story}/")]
    for k in keys_to_del:
        project_jobs.pop(k, None)

    # Delete local folder on server
    if chapter_id and chapter_id != "story":
        chapter_dir = PROJECTS_DIR / clean_story / chapter_id
        if chapter_dir.exists():
            shutil.rmtree(chapter_dir)
        parent_dir = PROJECTS_DIR / clean_story
        if parent_dir.exists() and not any(p for p in parent_dir.iterdir() if not p.name.startswith(".")):
            shutil.rmtree(parent_dir)
    else:
        target_dir = PROJECTS_DIR / clean_story
        if target_dir.exists():
            shutil.rmtree(target_dir)

    print(f"[Server] Successfully deleted project directory for story_id={clean_story}, chapter_id={chapter_id}")
    return {"ok": True, "story_id": clean_story, "chapter_id": chapter_id}


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
async def list_projects(request: Request):
    ws_id = get_workspace_id_from_request(request)
    if (not ws_id or ws_id not in agents_by_workspace) and len(agents_by_workspace) > 0:
        ws_id = list(agents_by_workspace.keys())[0]
    stories = []
    story_ids = []
    agent_files = {}
    
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        res = await tunnel_request_to_agent("list_projects_request", {}, workspace_id=ws_id, timeout=5.0)
        if res:
            story_ids = res.get("story_folders", [])
            agent_files = res.get("local_files", {})
            print(f"[Server] Fetched project folders from Agent ({ws_id}): {story_ids}")
            
    if not agent_ws or not story_ids:
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

    music_chapters = []
    dao_ly_chapters = []
    
    for key, info in agent_files.items():
        if key.startswith("music/"):
            ch_id = key.split("/", 1)[1]
            ch_title = ch_id.replace("-", " ").replace("_", " ").title()
            job_key = f"music/{ch_id}"
            job_state = project_jobs.get(job_key, {"status": "idle"})
            if info.get("has_video") and job_state.get("status") == "idle":
                job_state["status"] = "completed"
            music_chapters.append({
                "id": ch_id,
                "story_id": "music",
                "title": ch_title,
                "has_story": info.get("has_story", False),
                "has_video": info.get("has_video", False),
                "status": job_state.get("status", "idle"),
                "progress": job_state,
                "is_music": True
            })
        elif key.startswith("dao_ly/") or key.startswith("dao_ly_"):
            parts = key.split("/")
            s_id = parts[0]
            ch_id = parts[1] if len(parts) > 1 else "story"
            clean_title = s_id.replace("dao_ly_", "").replace("-", " ").replace("_", " ").title()
            job_key = f"{s_id}/{ch_id}"
            job_state = project_jobs.get(job_key, {"status": "idle"})
            if info.get("has_video") and job_state.get("status") == "idle":
                job_state["status"] = "completed"
            dao_ly_chapters.append({
                "id": ch_id,
                "story_id": s_id,
                "title": clean_title,
                "has_story": info.get("has_story", False),
                "has_video": info.get("has_video", False),
                "status": job_state.get("status", "idle"),
                "progress": job_state,
                "is_dao_ly": True
            })

    if music_chapters:
        stories.append({
            "story_id": "music",
            "title": "🎵 Music Projects",
            "chapters": sorted(music_chapters, key=lambda x: x["id"])
        })
        
    if dao_ly_chapters:
        stories.append({
            "story_id": "dao_ly",
            "title": "☯️ Video Đạo Lý",
            "chapters": sorted(dao_ly_chapters, key=lambda x: x["id"])
        })

    for story_id in story_ids:
        if story_id == "music" or story_id == "dao_ly" or story_id.startswith("dao_ly_"):
            continue
            
        from fastapi.concurrency import run_in_threadpool
        db_chapters = await run_in_threadpool(fetch_story_chapters, story_id)
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
async def get_project_status(request: Request, story_id: str, chapter_id: str):
    ws_id = get_workspace_id_from_request(request)
    job_key = f"{story_id}/{chapter_id}"
    job_state = project_jobs.get(job_key, {"status": "idle"}).copy()
    
    # If active agent connected via WebSocket, query real-time chapter status & files from agent
    res = await tunnel_request_to_agent("get_chapter_status_request", {"story_id": story_id, "chapter_id": chapter_id}, workspace_id=ws_id, timeout=3.0)
    if res and isinstance(res, dict):
        for k, v in res.items():
            if v is not None:
                if k == "status" and v == "idle" and job_state.get("status") not in ("idle", "completed", "failed", None):
                    continue
                job_state[k] = v
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
    start_fragment: Optional[int] = 0
    limit_fragments: Optional[int] = 0

class RunPipelineRequest(BaseModel):
    voice_config: Optional[VoiceConfig] = None
    art_style: Optional[str] = None
    story_text: Optional[str] = None
    use_watermark: Optional[bool] = True
    use_subtitles: Optional[bool] = True
    use_whisper: Optional[bool] = False
    force_rerun: Optional[bool] = False

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
async def list_voices(request: Request):
    ws_id = get_workspace_id_from_request(request)
    voices_list = []
    agent_ws = agents_by_workspace.get(ws_id)
    if agent_ws:
        res = await tunnel_request_to_agent("list_voices_request", {}, workspace_id=ws_id, timeout=5.0)
        if res and "voices" in res:
            voices_list = res["voices"]
            print(f"[Server] Fetched voices list from Agent ({ws_id}): {[v['id'] for v in voices_list]}")
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
    raise HTTPException(status_code=404, detail="Voice profile not found")

@app.get("/v1/projects/{story_id}/{chapter_id}/fragments")
async def get_project_fragments(story_id: str, chapter_id: str):
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
    frag_len = 20
    try:
        frag_len = config.getint("TEXT_FRAGMENT", "FRAGMENT_LENGTH", fallback=20)
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
async def run_project_pipeline(request: Request, story_id: str, chapter_id: str, request_data: Optional[RunPipelineRequest] = None):
    ws_id = get_workspace_id_from_request(request)
    if (not ws_id or ws_id not in agents_by_workspace) and len(agents_by_workspace) > 0:
        ws_id = list(agents_by_workspace.keys())[0]
    agent_ws = agents_by_workspace.get(ws_id)
    if not agent_ws:
        raise HTTPException(status_code=400, detail=f"No active Taka-Agent connected for workspace '{ws_id}'. Please start taka-agent on your computer.")
    
    project_dir = PROJECTS_DIR / story_id / chapter_id
    project_dir.mkdir(parents=True, exist_ok=True)
    story_file = project_dir / "story.txt"

    content = ""
    # Fetch content from Lore-Keeper or use provided story_text
    if story_id != "music":
        if request_data and request_data.story_text and request_data.story_text.strip():
            content = request_data.story_text.strip()
            with open(story_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[Server] Successfully wrote custom story_text to {story_file}")
        else:
            try:
                print(f"[Server] Fetching story content for chapter_id={chapter_id} from Lore-Keeper...")
                from fastapi.concurrency import run_in_threadpool
                content = await run_in_threadpool(fetch_chapter_content, chapter_id)
                with open(story_file, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"[Server] Successfully wrote story content to {story_file}")
            except Exception as e:
                print(f"[Server] Warning: Lore-Keeper fetch failed: {e}")

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

    print(f"[Server] Prepared voice config payload to agent ({ws_id}): { {k: (v[:30]+'...' if isinstance(v, str) and len(v) > 30 else v) for k, v in voice_payload.items()} }")
    # Send trigger message to target workspace agent
    trigger_message = {
        "type": "run_pipeline",
        "payload": {
            "project_name": f"{story_id}_{chapter_id}",
            "project_path": str(project_dir),
            "voice_config": voice_payload if voice_payload else None,
            "pipeline_type": "music" if story_id == "music" else ("dao_ly" if (story_id == "dao_ly" or story_id.startswith("dao_ly_")) else "story"),
            "art_style": request_data.art_style if request_data else None,
            "use_watermark": request_data.use_watermark if request_data else True,
            "use_subtitles": request_data.use_subtitles if request_data else True,
            "use_whisper": request_data.use_whisper if request_data else False,
            "force_rerun": request_data.force_rerun if request_data else False,
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
            #nav-dao-ly:hover, #nav-dao-ly.active {
                color: #f59e0b !important;
                text-shadow: 0 0 10px rgba(245, 158, 11, 0.5) !important;
            }

            .step-btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 28px;
                height: 28px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--border);
                font-size: 0.85rem;
                cursor: not-allowed;
                opacity: 0.3;
                transition: all 0.2s ease;
                user-select: none;
            }

            .step-btn.active {
                opacity: 1;
                cursor: pointer;
                background: linear-gradient(135deg, rgba(139, 92, 246, 0.45), rgba(59, 130, 246, 0.45));
                border: 1px solid rgba(167, 139, 250, 0.9);
                box-shadow: 0 0 10px rgba(139, 92, 246, 0.6), 0 0 4px rgba(255, 255, 255, 0.4);
                filter: brightness(1.25);
            }

            .step-btn.active:hover {
                background: linear-gradient(135deg, #8b5cf6, #6366f1);
                border-color: #a78bfa;
                transform: scale(1.25);
                box-shadow: 0 0 15px rgba(139, 92, 246, 0.9), 0 0 8px rgba(255, 255, 255, 0.8);
                filter: brightness(1.4);
            }

            .step-btn.running {
                opacity: 1;
                cursor: wait;
                border-color: var(--warning);
                background: rgba(245, 158, 11, 0.1);
                animation: pulse-border 1.5s infinite ease-in-out;
            }

            @keyframes pulse-border {
                0% { border-color: rgba(245, 158, 11, 0.3); box-shadow: 0 0 2px rgba(245, 158, 11, 0.2); }
                50% { border-color: rgba(245, 158, 11, 1); box-shadow: 0 0 8px rgba(245, 158, 11, 0.5); }
                100% { border-color: rgba(245, 158, 11, 0.3); box-shadow: 0 0 2px rgba(245, 158, 11, 0.2); }
            }

            /* Preview Modal Glassmorphism */
            .preview-modal {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.6);
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                z-index: 10000;
                align-items: center;
                justify-content: center;
            }

            .preview-modal-content {
                background: rgba(30, 30, 40, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 16px;
                padding: 2rem;
                max-width: 800px;
                width: 90%;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
                position: relative;
                text-align: center;
            }

            .preview-modal-close {
                position: absolute;
                top: 1rem;
                right: 1.2rem;
                font-size: 1.5rem;
                cursor: pointer;
                color: var(--text-muted);
                transition: color 0.2s ease;
            }

            .preview-modal-close:hover {
                color: var(--danger);
            }

            .preview-media-container {
                margin-top: 1.5rem;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
            }

            .preview-media-container video {
                max-width: 100%;
                max-height: 60vh;
                border-radius: 8px;
                border: 1px solid var(--border);
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }

            .preview-media-container img {
                max-width: 100%;
                max-height: 60vh;
                border-radius: 8px;
                border: 1px solid var(--border);
                object-fit: contain;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }

            .preview-media-container audio {
                width: 100%;
                max-width: 500px;
                margin-top: 1rem;
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
                <a id="nav-dao-ly" onclick="showPage('dao-ly')" style="display: flex; align-items: center; gap: 0.3rem;" title="Tạo video Đạo Lý 1-Click">☯️ Đạo Lý</a>
                <a id="nav-voices" onclick="showPage('voices')">Voices</a>
                <a id="nav-music" onclick="showPage('music')">Music</a>
            </nav>
            <div style="display: flex; align-items: center; gap: 1rem;">
                <div id="agent-badge" class="agent-badge">
                    <span class="badge-dot"></span>
                    <span id="agent-text">Agent Offline</span>
                </div>
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
                        <div style="display: flex; align-items: center; gap: 0.8rem;">
                            <span id="status-banner" class="status-banner">Idle</span>
                            <button id="details-run-btn" class="run-btn" style="padding: 0.4rem 1rem; font-size: 0.85rem;">Run</button>
                            <button id="details-delete-btn" onclick="deleteCurrentProject()" style="padding: 0.4rem 0.9rem; font-size: 0.85rem; background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.4); color: #ef4444; border-radius: 6px; cursor: pointer; font-weight: 600; transition: all 0.2s ease;">🗑️ Xóa dự án</button>
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

                    <div id="video-preview-container" class="video-preview" style="display: none; margin-bottom: 2rem; background: var(--surface-hover); padding: 1.25rem; border-radius: 12px; border: 1px solid var(--border);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                            <h3 style="margin: 0; font-size: 1.1rem; color: var(--primary-light);">🎬 Final Output Video</h3>
                            <a id="download-video-btn" href="" download class="btn-submit" style="font-size: 0.8rem; padding: 0.4rem 0.9rem; text-decoration: none; display: inline-flex; align-items: center; gap: 0.4rem; border-radius: 6px; font-weight: 600;">
                                📥 Tải Video
                            </a>
                        </div>
                        <div style="display: flex; justify-content: center;">
                            <video id="final-video" controls style="width: 100%; max-width: 280px; aspect-ratio: 9 / 16; border-radius: 10px; border: 1px solid var(--border); background: #000; box-shadow: 0 8px 24px rgba(0,0,0,0.5); object-fit: contain;">
                                Your browser does not support the video tag.
                            </video>
                        </div>
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

        <!-- Đạo Lý Studio Dialog -->
        <dialog id="dao-ly-dialog" style="max-width: 680px; width: 90%;">
            <h3 style="display:flex; justify-content:space-between; align-items:center; margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem; color: #f59e0b;">
                <span>☯️ Đạo Lý — Tạo Video Triết Lý 1-Click</span>
                <span style="font-size:0.8rem; opacity:0.6; color: var(--text-muted);">Shorts / Reels Generator</span>
            </h3>
            <form id="dao-ly-form" onsubmit="submitDaoLyProject(event)">
                <div class="form-group" style="margin-top: 1rem;">
                    <label for="dao-ly-sample-select" style="font-weight: 600; color: #a78bfa;">Chọn Kịch Bản Mẫu (Hoặc Tự Nhập Bằng Tay):</label>
                    <select id="dao-ly-sample-select" onchange="onSelectDaoLySample(this.value)" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; margin-bottom: 0.5rem;">
                        <option value="custom">-- Kịch bản Tùy Chỉnh (Tự nhập tiêu đề & nội dung bên dưới) --</option>
                        <option value="s1">📜 Kịch bản 1: Túi Tiền Và Tâm Hồn (Tài chính & Trí tuệ)</option>
                        <option value="s2">📜 Kịch bản 2: Nhìn Thấu Lòng Người (Ứng xử & Bản chất)</option>
                        <option value="s3">📜 Kịch bản 3: Bản Lĩnh Và Cơn Giận (Kiểm soát cảm xúc)</option>
                        <option value="s4">📜 Kịch bản 4: Sự Buông Bỏ Bình Yên (Tĩnh tâm & Quá khứ)</option>
                        <option value="s5">📜 Kịch bản 5: Sự Im Lặng Trưởng Thành (Thâm trầm & Nội tâm)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="dao-ly-title" style="font-weight: 600; color: #f59e0b;">1. Tiêu đề Video / Tên Kịch Bản:</label>
                    <input type="text" id="dao-ly-title" required placeholder="Nhập tiêu đề video (e.g. Sự Im Lặng Của Người Trưởng Thành)..." style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.65rem; border-radius: 6px; outline: none; font-size: 0.95rem; font-weight: 600;">
                </div>
                <div class="form-group">
                    <label for="dao-ly-story-text" style="font-weight: 600; color: #10b981;">2. Nội dung Kịch bản Đọc (Plain Text cho TTS):</label>
                    <textarea id="dao-ly-story-text" rows="6" required placeholder="Nhập văn bản kịch bản đọc để chuyển thành giọng nói tại đây..." style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none; font-family: inherit; font-size: 0.9rem; resize: vertical; line-height: 1.5;"></textarea>
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                    <div class="form-group">
                        <label for="dao-ly-voice" style="font-weight: 600;">Giọng đọc Đạo Lý:</label>
                        <select id="dao-ly-voice" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none;">
                            <option value="nam-dao-ly">👨 Nam Đạo Lý (OmniVoice - nam-dao-ly)</option>
                            <option value="nu-doc-truyen">👩 Nữ Đọc Truyện (OmniVoice - nu-doc-truyen)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="dao-ly-art-style" style="font-weight: 600;">Phong cách vẽ ảnh AI:</label>
                        <select id="dao-ly-art-style" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none;">
                            <option value="thuy_mac_blackwhite">☯️ Thủy mặc Đen - Trắng (Khuyên dùng)</option>
                            <option value="thuy_mac">🖌️ Thủy mặc Đen - Xám mờ sương</option>
                            <option value="woodblock">🪵 Mộc bản khắc gỗ trắng đen</option>
                            <option value="watercolor">🎨 Tranh màu nước hoài niệm</option>
                        </select>
                    </div>
                </div>
                <div class="form-group" style="margin-top: 0.5rem;">
                    <label for="dao-ly-aspect" style="font-weight: 600;">Tỷ lệ khung hình:</label>
                    <select id="dao-ly-aspect" style="width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 0.6rem; border-radius: 6px; outline: none;">
                        <option value="vertical">📱 Video Dọc (Shorts / TikTok / Reels 9:16)</option>
                        <option value="horizontal">💻 Video Ngang (YouTube 16:9)</option>
                    </select>
                </div>

                <div class="dialog-actions" style="margin-top: 1.5rem;">
                    <button type="button" class="btn-cancel" onclick="closeDaoLyStudioModal()">Hủy</button>
                    <button type="submit" id="dao-ly-submit-btn" class="btn-submit" style="background: linear-gradient(135deg, #f59e0b, #d97706); border: none; font-weight: bold; font-size: 0.95rem; color: #fff;">🚀 Khởi Tạo & Render Video Đạo Lý</button>
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

            function getWorkspaceId() {
                let wsId = localStorage.getItem("taka_workspace_id");
                if (!wsId || wsId === "null" || wsId === "undefined") {
                    wsId = "";
                }
                return wsId;
            }

            function setWorkspaceId(wsId) {
                if (wsId) {
                    localStorage.setItem("taka_workspace_id", wsId.trim());
                    let el = document.getElementById("workspace-id-text");
                    if (el) el.innerText = wsId.trim();
                    fetchStories();
                    if (typeof fetchVoicesPage === "function") fetchVoicesPage();
                    updateAgentStatus();
                }
            }

            function changeWorkspacePrompt() {
                let current = getWorkspaceId();
                let newWs = prompt("Không gian làm việc (Workspace ID):", current || "huutq");
                if (newWs && newWs.trim()) {
                    setWorkspaceId(newWs.trim());
                }
            }

            // Intercept window.fetch to automatically append X-Workspace-ID header
            const originalFetch = window.fetch;
            window.fetch = function(url, options) {
                options = options || {};
                options.headers = options.headers || {};
                let wsId = getWorkspaceId();
                let urlStr = typeof url === 'string' ? url : (url ? url.toString() : '');
                if (wsId && !urlStr.includes("/v1/agent/status")) {
                    if (options.headers instanceof Headers) {
                        options.headers.set("X-Workspace-ID", wsId);
                    } else if (Array.isArray(options.headers)) {
                        options.headers.push(["X-Workspace-ID", wsId]);
                    } else {
                        options.headers["X-Workspace-ID"] = wsId;
                    }
                }
                return originalFetch.call(this, url, options);
            };

            let currentStory = "";
            let currentChapter = "";
            let timerId = null;
            let storyFragments = [];

            async function updateAgentStatus() {
                try {
                    let res = await fetch("/v1/agent/status?_t=" + Date.now());
                    let data = await res.json();
                    let badge = document.getElementById("agent-badge");
                    let text = document.getElementById("agent-text");
                    let welcomeStatus = document.getElementById("welcome-agent-status");
                    let welcomeText = document.getElementById("welcome-status-text");
                    let welcomeDot = document.getElementById("welcome-status-dot");

                    // Auto-select online workspace if none saved or current is offline
                    if (data.active_workspaces && data.active_workspaces.length === 1) {
                        let autoWs = data.active_workspaces[0];
                        let currentWs = localStorage.getItem("taka_workspace_id");
                        if (currentWs !== autoWs) {
                            localStorage.setItem("taka_workspace_id", autoWs);
                            let wsEl = document.getElementById("workspace-id-text");
                            if (wsEl) wsEl.innerText = autoWs;
                            fetchStories();
                            // Re-fetch status with updated workspace header
                            if (!data.connected) {
                                return updateAgentStatus();
                            }
                        }
                    }

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
                        } else if (s.story_id === "dao_ly") {
                            header.innerHTML = `☯️ Video Đạo Lý`;
                            header.style.color = "#f59e0b";
                        } else {
                            header.innerHTML = `📖 Story: ${s.story_id}`;
                        }
                        sec.appendChild(header);
                        
                        let chList = document.createElement("div");
                        chList.className = "chapter-list";
                        
                        s.chapters.forEach(c => {
                            let targetStoryId = c.story_id || s.story_id;
                            let displayTitle = c.title;
                            if (s.story_id !== "music" && s.story_id !== "dao_ly") {
                                let idx = "";
                                let match = c.id.match(/chuong[-_](\d+)/i) || c.id.match(/chapter[-_](\d+)/i) || c.id.match(/(\d+)/);
                                if (match) {
                                    idx = match[1];
                                }
                                let cleanTitle = c.title.replace(/^(Chương|chuong|chapter)\s*\d+[\s-:]*/i, "").trim();
                                if (idx !== "") {
                                    displayTitle = `Chương ${idx}` + (cleanTitle ? `: ${cleanTitle}` : "");
                                } else {
                                    displayTitle = cleanTitle || c.title;
                                }
                            }

                            let activeClass = (targetStoryId === currentStory && c.id === currentChapter) ? "active" : "";
                            let item = document.createElement("div");
                            item.className = "chapter-item " + activeClass;
                            item.onclick = () => selectChapter(targetStoryId, c.id, displayTitle);
                            
                            let btnId = `btn-${s.story_id}-${c.id}`;
                            let isRunning = (c.status !== 'idle' && c.status !== 'completed');
                            
                            item.innerHTML = `
                                <div class="chapter-info" style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                                    <div style="flex: 1; overflow: hidden; text-overflow: ellipsis;">
                                        <h4>${displayTitle}</h4>
                                        <p>${c.has_video ? "🎬 Video completed" : "No video output yet"}</p>
                                    </div>
                                    <button class="delete-item-btn" title="Xóa dự án này" onclick="event.stopPropagation(); deleteProject('${targetStoryId}', '${c.id}');" style="background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); color: #ef4444; padding: 0.25rem 0.5rem; border-radius: 5px; font-size: 0.75rem; cursor: pointer; font-weight: 600; margin-left: 0.5rem;">🗑️</button>
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
                let vc = document.getElementById("video-preview-container");
                if (vc) vc.dataset.loadedUrl = "";
                
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
                    } else if (storyId.startsWith("dao_ly_")) {
                        runBtn.onclick = (event) => openDaoLyStudioModal(storyId);
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
                        storyFragments = Array.isArray(frags) ? frags : [];
                        renderStoryFragments();
                    })
                    .catch(err => {
                        storyFragments = [];
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
                
                if (!Array.isArray(storyFragments)) storyFragments = [];
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
                // Close open dialogs if switching tabs
                ['dao-ly-dialog', 'music-dialog', 'voice-config-dialog'].forEach(id => {
                    let d = document.getElementById(id);
                    if (d) {
                        try { d.close(); } catch(e) {}
                    }
                });

                // Clear active from all navbar items
                document.querySelectorAll('.header-menu a').forEach(a => a.classList.remove('active'));

                if (pageId === 'home') {
                    let homeBtn = document.getElementById('nav-home');
                    if (homeBtn) homeBtn.classList.add('active');
                    document.getElementById("main-grid").style.display = "grid";
                    document.getElementById("voices-page").style.display = "none";
                } else if (pageId === 'voices') {
                    let voicesBtn = document.getElementById('nav-voices');
                    if (voicesBtn) voicesBtn.classList.add('active');
                    document.getElementById("main-grid").style.display = "none";
                    document.getElementById("voices-page").style.display = "block";
                    loadVoicesList();
                } else if (pageId === 'dao-ly') {
                    let daoLyBtn = document.getElementById('nav-dao-ly');
                    if (daoLyBtn) daoLyBtn.classList.add('active');
                    document.getElementById("main-grid").style.display = "grid";
                    document.getElementById("voices-page").style.display = "none";
                    openDaoLyStudioModal();
                } else if (pageId === 'music') {
                    let musicBtn = document.getElementById('nav-music');
                    if (musicBtn) musicBtn.classList.add('active');
                    document.getElementById("main-grid").style.display = "grid";
                    document.getElementById("voices-page").style.display = "none";
                    openMusicDialog();
                }
            }

            async function loadVoicesDropdown() {
                try {
                    let res = await fetch("/v1/voices");
                    let voices = await res.json();
                    
                    // 1. Populate #vc-voice-id
                    let selectVc = document.getElementById("vc-voice-id");
                    if (selectVc) {
                        let currentValue = selectVc.value;
                        selectVc.innerHTML = '<option value="">-- Select Voice Profile --</option>';
                        voices.forEach(v => {
                            let opt = document.createElement("option");
                            opt.value = v.id;
                            opt.textContent = `${v.name} (${v.has_audio ? "Ref Audio Present" : "Missing Audio"})`;
                            selectVc.appendChild(opt);
                        });
                        if (currentValue) selectVc.value = currentValue;
                    }

                    // 2. Populate #dao-ly-voice dynamically
                    let selectDaoLy = document.getElementById("dao-ly-voice");
                    if (selectDaoLy) {
                        let currentValue = selectDaoLy.value;
                        selectDaoLy.innerHTML = "";
                        
                        voices.forEach(v => {
                            let opt = document.createElement("option");
                            opt.value = v.id;
                            let emoji = "🎙️";
                            if (v.id.includes("nam")) emoji = "👨";
                            else if (v.id.includes("nu")) emoji = "👩";
                            opt.textContent = `${emoji} ${v.id} (OmniVoice - ${v.name || v.id})`;
                            selectDaoLy.appendChild(opt);
                        });

                        if (currentValue) {
                            selectDaoLy.value = currentValue;
                        }
                    }
                } catch (e) {
                    console.error("Failed to load voices dropdown: " + e);
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
                } catch (e) {
                    alert("Failed to delete voice profile: " + e);
                }
            }

            async function deleteProject(storyId, chapterId) {
                let targetStory = storyId || currentStory;
                let targetChapter = chapterId || currentChapter;
                if (!targetStory) return;

                let displayLabel = targetStory;
                if (targetChapter && targetChapter !== "story") {
                    displayLabel += ` (${targetChapter})`;
                }
                if (!confirm(`Bạn có chắc chắn muốn XÓA DỰ ÁN "${displayLabel}" không?\n\nHành động này sẽ DỪNG TOÀN BỘ tiến trình pipeline đang chạy và XÓA TOÀN BỘ dữ liệu dự án trên đĩa cứng!`)) {
                    return;
                }

                try {
                    let url = `/v1/projects/${encodeURIComponent(targetStory)}`;
                    if (targetChapter && targetChapter !== "story") {
                        url += `/${encodeURIComponent(targetChapter)}`;
                    }
                    let res = await fetch(url, { method: "DELETE" });
                    if (res.ok) {
                        if (!currentStory || currentStory === targetStory || currentStory.startsWith(targetStory)) {
                            currentStory = "";
                            currentChapter = "";
                            let mainView = document.getElementById("main-details-view");
                            if (mainView) mainView.style.display = "none";
                            let emptyView = document.getElementById("empty-details-view");
                            if (emptyView) emptyView.style.display = "block";
                        }
                        await loadProjects();
                    } else {
                        let txt = await res.text();
                        let detail = txt;
                        try { detail = JSON.parse(txt).detail || txt; } catch(_) {}
                        alert("Lỗi xóa dự án: " + detail);
                    }
                } catch(e) {
                    alert("Lỗi kết nối khi xóa dự án: " + e);
                }
            }

            function deleteCurrentProject() {
                deleteProject(currentStory, currentChapter);
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

                    if (status.status === "completed" && timerId) {
                        clearInterval(timerId);
                        timerId = null;
                    }
                    
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
                    let buildCards = (grid.children.length !== total);
                    if (buildCards) {
                        grid.innerHTML = "";
                    }
                    
                    for (let i = 0; i < total; i++) {
                        let card = buildCards ? document.createElement("div") : grid.children[i];
                        if (buildCards) {
                            card.className = "fragment-card";
                        }
                        if (i === current) card.classList.add("active");
                        else card.classList.remove("active");
                        
                        let voiceActive = false;
                        let imgActive = false;
                        let clipActive = false;

                        let runStatus = status.status;

                        if (runStatus === "generating_audio") {
                            voiceActive = (i === current);
                        } else if (runStatus === "generating_images") {
                            imgActive = (i === current);
                        } else if (runStatus === "compiling_clips") {
                            clipActive = (i === current);
                        }

                        let audioUrlWav = `${LOCAL_MEDIA_ORIGIN}/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/audio/voiceover${i}.wav`;
                        let audioUrlMp3 = `${LOCAL_MEDIA_ORIGIN}/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/audio/voiceover${i}.mp3`;
                        let imageUrl = `${LOCAL_MEDIA_ORIGIN}/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/images/image${i}.png`;
                        let videoUrl = `${LOCAL_MEDIA_ORIGIN}/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/videos/video${i}.mp4`;

                        if (buildCards) {
                            card.innerHTML = `
                                <h4>Frag #${i}</h4>
                                <div class="step-indicator">
                                    <span class="step-btn ${voiceActive ? 'running' : ''}" id="preview-audio-${i}" title="Play Audio Voiceover">🎵</span>
                                    <span class="step-btn ${imgActive ? 'running' : ''}" id="preview-image-${i}" title="Show Generated Image">🖼️</span>
                                    <span class="step-btn ${clipActive ? 'running' : ''}" id="preview-video-${i}" title="Play Video Clip">🎥</span>
                                </div>
                            `;
                            grid.appendChild(card);
                        }

                        getMediaExists(audioUrlWav).then(existsWav => {
                            let btn = document.getElementById(`preview-audio-${i}`);
                            if (btn) {
                                if (existsWav) {
                                    btn.classList.add("active");
                                    btn.onclick = () => playAudioPreview(audioUrlWav, i);
                                } else {
                                    getMediaExists(audioUrlMp3).then(existsMp3 => {
                                        if (existsMp3) {
                                            btn.classList.add("active");
                                            btn.onclick = () => playAudioPreview(audioUrlMp3, i);
                                        } else if (!voiceActive) {
                                            btn.classList.remove("active");
                                            btn.classList.add("disabled");
                                        }
                                    });
                                }
                            }
                        });

                        getMediaExists(imageUrl).then(exists => {
                            let btn = document.getElementById(`preview-image-${i}`);
                            if (btn) {
                                if (exists) {
                                    btn.classList.add("active");
                                    btn.onclick = () => showImagePreview(imageUrl, i);
                                } else if (!imgActive) {
                                    btn.classList.remove("active");
                                    btn.classList.add("disabled");
                                }
                            }
                        });

                        getMediaExists(videoUrl).then(exists => {
                            let btn = document.getElementById(`preview-video-${i}`);
                            if (btn) {
                                if (exists) {
                                    btn.classList.add("active");
                                    btn.onclick = () => playVideoPreview(videoUrl, i);
                                } else if (!clipActive) {
                                    btn.classList.remove("active");
                                    btn.classList.add("disabled");
                                }
                            }
                        });
                    }

                    // Video Output Preview
                    let videoContainer = document.getElementById("video-preview-container");
                    let videoElement = document.getElementById("final-video");
                    if (status.status === "completed" || status.status === "idle") {
                        let videoUrl = `${LOCAL_MEDIA_ORIGIN}/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/final.mp4`;
                        if (videoContainer.dataset.loadedUrl !== videoUrl) {
                            let check = await getMediaExists(videoUrl);
                            if (check) {
                                videoContainer.dataset.loadedUrl = videoUrl;
                                videoContainer.style.display = "block";
                                let downloadBtn = document.getElementById("download-video-btn");
                                if (downloadBtn) {
                                    downloadBtn.href = videoUrl;
                                }
                                if (videoElement.src !== videoUrl) {
                                    videoElement.src = videoUrl;
                                    videoElement.load();
                                }
                            } else {
                                videoContainer.dataset.loadedUrl = "";
                                videoContainer.style.display = "none";
                            }
                        }
                    } else {
                        videoContainer.dataset.loadedUrl = "";
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

            const DAO_LY_SAMPLES = {
                s1: {
                    title: `Túi Tiền Và Tâm Hồn`,
                    text: `Kẻ nghèo nhất không phải người không có tiền, mà là người chỉ có tiền trong tay.

Khi bạn chỉ sống vì vật chất, sự tôn trọng người khác dành cho bạn cũng chỉ đắt giá bằng túi tiền của bạn mà thôi. Đồng tiền có thể mua được sự nịnh hót tạm thời, nhưng không bao giờ mua được tấm lòng trung thành. Người có trí tuệ coi tiền là công cụ để phụng sự cuộc sống, còn kẻ dại khờ coi tiền là thước đo duy nhất để đánh giá nhân cách.

Nếu một ngày đồng tiền mất đi giá trị, thứ còn lại duy nhất chính là phẩm giá và sự tử tế của bạn. Đừng đánh đổi sức khỏe, gia đình và sự bình yên để chạy theo những con số vô hồn. Hãy nhớ rằng, của cải vật chất khi chết đi không ai mang theo được, chỉ có giá trị bạn để lại cho đời mới là vĩnh cửu.

Cuộc sống này là một chặng đường dài, sự giàu có thật sự không nằm ở chiếc xe bạn đi hay ngôi nhà bạn ở, mà nằm ở bình yên trong tâm trí và sự ấm áp trong trái tim bạn. Kẻ tích góp tiền bạc mà bỏ quên tâm hồn thì chẳng khác nào người đi trong đêm tối ôm một bao vàng nặng nề nhưng không có lấy một ngọn đèn soi đường.

Hãy làm chủ đồng tiền, đừng để nó biến bạn thành nô lệ trong sự giàu có cô độc. Đừng để khi bước đến cuối cuộc đời, bạn mới nhận ra mình có rất nhiều tiền nhưng lại chẳng sở hữu bất kỳ điều gì thực sự có ý nghĩa. Hãy giữ cho mình một tâm hồn giàu có, một trái tim ấm áp trước khi tích lũy của cải.

Hãy nhớ rằng, tâm giàu thì đời an, trí sáng thì đường rộng. Đăng ký và theo dõi kênh để cùng rèn luyện tư duy và tích lũy tri thức mỗi ngày.`
                },
                s2: {
                    title: `Nhìn Thấu Lòng Người`,
                    text: `Đừng vội tin một người khi họ đối xử tốt với bạn lúc họ đang cần bạn.

Bản chất con người giống như một hồ nước sâu, chỉ khi gặp biến cố hoặc lợi ích bị đụng chạm, đáy nước mới hiện rõ. Người chân thành không dùng lời ngon tiếng ngọt để lấy lòng, mà lặng lẽ đứng bên bạn khi thế giới quay lưng. Kẻ dối trá thường rất vội vã với những lời hứa hẹn, còn người tử tế luôn bình thản chứng minh bằng thời gian.

Trải qua sóng gió, bạn mới biết ai là bạn, ai là bè. Sự tử tế thật sự không cần phô trương trên môi lưỡi, nó thể hiện ở sự tôn trọng và cách họ ứng xử khi bạn sa cơ thất thế. Nhìn thấu lòng người là một loại năng lực, nhưng không bóc phốt là một loại giáo dưỡng và bản lĩnh.

Trong cuộc đời, bạn sẽ gặp rất nhiều loại người: có người đến để dạy bạn bài học, có người đến để thử thách sự kiên nhẫn của bạn, và cũng có người xuất hiện chỉ để bạn nhận ra giá trị của sự chân thành. Đừng buồn vì bị phản bội hay dối lừa, bởi đó là cái giá để bạn trưởng thành và sâu sắc hơn.

Hãy học cách sống như một cây cổ thụ: rễ bám sâu vào lòng đất, mặc cho giông bão bên ngoài vẫn giữ sự vững chãi và bao dung. Chọn bạn mà chơi, chọn người mà tin, và quan trọng nhất là giữ cho tâm mình không bị vẩy bẩn bởi những lọc lừa của thế thái nhân tình.

Giữ sự tỉnh táo để nhìn đời, và giữ sự bao dung để sống yên bình giữa dòng đời biến động. Bấm đăng ký kênh để đón nhận thêm nhiều bài học triết lý đắt giá mỗi ngày.`
                },
                s3: {
                    title: `Bản Lĩnh Và Cơn Giận`,
                    text: `Mất kiểm soát cơn giận là cách nhanh nhất để bạn phá hủy thành quả của chính mình.

Một khoảnh khắc giận dữ có thể đốt cháy cả một rừng công sức bạn đã chắt chiu xây dựng bao năm. Người nông nổi dùng lời nói xỉa xói để chứng minh mình đúng, còn người bản lĩnh dùng sự im lặng để bao quát toàn cục. Kẻ thù lớn nhất không nằm ở bên ngoài, mà chính là sự bồng bồng và cái tôi ngông cuồng trong tâm trí bạn.

Nóng giận là bản năng của con người, nhưng kìm nén và chuyển hóa cơn giận mới là đỉnh cao của bản lĩnh. Khi giận dữ, mọi lời nói thốt ra đều mang độc tố làm tổn thương người khác và tự tàn phá chính năng lượng của bạn. Học cách lùi lại một bước, hít một hơi thật sâu để tâm trí lắng xuống trước khi đưa ra bất kỳ quyết định nào.

Người trí tuệ hiểu rằng giận dữ giống như việc bạn uống chất độc rồi mong chờ người khác ngộ độc. Sự trả thù tốt nhất không phải là ăn miếng trả miếng, mà là sống một cuộc đời thật rực rỡ và bình an. Khi bạn làm chủ được hơi thở và cảm xúc, không ai trên đời này có thể làm tổn thương bạn.

Làm chủ được cảm xúc, bạn mới có thể làm chủ được vận mệnh và gặt hái thành công bền vững. Bớt một chút tranh cãi đúng sai, bạn sẽ bớt đi hàng ngàn phiền lụy trong đời. 

Nhấn theo dõi kênh để rèn luyện sự bình thản và xây dựng bản lĩnh vững vàng mỗi ngày.`
                },
                s4: {
                    title: `Sự Buông Bỏ Bình Yên`,
                    text: `Thứ đang thiêu rụi cuộc đời bạn không phải là quá khứ, mà là sự hối tiếc vô ích.

Những gì đã xảy ra là điều bắt buộc phải xảy ra, dằn dằn bản thân hàng ngàn lần cũng không thể thay đổi được thực tại. Bạn không thể bắt đầu một chương mới nếu cứ mải miết đọc lại những trang sách cũ đầy nước mắt. Buông bỏ không phải là đầu hàng hay yếu đuối, mà là mỉm cười chấp nhận mọi thứ đã hoàn thành sứ mệnh của nó.

Cuộc sống quá ngắn để mang theo những gánh nặng tổn thương và sự oán hận từ quá khứ. Người làm bạn đau lòng đã bước tiếp từ lâu, sao bạn vẫn tự tay cứa thêm những vết thương vào tâm hồn mình mỗi đêm? Hãy học cách tha thứ cho người khác để cởi bỏ gông xích, và tha thứ cho chính bản thân mình của những năm tháng dại khờ.

Mọi cuộc gặp gỡ trong đời đều là vạn sự tùy duyên, người đến mang cho bạn niềm vui, người đi để lại cho bạn bài học. Đừng tiếc nuối những gì không thuộc về mình, bởi vì khi một cánh cửa đóng lại, vũ trụ sẽ mở ra những chân trời mới rộng lớn hơn.

Trả lại bình yên cho tâm trí, giải thoát cho bản thân và mở lòng đón nhận những điều tuyệt vời đang chờ phía trước. Sự thanh thản trong tâm hồn chính là món quà lớn nhất bạn có thể tự tặng cho chính mình.

Theo dõi kênh để cùng tìm lại sự bình an và nuôi dưỡng tâm hồn mỗi ngày.`
                },
                s5: {
                    title: `Sự Im Lặng Trưởng Thành`,
                    text: `Càng trưởng thành, con người ta càng trở nên im lặng.

Không phải vì hết lời để nói, mà vì họ nhận ra không phải ai cũng đủ trình độ và trải nghiệm để hiểu được sự trầm mặc của mình. Giải thích với người không cùng tầng tư duy chỉ làm tổn hại đến năng lượng và thời gian quý báu của bạn. Nước sâu thì chảy chậm, người khôn thì nói ít. Khi bạn ngừng tranh luận đúng sai với đời, đó là lúc trí tuệ lên tiếng.

Sự trưởng thành thật sự bắt đầu khi bạn không còn khao khát chứng tỏ bản thân với bất kỳ ai. Bạn hiểu rằng thị phi và những lời đàm tiếu ngoài kia chỉ là mây khói thoảng qua, còn sự bình yên trong tâm hồn mới là đích đến cuối cùng. Học cách sống khiêm nhường, lặng lẽ làm việc và tận hưởng từng phút giây của cuộc sống.

Im lặng không phải là chịu đựng hay cam chịu, mà là sự tĩnh lặng của một tâm trí đã trải qua đủ phong ba bão táp. Người im lặng nghe được tiếng nói của nội tâm, nhìn rõ bản chất của sự vật và biết lúc nào nên tiến, lúc nào nên lui.

Lặng lẽ tích lũy sức mạnh và tri thức, rồi thời gian sẽ cho tất cả những câu trả lời thỏa đáng nhất.

Đón xem các bài học cuộc sống sâu sắc tiếp theo bằng cách bấm nút đăng ký và theo dõi kênh.`
                }
            };

            let currentEditingDaoLyStoryId = null;

            async function openDaoLyStudioModal(storyId = null) {
                currentEditingDaoLyStoryId = storyId;
                let dialog = document.getElementById("dao-ly-dialog");
                if (dialog) {
                    let titleInput = document.getElementById("dao-ly-title");
                    let textInput = document.getElementById("dao-ly-story-text");
                    let select = document.getElementById("dao-ly-sample-select");
                    let submitBtn = document.getElementById("dao-ly-submit-btn");

                    if (storyId) {
                        if (submitBtn) submitBtn.innerText = "🚀 Khởi Chạy Lại Pipeline Đạo Lý";
                        if (select) select.value = "custom";
                        if (titleInput) titleInput.value = storyId;
                        if (textInput) textInput.value = "Đang tải kịch bản...";
                        
                        try {
                            let storyRes = await fetch(`/v1/system/agent/files/${encodeURIComponent(storyId)}/story/story.txt`);
                            if (storyRes.ok) {
                                let txt = await storyRes.text();
                                if (textInput) textInput.value = txt;
                            } else {
                                if (textInput) textInput.value = "";
                            }
                        } catch(e) {
                            console.error("Failed to load story.txt", e);
                            if (textInput) textInput.value = "";
                        }
                    } else {
                        if (submitBtn) submitBtn.innerText = "🚀 Khởi Tạo & Render Video Đạo Lý";
                        if (select) select.value = "custom";
                        if (titleInput) {
                            let timestamp = new Date().toISOString().replace(/[-:T.]/g, "").slice(2, 10);
                            titleInput.value = "Kịch Bản Đạo Lý " + timestamp;
                        }
                        if (textInput) textInput.value = "";
                    }
                    await loadVoicesDropdown();
                    dialog.showModal();
                }
            }

            function closeDaoLyStudioModal() {
                let dialog = document.getElementById("dao-ly-dialog");
                if (dialog) dialog.close();
                document.querySelectorAll('.header-menu a').forEach(a => a.classList.remove('active'));
                let homeBtn = document.getElementById('nav-home');
                if (homeBtn) homeBtn.classList.add('active');
            }

            function onSelectDaoLySample(key) {
                let titleInput = document.getElementById("dao-ly-title");
                let textInput = document.getElementById("dao-ly-story-text");
                if (DAO_LY_SAMPLES[key]) {
                    if (titleInput) titleInput.value = DAO_LY_SAMPLES[key].title;
                    if (textInput) textInput.value = DAO_LY_SAMPLES[key].text;
                }
            }

            async function submitDaoLyProject(event) {
                event.preventDefault();
                let titleVal = document.getElementById("dao-ly-title").value.trim();
                let storyText = document.getElementById("dao-ly-story-text").value.trim();
                let voiceVal = document.getElementById("dao-ly-voice").value;
                let artStyle = document.getElementById("dao-ly-art-style").value;
                let aspect = document.getElementById("dao-ly-aspect").value;

                if (!titleVal || !storyText) {
                    alert("Vui lòng nhập cả Tiêu đề video và Nội dung kịch bản đọc!");
                    return;
                }

                try {
                    let projName = currentEditingDaoLyStoryId;
                    if (!projName) {
                        let slug = titleVal.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
                            .replace(/đ/g, "d").replace(/Đ/g, "d")
                            .replace(/[^a-z0-9_-]/g, "_").replace(/_+/g, "_").replace(/^_+|_+$/g, "");
                        
                        let timestamp = new Date().toISOString().replace(/[-:T.]/g, "").slice(2, 10);
                        projName = "dao_ly_" + (slug || "story") + "_" + timestamp;

                        closeDaoLyStudioModal();

                        let res = await fetch("/v1/projects?story_id=" + encodeURIComponent(projName), { method: "POST" });
                        if (!res.ok) {
                            let txt = await res.text();
                            let detail = txt;
                            try { detail = JSON.parse(txt).detail || txt; } catch(_) {}
                            alert("Lỗi tạo dự án: " + detail);
                            return;
                        }
                    } else {
                        closeDaoLyStudioModal();
                    }

                    let voiceConfig = {
                        provider: "omnivoice",
                        voice_id: voiceVal,
                        start_fragment: 0,
                        limit_fragments: 0
                    };

                    // Run pipeline with created project
                    let runUrl = `/v1/projects/${encodeURIComponent(projName)}/story/run`;
                    let runRes = await fetch(runUrl, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            voice_config: voiceConfig,
                            art_style: artStyle,
                            story_text: storyText,
                            use_watermark: true,
                            use_subtitles: true
                        })
                    });

                    if (runRes.ok) {
                        await loadProjects();
                        selectChapter(projName, "story");
                    } else {
                        let txt = await runRes.text();
                        let detail = txt;
                        try { detail = JSON.parse(txt).detail || txt; } catch(_) {}
                        alert("Lỗi chạy pipeline: " + detail);
                    }

                } catch(e) {
                    alert("Không thể khởi tạo dự án Đạo Lý: " + e);
                }
            }

            function openMusicDialog() {
                document.getElementById("music-project-name").value = "";
                document.getElementById("music-path").value = "";
                document.getElementById("music-file").value = "";
                document.getElementById("music-dialog").showModal();
            }

            function closeMusicDialog() {
                let dialog = document.getElementById("music-dialog");
                if (dialog) dialog.close();
                document.querySelectorAll('.header-menu a').forEach(a => a.classList.remove('active'));
                let homeBtn = document.getElementById('nav-home');
                if (homeBtn) homeBtn.classList.add('active');
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

            const LOCAL_MEDIA_ORIGIN = window.location.origin;
            let mediaExistsCache = {};

            async function getMediaExists(url) {
                if (mediaExistsCache[url] === true) {
                    return true;
                }
                if (mediaExistsCache[url] === "checking") {
                    return false;
                }
                mediaExistsCache[url] = "checking";
                try {
                    let res = await fetch(url, { method: "HEAD" });
                    if (res.ok) {
                        mediaExistsCache[url] = true;
                        return true;
                    }
                    if (url.startsWith("/media/")) {
                        let localFallback = url.replace("/media", window.location.origin);
                        try {
                            let resLocal = await fetch(localFallback, { method: "HEAD" });
                            if (resLocal.ok) {
                                mediaExistsCache[url] = true;
                                return true;
                            }
                        } catch (errLocal) {}
                    }
                    mediaExistsCache[url] = false;
                    return false;
                } catch(e) {
                    mediaExistsCache[url] = false;
                    return false;
                }
            }

            function showPreviewModal(title, contentHtml) {
                document.getElementById("preview-modal-title").innerText = title;
                document.getElementById("preview-modal-media").innerHTML = contentHtml;
                document.getElementById("preview-modal").style.display = "flex";
            }

            function closePreviewModal(event) {
                if (event) event.stopPropagation();
                let container = document.getElementById("preview-modal-media");
                let media = container.querySelector("video, audio");
                if (media) {
                    media.pause();
                }
                document.getElementById("preview-modal").style.display = "none";
                container.innerHTML = "";
            }

            function playAudioPreview(url, fragIdx) {
                showPreviewModal(`Frag #${fragIdx} - Audio Voiceover`, `
                    <audio src="${url}" controls autoplay style="width:100%; max-width:500px; margin-top:1rem;">
                        Your browser does not support the audio element.
                    </audio>
                `);
            }

            function showImagePreview(url, fragIdx) {
                showPreviewModal(`Frag #${fragIdx} - Generated Image`, `
                    <img src="${url}" alt="Frag #${fragIdx} Image" style="max-width:100%; max-height:60vh; border-radius:8px;">
                `);
            }

            function playVideoPreview(url, fragIdx) {
                showPreviewModal(`Frag #${fragIdx} - Video segment`, `
                    <video src="${url}" controls autoplay style="max-width: 100%; max-height: 60vh; aspect-ratio: 9 / 16; border-radius: 8px; background: #000; object-fit: contain;">
                        Your browser does not support the video element.
                    </video>
                `);
            }
        </script>

        <!-- Preview Modal Overlay -->
        <div id="preview-modal" class="preview-modal" onclick="closePreviewModal(event)">
            <div class="preview-modal-content" onclick="event.stopPropagation()">
                <span class="preview-modal-close" onclick="closePreviewModal(event)">&times;</span>
                <h3 id="preview-modal-title" style="margin-top: 0; color: var(--primary-light);">Preview</h3>
                <div class="preview-media-container" id="preview-modal-media">
                    <!-- Dynamic preview element goes here -->
                </div>
            </div>
        </div>
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
