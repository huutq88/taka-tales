# 🎙️ TAKA-TALES SYSTEM ARCHITECTURE SPECIFICATION
> **Dự án:** Taka-Tales - Hệ thống Sản xuất Video Tự động Thế hệ mới (Taka-Server & Taka-Agent)  
> **Repository:** `git@github.com:huutq88/taka-tales.git`
> **Kiến trúc tham chiếu:** Melorix Hybrid Worker Architecture  
> **Tích hợp mở rộng:** Lore-Keeper Database & API (Tự động lấy nội dung câu chuyện/chương truyện từ Postgres/HTTP API)

---

## 🧭 1. TỔNG QUAN KIẾN TRÚC HYBRID (OVERVIEW)

Hệ thống hoạt động theo mô hình **Client-Server** bất đồng bộ qua **WebSockets**, tích hợp cùng hệ sinh thái **Lore-Keeper** để lấy nội dung chương truyện trực tiếp mà không cần tải lên thủ công:

```mermaid
graph TD
    User([Giao diện Web Dashboard]) <-->|1. Chọn truyện & chương từ Lore-Keeper| Server[Taka-Server: Port 8080]
    
    subgraph Lore-Keeper Cluster Context
        Server <-->|2a. Gọi API lấy Chapter Content| LK_API[Lore-Keeper API: Port 8000]
        Server <-->|2b. Hoặc truy vấn trực tiếp| PG[(Postgres DB)]
    end
    
    Server <-->|3. Gửi Job qua WebSocket| Agent[Taka-Agent Background Worker]
    
    subgraph Taka-Agent Cục bộ (Máy GPU của Client)
        Agent -->|4. Setup & Tạo Giọng nói| OmniVoice[OmniVoice TTS: tools/OmniVoice]
        Agent -->|5. Phân tích lời & Sinh prompt| Ollama[Local Ollama: Port 11434]
        Agent -->|6. Vẽ hình ảnh minh họa| SD[Stable Diffusion WebUI API]
        Agent -->|7. Biên tập & Ghép nhạc| MoviePy[MoviePy Video Engine]
    end
    
    MoviePy -->|8. Xuất file final.mp4| Server
```

---

## 💾 2. TÍCH HỢP LORE-KEEPER & TRUY VẤN CƠ SỞ DỮ LIỆU

Khi deploy cùng cụm (cluster) hoặc mạng nội bộ với `lore-keeper`, `Taka-Server` có thể lấy nội dung văn bản gốc qua 2 phương án:

### Phương án A: Gọi qua API HTTP (Khuyên dùng)
Taka-Server thực hiện request HTTP tới endpoint của `lore-keeper` để lấy nội dung Markdown/Text đầy đủ của chương truyện:
* **Endpoint:** `GET http://lore-keeper:8000/api/chapters/{chapter_id}`
* **Dữ liệu trả về:**
```json
{
  "ok": true,
  "chapter": {
    "id": "chap_1",
    "title": "A Long-expected Party",
    "document_id": "pg_doc_123",
    "content": "Nội dung văn bản chương truyện gốc ở đây..."
  }
}
```

### Phương án B: Kết nối trực tiếp vào Postgres Database
Nếu kết nối trực tiếp trong cùng một mạng Kubernetes/Docker Network, Taka-Server đọc biến môi trường `POSTGRES_URI` của cụm và thực hiện truy vấn SQL:
```sql
SELECT d.content 
FROM agent_documents ad 
JOIN documents d ON ad.document_id = d.id 
WHERE ad.id::text = %s
```
*(Trong đó `%s` là `document_id` hoặc ID tài liệu của chương truyện cần kết xuất).*

---

## 📂 3. CẤU TRÚC THƯ MỤC HỆ THỐNG (FOLDER STRUCTURE)

```text
taka-tales/
├── config.ini                     # File cấu hình chung (Ollama, OmniVoice, Postgres DB)
├── requirements.txt               # FastAPI, Websockets, uvicorn, psycopg2-binary
├── taka_server.py                 # Server điều phối (tích hợp cổng kết nối Lore-Keeper)
├── taka_agent.py                  # Agent xử lý cục bộ trên máy client
├── bg_music/                      # Chứa nhạc nền (.mp3)
├── tools/
│   └── OmniVoice/                 # Tự động clone bởi Agent từ k2-fsa/OmniVoice
└── projects/
    └── [project_name]/            # Tự động đồng bộ hóa nội dung chương truyện tải từ Lore-Keeper
        ├── story.txt              # Text được tải về tự động từ DB/API
        ├── final.mp4              # Sản phẩm video hoàn chỉnh
```

---

## 🛠️ 4. MÃ NGUỒN TÍCH HỢP THAM CHIẾU

### Cập nhật cấu hình DB & API (`config.ini`)
```ini
[LORE_KEEPER]
# Địa chỉ API của Lore-Keeper trong mạng nội bộ
API_URL = http://lore-keeper:8000
# Đường dẫn kết nối Postgres DB chung
POSTGRES_URI = postgresql://user:password@host:port/database
```

### Cập nhật mã nguồn `Taka-Server` (`taka_server.py`) để lấy truyện tự động
```python
import httpx
import psycopg2
import os

LORE_KEEPER_API = os.getenv("LORE_KEEPER_API", "http://lore-keeper:8000")
POSTGRES_URI = os.getenv("POSTGRES_URI")

async def fetch_story_from_lore_keeper(chapter_id: str) -> str:
    """
    Ưu tiên lấy nội dung qua API HTTP của Lore-Keeper.
    Nếu thất bại, tự động chuyển sang query trực tiếp Postgres DB.
    """
    # 1. Thử lấy qua API HTTP
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{LORE_KEEPER_API}/api/chapters/{chapter_id}")
            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and "content" in data["chapter"]:
                    return data["chapter"]["content"]
    except Exception as e:
        print(f"[Server] Không thể gọi API Lore-Keeper: {e}. Thử kết nối DB...")

    # 2. Fallback: Query trực tiếp Postgres
    if POSTGRES_URI:
        try:
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
            print(f"[Server] Kết nối DB Postgres lỗi: {e}")
            
    raise ValueError(f"Không thể lấy nội dung chương truyện {chapter_id}")
```
*(Đoạn code trên tích hợp trực tiếp vào hàm `/v1/projects/{project_name}/run` của `taka_server.py`. Khi kích hoạt, server tự động kéo nội dung từ database lưu vào file `story.txt` của dự án trước khi phát lệnh cho `taka-agent` thực hiện công việc).*
