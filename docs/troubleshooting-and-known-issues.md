# TakaTales - Báo Cáo Sự Cố & Hướng Dẫn Sửa Lỗi Thường Gặp (Troubleshooting & Known Issues)

> **Tài liệu tổng hợp các bug phức tạp, tốn thời gian xử lý và các nguyên tắc kỹ thuật cốt lõi trong hệ thống TakaTales.**

---

## 📋 Danh Mục Bug & Sự Cố Cốt Lõi

1. [Bug 1: Lỗi Tỷ Lệ Ảnh Dọc 9:16 & Cờ Kích Thước `ima2` OpenAI](#bug-1-lỗi-tỷ-lệ-ảnh-dọc-916--cờ-kích-thước-ima2-openai)
2. [Bug 2: Lỗi Nhận Workspace & Fallback URL Server Local](#bug-2-lỗi-nhận-workspace--fallback-url-server-local)
3. [Bug 3: Lỗi UnboundLocalError & Thiếu `final_aspect` Trong Vòng Lặp Pipeline](#bug-3-lỗi-unboundlocalerror--thiếu-final_aspect-trong-vòng-lặp-pipeline)
4. [Bug 4: Lỗi Trùng Lặp Thư Mục Dự Án Ngầm (`~/.taka-agent/projects/projects/`)](#bug-4-lỗi-trùng-lặp-thư-mục-dự-án-ngầm-taka-agentprojectsprojects)
5. [Bug 5: Chênh Lệch Tiến Độ Giữa `ima2` Proxy (3333) Và Taka Web UI (8080)](#bug-5-chênh-lệch-tiến-độ-giữa-ima2-proxy-3333-và-taka-web-ui-8080)
6. [Bug 6: Lỗi `NameError: num_frags` Khi Bấm Video Render Only](#bug-6-lỗi-nameerror-num_frags-khi-bấm-video-render-only)

---

## 6. Bug 6: Lỗi `NameError: num_frags` Khi Bấm Video Render Only

### 🔴 Hiện tượng:
- Khi chọn chế độ `Video Render Only`, hệ thống tạo xong các video clip phân đoạn (`video*.mp4`) nhưng không báo hoàn thành và không xuất file `final.mp4` trên Web UI.

### 🔍 Nguyên nhân gốc rễ (Root Cause):
- Trong `taka_agent.py` dòng 1384, khi gửi sự kiện WebSocket thông báo hoàn thành pipeline, mã nguồn sử dụng nhầm tên biến `num_frags` (không tồn tại) thay vì `total_frags`. Điều này khiến agent văng ngoại lệ `NameError: name 'num_frags' is not defined` ngay trước bước báo `completed`.

### 🛠️ Giải pháp khắc phục (Fix):
1. **Sửa `taka_agent.py`**: Thay `num_frags` bằng `total_frags`.
2. **Cập nhật `core/video_engine.py`**: Bổ sung tự động sao chép file xuất ra sang `final.mp4` trực tiếp bên trong `make_final_video`.

---

## 1. Bug 1: Lỗi Tỷ Lệ Ảnh Dọc 9:16 & Cờ Kích Thước `ima2` OpenAI

### 🔴 Hiện tượng:
- Khi tạo ảnh cho video **Reels Dọc (9:16)**, ảnh trả về từ `ima2` bị méo tỷ lệ 4:5 (`1122 × 1402`), ảnh bị crop mất khung hình hoặc bị ép về ảnh Ngang 3:2 (`1536 × 1024`).

### 🔍 Nguyên nhân gốc rễ (Root Cause):
1. **Lỗi Model Mặc Định**: Trong `~/.ima2/config.json`, thuộc tính `defaults.image` bị chỉ định là `"oauth/gpt-5.6-sol"`. Model `sol` bị hardcode luôn trả về ảnh Ngang `1536x1024` và phớt lờ cờ `-s`.
2. **Lỗi Parser Cờ `-s` Của OpenAI API**: Khi truyền cờ `-s 1024x1824`, bộ kiểm tra schema tool của OpenAI bị nhảy fallback về 4:5 (`1122x1402`). Trong khi đó, cờ **`-s 1152x2048`** (chuẩn 2K 9:16) hoặc **`-s 1024x1536`** mới kích hoạt đúng chế độ **Native 9:16 Vertical Portrait (`941 × 1672`)**.
3. **Lỗi LLM Prompt Enrichment**: Khi prompt không có từ khóa định hướng khung hình rõ ràng, LLM của OpenAI tự động mở rộng prompt thành cảnh góc rộng cinematic (Ngang).

### 🛠️ Giải pháp khắc phục (Fix):
1. **Cập nhật `~/.ima2/config.json`**:
   ```json
   {
     "provider": "oauth",
     "defaults": { "image": "oauth/gpt-5.6-luna" },
     "imageModels": { "default": "oauth/gpt-5.6-luna" }
   }
   ```
2. **Cập nhật Cờ Kích Thước & Tiền Tố Orientation** (`core/video_engine.py`):
   - **Với Dọc 9:16**: Dùng cờ `-s 1152x2048` và chèn tiền tố prompt:
     `"Vertical 9:16 portrait orientation, tall vertical mobile frame format, <prompt>"`
   - **Với Ngang 16:9**: Dùng cờ `-s 1824x1024` và chèn tiền tố prompt:
     `"Horizontal 16:9 widescreen landscape orientation, wide cinematic format, <prompt>"`
3. **Thuật Toán Resizing 0% Crop Loss**:
   Trong `core/video_engine.py`, khi ảnh nguồn và ảnh đích cùng định hướng Dọc ($\text{ratio diff} < 0.25$), Taka thực hiện **Resize Lanczos trực tiếp** từ `941 × 1672` sang `1024 × 1824` với **0% cropping loss**.

---

## 2. Bug 2: Lỗi Nhận Workspace & Fallback URL Server Local

### 🔴 Hiện tượng:
- Web UI báo lỗi *"workspace disconnected"* hoặc không quét được danh sách dự án local.

### 🔍 Nguyên nhân gốc rễ (Root Cause):
- File `taka_agent.py` mặc định trỏ `SERVER_URL` về địa chỉ server remote không tồn tại thay vì `http://127.0.0.1:8080`. Đồng thời `taka_server.py` yêu cầu Workspace ID khớp chính xác mới cho phép tunnel request.

### 🛠️ Giải pháp khắc phục (Fix):
1. Đặt fallback mặc định trong `taka_agent.py`: `SERVER_URL = "http://127.0.0.1:8080"`.
2. Trong `taka_server.py` (`tunnel_request_to_agent`), tự động kết nối với local agent đang active gần nhất nếu client gửi request không kèm Workspace ID.

---

## 3. Bug 3: Lỗi UnboundLocalError & Thiếu `final_aspect` Trong Vòng Lặp Pipeline

### 🔴 Hiện tượng:
- Khi chạy lại riêng bước tạo ảnh (`rerun_mode="images_only"`), agent bị crash với lỗi:
  `UnboundLocalError: local variable 'img' referenced before assignment`.

### 🔍 Nguyên nhân gốc rễ (Root Cause):
- Biến `img` được định nghĩa bên trong khối `if` kiểm tra invalidation cache, khi chạy chế độ rerun force thì luồng bỏ qua việc khởi tạo biến `img`. Đột nhiên lệnh check đường dẫn ảnh ở bước tiếp theo không tìm thấy biến `img`.
- Tham số tỷ lệ khung hình `final_aspect` bị bỏ quên không truyền vào hàm `video_engine.generate_image`.

### 🛠️ Giải pháp khắc phục (Fix):
- Khai báo rõ ràng `img = project_dir / f"images/image{idx}.jpg"` ở đầu hàm callback trước mọi kiểm tra điều kiện.
- Truyền đầy đủ `final_aspect` vào tất cả các điểm gọi `generate_image`.

---

## 4. Bug 4: Lỗi Trùng Lặp Thư Mục Dự Án Ngầm (`~/.taka-agent/projects/projects/`)

### 🔴 Hiện tượng:
- Dự án bị tạo lồng nhau thành `~/.taka-agent/projects/projects/dự-án-x`, dẫn tới việc đọc ghi file kịch bản không đồng bộ giữa Web UI và Agent.

### 🔍 Nguyên nhân gốc rễ (Root Cause):
- Khi ghép đường dẫn `pathlib.Path`, code cũ thực hiện nối chuỗi thư mục gốc `projects` hai lần.

### 🛠️ Giải pháp khắc phục (Fix):
- Đảm bảo kiến trúc Local-First chuẩn phẳng:
  `~/.taka-agent/projects/<project-slug>/<episode-slug>`
- Tự động xóa thư mục trùng lặp ngầm và cập nhật quy tắc ghi đè trong `taka_agent.py`.

---

## 5. Bug 5: Chênh Lệch Tiến Độ Giữa `ima2` Proxy (3333) Và Taka Web UI (8080)

### 🔴 Hiện tượng:
- Trên giao diện proxy `ima2` (port 3333) báo đã tạo xong 4 ảnh, nhưng Taka Web UI mới chỉ báo xong 2 ảnh.

### 🔍 Nguyên nhân gốc rễ (Root Cause):
- Không phải do lỗi hệ thống. Đây là kết quả của **Bộ khóa tuần tự `Semaphore(1)`** và **Quy trình Hậu Kỳ 5 bước** trong Taka:
  1. `Semaphore(1)` bắt buộc Taka xử lý tuần tự từng ảnh một để tránh đụng trần Rate Limit OpenAI.
  2. Sau khi `ima2` tải file thô về, Taka thực hiện hậu kỳ PIL (Chuyển 24-bit sRGB $\rightarrow$ Resize Lanczos $\rightarrow$ Nén JPEG 95% $\rightarrow$ Xóa temp).
  3. Sau khi hậu kỳ hoàn tất 100%, Taka mới gửi sự kiện WebSocket báo Web UI cập nhật.

---

## 🔐 Quy Trình Đóng Gói & Deploy Khi Sửa Code Backend

Mỗi khi chỉnh sửa file trong thư mục làm việc (`taka-tales/`), bạn cần thực hiện các bước sau để đảm bảo daemon local cập nhật mã nguồn mới nhất:

```bash
# 1. Commit và Push mã nguồn
git add .
git commit -m "fix: mô tả nội dung sửa lỗi"
git push origin main

# 2. Chạy script đồng bộ sang ~/.taka-agent/ và Restart Daemon
./env/bin/python -c "
import os, signal, subprocess, pathlib, shutil, time

ws_dir = pathlib.Path('/Users/huutq/Desktop/WorkingSpace/Taka/taka-tales')
home_agent_dir = pathlib.Path.home() / '.taka-agent'

# Terminate existing processes
res = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
for line in res.stdout.splitlines():
    if ('taka_agent.py' in line or 'taka_server.py' in line) and 'grep' not in line:
        try: os.kill(int(line.split()[1]), signal.SIGTERM)
        except Exception: pass

time.sleep(1)

# Sync files
for item in ['taka_agent.py', 'taka_server.py', 'core', 'presets', 'config.ini']:
    src, dst = ws_dir / item, home_agent_dir / item
    if src.exists():
        if src.is_dir():
            if dst.exists(): shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else: shutil.copy2(src, dst)

# Spawn new processes
env_python = ws_dir / 'env' / 'bin' / 'python'
subprocess.Popen([str(env_python), 'taka_server.py'], cwd=str(ws_dir))
subprocess.Popen([str(env_python), '-u', str(home_agent_dir / 'taka_agent.py')], cwd=str(home_agent_dir))
print('Restarted taka_server and taka_agent daemons successfully!')
"
```

---
*Tài liệu được khởi tạo và duy trì tự động bởi Antigravity Engine cho dự án TakaTales.*
