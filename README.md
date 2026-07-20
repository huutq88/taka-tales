# 🎙️ Taka Tales — Hệ thống Sản xuất Video Tự động hóa

**Taka Tales** (trước đây là *Teller of Tales*) là một hệ thống tự động hóa thông minh giúp chuyển đổi các chương sách hoặc câu chuyện bằng văn bản thành video thuyết minh hoàn chỉnh có chất lượng cao. Hệ thống kết hợp các mô hình ngôn ngữ lớn (LLM), công cụ tạo ảnh AI (Stable Diffusion), tổng hợp giọng nói (TTS) và thư viện MoviePy để thực hiện toàn bộ quy trình biên tập video một cách tự động.

Dự án đã được nâng cấp lên kiến trúc **Client-Server** bất đồng bộ qua **WebSockets**, cho phép tách biệt giữa máy chủ điều phối và các máy trạm (workers/agents) xử lý GPU hạng nặng, đồng thời tích hợp trực tiếp với cơ sở dữ liệu **Lore-Keeper**.

---

## ✨ Điểm nổi bật & Tính năng mới

1. **Kiến trúc Client-Server Asynchronous**:
   * **Taka Coordinator Server (`taka_server.py`)**: Máy chủ trung tâm viết bằng FastAPI, phục vụ giao diện Web Dashboard và quản lý các tác vụ.
   * **Taka Agent (`taka_agent.py`)**: Worker chạy ngầm kết nối qua WebSocket, tự động kiểm tra môi trường phần cứng (CUDA/MPS), tự động cài đặt công cụ OmniVoice TTS và thực thi tác vụ render video.
2. **Giao diện Web Dashboard Hiện đại**: Giao diện tối tối giản (Dark Glassmorphism) hiển thị danh sách dự án, giám sát trạng thái thời gian thực của agent, theo dõi tiến độ từng phân đoạn (fragment) và phát video trực tiếp trên trình duyệt khi hoàn thành.
3. **Tích hợp Cơ sở dữ liệu Lore-Keeper**: Tự động tải nội dung chương truyện trực tiếp từ cơ sở dữ liệu PostgreSQL (`POSTGRES_URI`) khi người dùng nhập `chapter_id`, thay vì phải tạo file thủ công.
4. **Hệ thống TTS đa dạng**: Hỗ trợ giọng nói chất lượng cao từ **Kokoro TTS**, **ElevenLabs**, **Edge-TTS** (miễn phí) và tích hợp nâng cao với **OmniVoice** (tự động clone mã nguồn từ GitHub nếu thiếu).
5. **Prompt Engine Thông minh**: Tự sinh mô tả hình ảnh bằng **Ollama** (offline), **ChatGPT API**, hoặc fallback bằng **KeyBERT** trích xuất từ khóa.
6. **Pipeline Xử lý Song song**: Tự động chia nhỏ câu chuyện thành các phân đoạn (fragment) có độ dài cấu hình được, chạy song song các tiến trình xử lý hình ảnh/âm thanh để tối ưu hiệu suất phần cứng.

---

## 📂 1. Cấu trúc thư mục dự án (Folder Structure)

Để giữ cho kho lưu trữ Git luôn **gọn nhẹ nhất có thể** khi commit, hệ thống sử dụng cơ chế bỏ qua (ignore) toàn bộ các tệp trung gian và tệp đầu ra dung lượng lớn được tạo ra trong quá trình chạy pipeline.

