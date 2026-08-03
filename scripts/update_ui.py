import re, pathlib

server_file = pathlib.Path("taka_server.py")
content = server_file.read_text(encoding="utf-8")

# Prepare new UI HTML
new_html = r"""
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
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

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            background-image: radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.15) 0%, transparent 40%),
                              radial-gradient(circle at 90% 80%, rgba(16, 185, 129, 0.08) 0%, transparent 40%);
            padding: 1.5rem;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1.2rem;
            flex-wrap: wrap;
            gap: 1rem;
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

        h1 { font-size: 1.8rem; font-weight: 800; letter-spacing: -0.05em; }

        .nav-menu {
            display: flex;
            gap: 0.5rem;
            background: rgba(255, 255, 255, 0.03);
            padding: 0.3rem;
            border-radius: 12px;
            border: 1px solid var(--border);
        }

        .nav-tab {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 0.6rem 1.4rem;
            border-radius: 8px;
            font-weight: 700;
            font-size: 0.95rem;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .nav-tab:hover { color: #fff; background: rgba(255,255,255,0.05); }

        .nav-tab.active {
            background: linear-gradient(135deg, var(--primary), #7c3aed);
            color: #fff;
            box-shadow: 0 4px 15px var(--primary-glow);
        }

        .agent-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1.2rem;
            border-radius: 50px;
            background: var(--card-bg);
            border: 1px solid var(--border);
            font-size: 0.85rem;
            font-weight: 600;
        }

        .badge-dot { width: 8px; height: 8px; border-radius: 50%; background-color: var(--text-muted); }

        .agent-badge.connected .badge-dot {
            background-color: var(--success);
            box-shadow: 0 0 10px var(--success-glow);
        }

        .cat-pill {
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 0.4rem 1rem;
            border-radius: 50px;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .cat-pill:hover, .cat-pill.active {
            background: rgba(139, 92, 246, 0.15);
            border-color: var(--primary);
            color: #fff;
        }

        .grid {
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 1.5rem;
        }

        @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }

        .glass-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.2rem;
        }

        .story-section { margin-bottom: 1.2rem; }

        .story-header-title {
            font-size: 0.85rem;
            font-weight: 800;
            color: var(--primary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 0.6rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .chapter-list { display: flex; flex-direction: column; gap: 0.4rem; }

        .chapter-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 0.9rem;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.01);
            border: 1px solid var(--border);
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .chapter-item:hover, .chapter-item.active {
            background: rgba(139, 92, 246, 0.08);
            border-color: var(--primary);
            transform: translateX(4px);
        }

        .run-btn {
            background: linear-gradient(135deg, var(--primary), #a78bfa);
            border: none;
            color: #fff;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            font-weight: 700;
            font-size: 0.85rem;
            cursor: pointer;
            box-shadow: 0 4px 15px var(--primary-glow);
            transition: all 0.2s ease;
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
    <header>
        <div class="logo-container">
            <div class="logo-icon">🌌</div>
            <div>
                <h1>Taka Tales Studio</h1>
                <p style="color: var(--text-muted); font-size: 0.85rem;">Google Flow AI Studio • Multi-Agent Pipeline</p>
            </div>
        </div>

        <nav class="nav-menu">
            <button class="nav-tab active" id="nav-home-btn" onclick="switchNavTab('home')">
                🏠 Home (Projects)
            </button>
            <button class="nav-tab" id="nav-voices-btn" onclick="switchNavTab('voices')">
                🎙️ Voices
            </button>
        </nav>

        <div style="display: flex; gap: 1rem; align-items: center;">
            <div class="agent-badge" id="agent-badge">
                <div class="badge-dot" id="badge-dot"></div>
                <span id="agent-status">Connecting...</span>
            </div>
            <button class="run-btn" style="background: linear-gradient(135deg, #10b981, #059669); padding: 0.6rem 1.4rem;" onclick="openNewProjectModal()">
                ✨ + New Project
            </button>
        </div>
    </header>

    <!-- HOME VIEW (Projects Workspace) -->
    <div id="view-home">
        <!-- Category Filter Pills -->
        <div style="display: flex; gap: 0.6rem; margin-bottom: 1.2rem; flex-wrap: wrap;">
            <button class="cat-pill active" onclick="filterCategory('all', this)">🌐 All</button>
            <button class="cat-pill" onclick="filterCategory('story', this)">📖 Story (Lore-Keeper)</button>
            <button class="cat-pill" onclick="filterCategory('reels', this)">📱 Reels (9:16)</button>
            <button class="cat-pill" onclick="filterCategory('long', this)">🎬 Long (16:9)</button>
            <button class="cat-pill" onclick="filterCategory('sketch', this)">✏️ Sketch (16:9)</button>
            <button class="cat-pill" onclick="filterCategory('music', this)">🎵 Music</button>
        </div>

        <div class="grid">
            <!-- Sidebar Projects List -->
            <div class="glass-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                    <h3 style="font-size: 1rem; color: #fff; margin: 0;">📁 Projects Directory</h3>
                    <button onclick="openNewProjectModal()" style="background: rgba(139,92,246,0.15); border: 1px solid var(--primary); color: #a78bfa; padding: 0.3rem 0.6rem; border-radius: 6px; cursor: pointer; font-size: 0.75rem; font-weight: bold;">+ New</button>
                </div>
                <div id="project-list"></div>
            </div>

            <!-- Workspace Details Area -->
            <div class="glass-card">
                <div id="details-panel" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem; margin-bottom: 1rem;">
                        <div>
                            <h2 id="current-project-title" style="font-size: 1.3rem; font-weight: 800; color: #fff;">Select a Project</h2>
                            <p id="current-project-subtitle" style="font-size: 0.8rem; color: var(--text-muted);">Workspace Details</p>
                        </div>
                        <div id="status-banner" style="font-size: 0.8rem; padding: 0.3rem 0.8rem; border-radius: 6px; background: rgba(255,255,255,0.05); border: 1px solid var(--border);">Ready</div>
                    </div>

                    <!-- Voice & Art Config Bar -->
                    <div style="background: rgba(0,0,0,0.2); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;">
                        <h4 style="font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--primary);">⚙️ Pipeline Settings</h4>
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem;">
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">Voice Profile</label>
                                <select id="voice-select" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;"></select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted); display: block; margin-bottom: 0.3rem;">Art Style</label>
                                <select id="art-style-select" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff;">
                                    <option value="comic">Comic 2D Vector</option>
                                    <option value="pencil">Pencil Sketch (Dark Grimdark)</option>
                                    <option value="anime">Anime Shonen</option>
                                </select>
                            </div>
                        </div>
                        <div style="display: flex; gap: 1rem; margin-top: 1rem; flex-wrap: wrap;">
                            <button class="run-btn" onclick="startPipelineForActiveProject('all')">🚀 Run Full Pipeline</button>
                            <button class="run-btn" style="background: linear-gradient(135deg, #f59e0b, #d97706);" onclick="startPipelineForActiveProject('subtitles_only')">📝 Subtitles Only Rerun</button>
                        </div>
                    </div>

                    <!-- Fragments & Output Video -->
                    <div id="workspace-content"></div>
                </div>

                <div id="no-project-selected" style="text-align: center; padding: 4rem 1rem; color: var(--text-muted);">
                    <div style="font-size: 3rem; margin-bottom: 1rem;">✨</div>
                    <h3>No Project Selected</h3>
                    <p style="font-size: 0.9rem; margin-top: 0.5rem;">Click <b>"+ New Project"</b> above to create a project or select an existing project from the left directory.</p>
                </div>
            </div>
        </div>
    </div>

    <!-- VOICES DASHBOARD VIEW -->
    <div id="view-voices" style="display: none;">
        <div class="glass-card" style="padding: 1.5rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <div>
                    <h2 style="font-size: 1.4rem; font-weight: 800; color: #fff;">🎙️ OmniVoice Voice Management</h2>
                    <p style="color: var(--text-muted); font-size: 0.85rem;">Reference voice profiles, audio sample preview & protected voice profiles</p>
                </div>
            </div>
            <div id="voices-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.2rem;"></div>
        </div>
    </div>

    <!-- NEW PROJECT MODAL (Google Flow) -->
    <div id="new-project-modal" class="modal-overlay" style="display: none;">
        <div class="glass-card" style="max-width: 540px; width: 90%; margin: auto; padding: 1.8rem; border-radius: 16px; position: relative;">
            <button onclick="closeNewProjectModal()" style="position: absolute; top: 1rem; right: 1rem; background: none; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer;">&times;</button>
            <h3 style="font-size: 1.3rem; font-weight: 800; margin-bottom: 0.4rem; color: #fff;">✨ Create New Project (Google Flow)</h3>
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

    <!-- PREVIEW MODAL -->
    <div id="preview-modal" class="modal-overlay" style="display: none;" onclick="closePreviewModal(event)">
        <div class="glass-card" style="max-width: 600px; width: 90%; margin: auto; padding: 1.5rem;" onclick="event.stopPropagation()">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <h3 id="preview-modal-title" style="color: #fff; margin: 0;">Preview</h3>
                <button onclick="closePreviewModal(event)" style="background: none; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <div id="preview-modal-media" style="text-align: center;"></div>
        </div>
    </div>

    <script>
        let currentCategoryFilter = "all";
        let selectedNewCategory = "story";
        let currentStory = null;
        let currentChapter = null;

        function switchNavTab(tab) {
            document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
            document.getElementById('nav-' + tab + '-btn').classList.add('active');
            if (tab === 'home') {
                document.getElementById('view-home').style.display = 'block';
                document.getElementById('view-voices').style.display = 'none';
                loadProjects();
            } else if (tab === 'voices') {
                document.getElementById('view-home').style.display = 'none';
                document.getElementById('view-voices').style.display = 'block';
                loadVoicesDashboard();
            }
        }

        function filterCategory(cat, el) {
            currentCategoryFilter = cat;
            document.querySelectorAll('.cat-pill').forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');
            loadProjects();
        }

        async function updateAgentStatus() {
            try {
                let res = await fetch("/v1/agent/status");
                let data = await res.json();
                let badge = document.getElementById("agent-badge");
                let status = document.getElementById("agent-status");
                if (data.connected) {
                    badge.className = "agent-badge connected";
                    status.innerText = "Agent Online";
                } else {
                    badge.className = "agent-badge";
                    status.innerText = "Agent Offline";
                }
            } catch(e) {}
        }

        async function loadProjects() {
            try {
                let res = await fetch("/v1/projects");
                let stories = await res.json();
                let list = document.getElementById("project-list");
                list.innerHTML = "";
                
                if (!Array.isArray(stories) || stories.length === 0) {
                    list.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">No projects yet. Click '+ New' to create one.</p>`;
                    return;
                }

                stories.forEach(s => {
                    if (currentCategoryFilter !== 'all' && s.story_id !== currentCategoryFilter && s.story_id !== 'reels') {
                        return;
                    }
                    let sec = document.createElement("div");
                    sec.className = "story-section";
                    
                    let header = document.createElement("div");
                    header.className = "story-header-title";
                    header.innerHTML = `<span>${s.title || s.story_id}</span> <span>(${s.chapters.length})</span>`;
                    sec.appendChild(header);

                    let chList = document.createElement("div");
                    chList.className = "chapter-list";

                    s.chapters.forEach(c => {
                        let item = document.createElement("div");
                        item.className = "chapter-item" + (c.id === currentChapter ? " active" : "");
                        item.onclick = () => selectChapter(s.story_id, c.id, c.title);
                        
                        let statusColor = c.status === 'completed' ? '#10b981' : '#f59e0b';
                        item.innerHTML = `
                            <div>
                                <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">${c.title}</div>
                                <div style="font-size: 0.7rem; color: var(--text-muted);">ID: ${c.id}</div>
                            </div>
                            <span style="font-size: 0.7rem; color: ${statusColor}; font-weight: bold;">● ${c.status}</span>
                        `;
                        chList.appendChild(item);
                    });

                    sec.appendChild(chList);
                    list.appendChild(sec);
                });
            } catch(e) {
                console.error("loadProjects error:", e);
            }
        }

        async function selectChapter(storyId, chapterId, title) {
            currentStory = storyId;
            currentChapter = chapterId;
            document.getElementById("no-project-selected").style.display = "none";
            document.getElementById("details-panel").style.display = "block";
            document.getElementById("current-project-title").innerText = title || chapterId;
            document.getElementById("current-project-subtitle").innerText = `Category: ${storyId} • ID: ${chapterId}`;
            
            loadVoicesSelect();
            loadFragments(storyId, chapterId);
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
            container.innerHTML = `<p style="color: var(--text-muted);">Loading workspace content...</p>`;
            try {
                let res = await fetch(`/v1/projects/${encodeURIComponent(storyId)}/${encodeURIComponent(chapterId)}/fragments`);
                let frags = await res.json();
                if (!Array.isArray(frags) || frags.length === 0) {
                    container.innerHTML = `<p style="color: var(--text-muted);">No fragments found for this chapter.</p>`;
                    return;
                }
                
                let html = `<h4 style="font-size: 0.95rem; margin-bottom: 0.8rem; color: var(--primary);">📝 Fragments & Audio Clips (${frags.length})</h4>`;
                html += `<div style="display: flex; flex-direction: column; gap: 0.6rem;">`;
                frags.forEach(f => {
                    html += `
                        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); padding: 0.8rem; border-radius: 8px; display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <span style="font-size: 0.75rem; color: var(--primary); font-weight: bold; margin-right: 0.5rem;">#${f.index + 1}</span>
                                <span style="font-size: 0.85rem; color: #eee;">${f.text}</span>
                            </div>
                        </div>
                    `;
                });
                html += `</div>`;
                container.innerHTML = html;
            } catch(e) {
                container.innerHTML = `<p style="color: var(--danger);">Error loading fragments: ${e.message}</p>`;
            }
        }

        async function startPipelineForActiveProject(mode) {
            if (!currentStory || !currentChapter) {
                alert("Please select a project chapter first!");
                return;
            }
            let voiceId = document.getElementById("voice-select").value;
            let artStyle = document.getElementById("art-style-select").value;
            
            let url = `/v1/projects/${encodeURIComponent(currentStory)}/${encodeURIComponent(currentChapter)}/run`;
            document.getElementById("status-banner").innerText = "Processing...";
            try {
                let res = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        voice_config: { voice_id: voiceId, provider: "omnivoice" },
                        art_style: artStyle,
                        rerun_mode: mode
                    })
                });
                if (res.ok) {
                    alert("Pipeline launched! Agent will render in background.");
                } else {
                    let err = await res.json();
                    alert("Failed: " + (err.detail || "Error launching pipeline"));
                }
            } catch(e) {
                alert("Error: " + e.message);
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
                let data = await res.json();
                if (res.ok) {
                    closeNewProjectModal();
                    loadProjects();
                    alert("🎉 Project created successfully!");
                } else {
                    alert("Error: " + (data.detail || "Creation failed"));
                }
            } catch(err) {
                alert("Creation error: " + err.message);
            }
        }

        function closePreviewModal(event) {
            if (event) event.stopPropagation();
            document.getElementById("preview-modal").style.display = "none";
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
"""

content = re.sub(r'@app\.get\("/", response_class=HTMLResponse\)\s*async def dashboard\(\):.*', new_html, content, flags=re.DOTALL)
server_file.write_text(content, encoding="utf-8")
print("Updated taka_server.py dashboard cleanly.")