```text
taka-tales/
├── taka_server.py                 # Máy chủ điều phối & Giao diện Web Dashboard (FastAPI)
├── taka_agent.py                  # Agent xử lý ngầm (WebSocket Client & Worker)
├── config.ini                     # Cấu hình hệ thống (Ollama, SD, TTS, Agent, Database)
├── requirements.txt               # Thư viện phụ thuộc của Python
├── notes.txt                      # Ghi chú kỹ thuật & Tối ưu hóa GPU cho MoviePy
├── README.md                      # Hướng dẫn sử dụng hệ thống
├── TAKA_TALES_ARCHITECTURE.md     # Tài liệu đặc tả kiến trúc kỹ thuật chi tiết
├── bg_music/                      # Nhạc nền cho video (được cấu hình trong config.ini)
│   └── *.mp3                      # [Bị bỏ qua bởi Git] Tránh commit các file nhạc nặng
├── core/                          # Nhân engine xử lý video và NLP
│   ├── __init__.py
│   ├── video_engine.py            # Logic xử lý text, TTS, SD API, MoviePy Rendering
│   └── characters_descriptions.ini# Mô tả nhân vật cố định để tạo hình nhất quán
├── tools/                         # Các công cụ hỗ trợ
│   ├── OmniVoice/                 # [Bị bỏ qua bởi Git] Tự động clone & cài đặt bởi Agent
│   ├── move_to_dirs.py
│   └── process_content.py
└── projects/                      # Thư mục chứa các dự án biên tập video
    └── [project_name]/
        ├── story.txt              # Nội dung văn bản (đầu vào thủ công hoặc tải từ DB)
        ├── final.mp4              # [Bị bỏ qua bởi Git] Video hoàn chỉnh cuối cùng
        ├── text/                  # [Bị bỏ qua bởi Git] Câu và fragment đã được bóc tách
        ├── audio/                 # [Bị bỏ qua bởi Git] Các file giọng nói phân đoạn (.wav/.mp3)
        ├── images/                # [Bị bỏ qua bởi Git] Các hình ảnh minh họa (.jpg)
        └── videos/                # [Bị bỏ qua bởi Git] Các clip phân đoạn (.mp4)
```

> [!TIP]
> Nhờ cấu hình `.gitignore` tối ưu, bạn chỉ lưu trữ mã nguồn cốt lõi và dữ liệu văn bản đầu vào (`story.txt`). Toàn bộ tài nguyên đa phương tiện phát sinh (ảnh, âm thanh, video, thư viện cài thêm) đều không bị đẩy lên Git.

---

## ⚙️ 2. Hướng dẫn Cấu hình (`config.ini`)

Mở file `config.ini` và cập nhật các tham số phù hợp với môi trường của bạn:

```ini
[GENERAL]
DEBUG = True
FPS = 10                     # Số khung hình/giây (FPS cao hơn làm video mượt nhưng render lâu)
FREE_SWAP = 200              # GB swap trống tối thiểu để tiếp tục tác vụ tiếp theo

[AUDIO]
TTS_PROVIDER = kokoro        # Nhà cung cấp TTS: kokoro, elevenlabs, hoặc edge (Edge-TTS miễn phí)
KOKORO_VOICE_ID = af_heart   # Voice ID của Kokoro
KOKORO_URL = http://localhost:8880/v1/audio/speech
BG_MUSIC = yes               # Bật nhạc nền
BG_MUSIC_PATH = bg_music/Fantasy Music - Passing the Crown - Avery Alexander (youtube).mp3
MUSIC_VOLUME = 0.05          # Âm lượng nhạc nền so với giọng đọc (khuyên dùng: 0.05 - 0.25)

[IMAGE_PROMPT]
IMAGE_PROMPT_PROVIDER = ollama # ollama (local LLM), yes (ChatGPT), hoặc no (KeyBERT fallback)
OLLAMA_MODEL = llama3.1:8b-instruct-q8_0

[STABLE_DIFFUSION]
USE_SD_VIA_API = yes         # Kết nối API Stable Diffusion cục bộ (A1111) hoặc pollinations (online)
SD_URL = http://127.0.0.1:7860
image_width = 1344
image_height = 768
seed = -1                    # -1 cho ngẫu nhiên, số nguyên dương cố định để giữ nét vẽ ổn định

[TAKA_AGENT]
SERVER_URL = http://localhost:8080
WORKSPACE_ID = default_workspace
OMNIVOICE_PATH = tools/OmniVoice
OMNIVOICE_MODEL_DIR = tools/OmniVoice/checkpoints

[LORE_KEEPER]
# Cấu hình kết nối Postgres DB để tải chương truyện tự động
POSTGRES_URI = postgresql://username:password@localhost:5432/lore_keeper_db
```

---

## 🛠️ 3. Cài đặt hệ thống (Installation)

### Yêu cầu hệ thống
* Python **3.8** đến **3.11**
* Hệ điều hành hỗ trợ GPU NVIDIA (CUDA) hoặc Apple Silicon (MPS) để tăng tốc xử lý sinh ảnh và render video.
* Cài đặt **ImageMagick** (Yêu cầu bắt buộc đối với thư viện MoviePy để ghi text đè lên video):
  * Tải xuống và cài đặt từ trang chủ [ImageMagick](https://imagemagick.org/script/download.php).
  * Trong lúc cài đặt trên Windows/macOS, đảm bảo tích chọn:
    - *Associate supported file extensions*
    - *Install legacy utilities (e.g. convert)*

### Hướng dẫn Cài đặt Taka-Agent

#### Cách 1: Cài đặt nhanh bằng lệnh One-liner (Khuyên dùng - Giống Melorix)
Nếu máy trạm (Worker Machine) đã cài đặt Python 3, Git, và curl/PowerShell, người dùng mới chỉ cần chạy một dòng lệnh duy nhất trên terminal phù hợp với hệ điều hành của họ (không cần nhập thủ công `workspace_id`, hệ thống sẽ tự động tạo mã định danh duy nhất (Device Fingerprint) làm `workspace_id` mặc định):

* **Dành cho macOS / Linux (Terminal / Bash)**:
  ```bash
  # Thay thế http://localhost:8080 bằng địa chỉ IP/domain của Coordinator Server thực tế
  curl -fsSL "http://localhost:8080/v1/system/install-agent.sh" | bash
  ```
  *(Truyền tham số tùy chọn nếu muốn đặt tên máy trạm: `curl -fsSL "http://localhost:8080/v1/system/install-agent.sh?workspace_id=ten_may_cua_ban" | bash`)*

* **Dành cho Windows (PowerShell)**:
  ```powershell
  # Mở PowerShell và chạy (thay thế http://localhost:8080 bằng địa chỉ IP/domain thực tế của Server)
  irm "http://localhost:8080/v1/system/install-agent.ps1" | iex
  ```
  *(Truyền tham số tùy chọn nếu muốn đặt tên máy trạm: `irm "http://localhost:8080/v1/system/install-agent.ps1?workspace_id=ten_may_cua_ban" | iex`)*

Sau khi hoàn tất cài đặt, hãy di chuyển vào thư mục vừa tạo và khởi chạy Agent:
* **macOS / Linux**:
  ```bash
  cd taka-agent && source env/bin/activate && python taka_agent.py
  ```
* **Windows**:
  ```powershell
  cd taka-agent
  env\Scripts\activate
  python taka_agent.py
  ```

#### Cách 2: Cài đặt thủ công
1. **Khởi tạo môi trường ảo (Virtual Environment)**:
   ```bash
   python -m venv env
   source env/bin/activate       # Trên macOS/Linux
   # hoặc
   env\Scripts\activate          # Trên Windows
   ```
2. **Cài đặt PyTorch phù hợp với GPU của bạn**:
   * *Ví dụ cho CUDA 11.8*:
     ```bash
     pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
     ```
3. **Cài đặt các thư viện trong `requirements.txt`**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Cài đặt thư viện kết nối cơ sở dữ liệu (nếu dùng Lore-Keeper PostgreSQL)**:
   ```bash
   pip install psycopg2-binary
   ```

---

## 🚀 4. Quy trình khởi chạy & Sử dụng (Running & Usage)

Để chạy hệ thống Taka Tales, bạn thực hiện theo các bước sau:

### Bước 1: Khởi chạy Taka Coordinator Server
Chạy máy chủ điều phối FastAPI:
```bash
python taka_server.py
```
Server sẽ chạy mặc định tại cổng `8080`. Bạn có thể truy cập Giao diện quản lý qua trình duyệt tại địa chỉ: **`http://localhost:8080`**.

### Bước 2: Khởi chạy Taka Agent
Trong một terminal khác (ở máy tính có GPU để sinh ảnh/render), kích hoạt môi trường ảo và khởi động Agent:
```bash
python taka_agent.py
```
Agent sẽ kết nối WebSocket đến server, báo cáo cấu hình hệ thống (CUDA, Ollama, Stable Diffusion) và chuyển sang trạng thái chờ lệnh từ Server.

### Bước 3: Tạo và chạy một dự án video
Có 2 phương thức để cấp dữ liệu văn bản đầu vào:

#### Cách 1: Sử dụng thư mục `projects/` cục bộ (Thủ công)
1. Tạo một thư mục con bên trong `projects/`, ví dụ: `projects/chuyen_cua_taka/`
2. Tạo một tệp văn bản tên là `story.txt` bên trong thư mục này và dán nội dung chương truyện vào đó.
3. F5 lại trang `http://localhost:8080`. Dự án `chuyen_cua_taka` sẽ xuất hiện trong danh sách bên trái.
4. Bấm **Run Project** để bắt đầu quy trình.

#### Cách 2: Tải tự động qua Lore-Keeper PostgreSQL (Tự động)
1. Đảm bảo cấu hình đúng `POSTGRES_URI` trong file `config.ini` hoặc biến môi trường `POSTGRES_URI`.
2. F5 trang quản trị và chọn dự án. Bấm **Run Project**.
3. Hệ thống sẽ bật một hộp thoại yêu cầu nhập **Lore-Keeper Chapter ID**.
4. Nhập ID chương truyện cần tải (ví dụ: `chap_01`).
5. Server sẽ tự động kết nối cơ sở dữ liệu, lấy nội dung văn bản, tự động tạo cấu trúc thư mục dự án và tệp `story.txt` tương ứng, sau đó chuyển việc cho Agent bắt đầu sinh âm thanh, sinh ảnh và biên tập video.

### Bước 4: Xem kết quả
Tiến độ xử lý (tách câu, tạo prompt, sinh audio, tạo hình vẽ, dựng clip ngắn, ghép nối video cuối cùng) được hiển thị trực quan theo thời gian thực trên Web UI.
Khi hoàn thành, trình phát video sẽ xuất hiện trên Web UI để bạn có thể xem trực tiếp kết quả. Video hoàn thiện cũng được lưu tại:
`projects/[project_name]/final.mp4`

---

## 🧹 5. Quản lý tài nguyên & Dọn dẹp kho Git

Do đặc thù sinh nhiều file đa phương tiện trung gian trong quá trình tạo video, nếu bạn muốn dọn dẹp thư mục làm việc cục bộ hoặc cần xóa các tài nguyên đã tạo để giải phóng ổ đĩa, bạn có thể thực hiện:

### Lệnh dọn dẹp các tệp trung gian sinh ra trong dự án
Bạn có thể xóa thủ công các thư mục `text`, `audio`, `images`, `videos` và file `.mp4` trong dự án của mình khi đã xuất video thành công, hoặc dùng script tự động.
Hệ thống sẽ tự động tạo lại các thư mục này nếu bạn bấm nút **Run Project** chạy lại lần sau.

### Giải pháp Git gọn nhẹ
Các quy tắc trong tệp [.gitignore](file:///Users/huutq/Desktop/WorkingSpace/Demo/taka-tales/.gitignore) đã bảo vệ kho lưu trữ của bạn khỏi việc commit nhầm các tài nguyên nặng. Nếu bạn lỡ commit các tệp âm nhạc hoặc video cũ trước đó, hãy sử dụng lệnh sau để gỡ bỏ chúng khỏi Git tracking mà vẫn giữ lại tệp trên máy cục bộ của bạn:

```bash
# Gỡ bỏ các file mp3 nhạc nền khỏi git tracking
git rm --cached bg_music/*.mp3

# Gỡ bỏ các video mẫu hoặc ảnh mẫu trong docs khỏi git tracking (nếu cần)
git rm --cached docs/*.mp4
git rm --cached docs/screenshot.png

# Gỡ bỏ các thư mục dự án sinh ra trước đó
git rm -r --cached projects/*/audio/ projects/*/images/ projects/*/videos/ projects/*/*.mp4
```
Sau đó commit và push thay đổi. Kho lưu trữ Git của bạn sẽ trở nên vô cùng gọn nhẹ và sạch sẽ!
