# Kiến trúc Subtitle Engine độc lập

## 1. Mục tiêu

Xây dựng một Subtitle Engine có khả năng:

- Nhận video hoặc audio.
- Nhận transcript có sẵn hoặc tự nhận diện lời thoại.
- Tạo word-level timeline.
- Tự động chia subtitle theo ngữ nghĩa và nhịp đọc.
- Áp nhiều phong cách subtitle khác nhau.
- Hỗ trợ highlight từng từ.
- Hỗ trợ animation như pop, scale, fade, bounce và karaoke.
- Render hoàn toàn tự động bằng CLI.
- Xử lý hàng loạt.
- Không phụ thuộc CapCut.
- Có thể thay renderer trong tương lai mà không thay đổi toàn bộ hệ thống.

Engine được thiết kế như một nền tảng có thể dùng lại cho:

- TikTok.
- YouTube Shorts.
- Facebook Reels.
- Podcast video.
- Audiobook.
- Video AI.
- Video quảng cáo.
- Video kể chuyện.
- Video giáo dục.

---

# 2. Nguyên tắc kiến trúc

Subtitle Engine được chia thành các lớp độc lập:

```text
Input
  ↓
Speech Alignment
  ↓
Transcript Reconciliation
  ↓
Caption Segmentation
  ↓
Layout Engine
  ↓
Style Engine
  ↓
Animation Engine
  ↓
Render Scene
  ↓
Renderer Adapter
  ↓
Output Video
```

Mỗi lớp chỉ đảm nhiệm một nhiệm vụ.

Ví dụ:

- WhisperX chỉ phụ trách timestamp.
- Caption Engine chỉ phụ trách chia cụm.
- Layout Engine chỉ phụ trách vị trí và xuống dòng.
- Animation Engine chỉ phụ trách chuyển động.
- Renderer chỉ phụ trách xuất hình ảnh hoặc video.

Cách chia này giúp thay thế từng thành phần mà không ảnh hưởng toàn bộ pipeline.

---

# 3. Kiến trúc tổng thể

```text
                    ┌─────────────────────┐
                    │     Input Layer     │
                    │ Video / Audio / TXT │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │    Media Service    │
                    │ FFmpeg / FFprobe    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Alignment Provider  │
                    │ WhisperX / Others   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Transcript Resolver │
                    │ Script + ASR merge  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Caption Engine    │
                    │ Segment / Grouping  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │    Layout Engine    │
                    │ Wrap / Safe Area    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     Style Engine    │
                    │ Font / Color / Box  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Animation Engine   │
                    │ Pop / Fade / Scale  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Render Scene Model  │
                    │ Renderer-neutral IR │
                    └──────────┬──────────┘
                               │
               ┌───────────────┼────────────────┐
               ▼               ▼                ▼
       ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
       │ ASS Renderer │ │ SVG Renderer │ │ Web Renderer │
       │ FFmpeg/libass│ │ FFmpeg       │ │ Remotion     │
       └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │   Output Validator  │
                    └──────────┬──────────┘
                               │
                               ▼
                          output.mp4
```

---

# 4. Thành phần hệ thống

## 4.1. Input Layer

Input Layer tiếp nhận:

```text
video.mp4
audio.wav
transcript.txt
job.json
style-preset.json
```

Ví dụ job:

```json
{
  "id": "job-001",
  "video": "input/video-001.mp4",
  "transcript": "input/video-001.txt",
  "language": "vi",
  "preset": "viral-bold-yellow",
  "renderer": "ass",
  "output": "output/video-001.mp4"
}
```

Input Layer thực hiện:

- Kiểm tra file tồn tại.
- Kiểm tra video có audio.
- Đọc kích thước video.
- Đọc frame rate.
- Kiểm tra transcript.
- Xác định ngôn ngữ.
- Tạo workspace riêng cho job.

---

## 4.2. Media Service

Sử dụng:

- FFmpeg.
- FFprobe.

Nhiệm vụ:

- Tách audio.
- Chuẩn hóa audio.
- Đọc metadata.
- Chuẩn hóa frame rate.
- Scale hoặc crop video nếu cần.
- Tạo proxy video cho preview.
- Trích waveform nếu xây editor.

Audio chuẩn:

```text
WAV
PCM 16-bit
Mono
16 kHz
```

Ví dụ:

```bash
ffmpeg -i input.mp4 \
  -vn \
  -ac 1 \
  -ar 16000 \
  -c:a pcm_s16le \
  workspace/audio.wav
```

---

## 4.3. Alignment Provider

Alignment Provider là interface chung cho các engine tạo timestamp.

```python
class AlignmentProvider:
    def align(
        self,
        audio_path: str,
        transcript: str | None,
        language: str
    ) -> "AlignmentResult":
        ...
```

Provider đầu tiên:

```text
WhisperXAlignmentProvider
```

Provider có thể bổ sung:

```text
StableTsAlignmentProvider
MFAAlignmentProvider
CloudAlignmentProvider
```

Đầu ra chuẩn:

```json
{
  "language": "vi",
  "duration": 12.4,
  "words": [
    {
      "id": "w001",
      "text": "Xin",
      "start": 0.52,
      "end": 0.78,
      "confidence": 0.97
    }
  ]
}
```

Tất cả provider phải trả về cùng một schema.

---

## 4.4. Transcript Resolver

Transcript Resolver kết hợp:

- Transcript gốc.
- Transcript từ speech recognition.
- Word-level timestamp.

Mục tiêu:

- Giữ đúng nội dung của script gốc.
- Lấy timeline từ alignment.
- Sửa lỗi nhận diện.
- Phát hiện phần lời thoại bị thiếu hoặc thừa.
- Gắn trạng thái độ tin cậy.

Pipeline:

```text
Script gốc
    +
ASR transcript
    ↓
Normalize
    ↓
Tokenize
    ↓
Sequence Alignment
    ↓
Timestamp Mapping
    ↓
Resolved Transcript
```

Các trường hợp cần xử lý:

- Người nói bỏ một từ.
- Người nói thêm từ.
- Cách đọc khác chính tả.
- Số được đọc thành chữ.
- Tên riêng bị nhận sai.
- Câu bị lặp.
- Khoảng im lặng dài.

Đầu ra:

```json
{
  "words": [
    {
      "text": "video",
      "spoken_text": "vi deo",
      "start": 2.1,
      "end": 2.6,
      "status": "mapped",
      "confidence": 0.91
    }
  ]
}
```

---

# 5. Caption Engine

Caption Engine chuyển danh sách từ thành các caption dễ đọc.

## 5.1. Input

```json
{
  "words": [
    {
      "text": "Xin",
      "start": 0.52,
      "end": 0.78
    }
  ]
}
```

## 5.2. Output

```json
{
  "captions": [
    {
      "id": "caption-001",
      "start": 0.52,
      "end": 2.14,
      "text": "Xin chào các bạn",
      "lines": [
        "Xin chào",
        "các bạn"
      ],
      "words": []
    }
  ]
}
```

## 5.3. Quy tắc chia caption

Mỗi preset có thể định nghĩa:

```json
{
  "min_words": 2,
  "max_words": 6,
  "max_duration_ms": 2500,
  "min_duration_ms": 450,
  "max_chars_per_line": 20,
  "max_lines": 2,
  "break_on_punctuation": true,
  "avoid_single_word_line": true
}
```

Caption Engine cân nhắc:

- Dấu câu.
- Khoảng dừng.
- Số từ.
- Độ dài ký tự.
- Cụm danh từ.
- Tên riêng.
- Nhịp nói.
- Thời lượng hiển thị.
- Tốc độ đọc.

## 5.4. Segmentation Strategy

Interface:

```python
class CaptionSegmenter:
    def segment(
        self,
        words: list["TimedWord"],
        rules: "SegmentationRules"
    ) -> list["Caption"]:
        ...
```

Các strategy:

```text
RuleBasedSegmenter
PauseAwareSegmenter
SemanticSegmenter
HybridSegmenter
```

MVP sử dụng:

```text
HybridSegmenter
```

HybridSegmenter ưu tiên lần lượt:

1. Dấu câu.
2. Khoảng nghỉ.
3. Giới hạn số từ.
4. Giới hạn ký tự.
5. Không tách cụm ngữ nghĩa quan trọng.

---

# 6. Layout Engine

Layout Engine quyết định subtitle được đặt ở đâu và xuống dòng như thế nào.

## 6.1. Trách nhiệm

- Tính kích thước text.
- Chia dòng.
- Căn giữa.
- Giữ subtitle trong safe area.
- Tránh khu vực UI của nền tảng.
- Hỗ trợ nhiều tỉ lệ video.
- Tránh che mặt nhân vật nếu có face detection.
- Tránh che text hoặc logo có sẵn.

## 6.2. Safe Area

Ví dụ video 1080 × 1920:

```json
{
  "canvas": {
    "width": 1080,
    "height": 1920
  },
  "safe_area": {
    "left": 80,
    "right": 80,
    "top": 160,
    "bottom": 320
  }
}
```

Với TikTok và Reels, phần bên phải và phía dưới thường có UI, nên subtitle không nên đặt sát mép.

## 6.3. Layout Strategies

```text
BottomCenterLayout
CenterLayout
DynamicFaceAwareLayout
SpeakerLayout
WordCloudLayout
```

MVP:

```text
BottomCenterLayout
CenterLayout
```

## 6.4. Layout Output

```json
{
  "x": 540,
  "y": 1450,
  "width": 900,
  "height": 220,
  "alignment": "center",
  "line_spacing": 14
}
```

---

# 7. Style Engine

Style Engine quản lý giao diện subtitle bằng preset.

## 7.1. Preset

```json
{
  "id": "viral-bold-yellow",
  "font": {
    "family": "Montserrat ExtraBold",
    "size": 84,
    "weight": 800
  },
  "text": {
    "color": "#FFFFFF",
    "active_color": "#FFD400",
    "inactive_opacity": 1.0
  },
  "outline": {
    "enabled": true,
    "color": "#000000",
    "width": 8
  },
  "shadow": {
    "enabled": true,
    "x": 0,
    "y": 6,
    "blur": 6,
    "color": "#000000AA"
  },
  "background": {
    "enabled": false,
    "color": "#00000099",
    "radius": 18,
    "padding_x": 28,
    "padding_y": 16
  }
}
```

## 7.2. Preset đề xuất

```text
viral-bold-yellow
karaoke-green
minimal-white
podcast-clean
storytelling-serif
kids-colorful
news-lower-third
word-by-word-pop
```

## 7.3. Theme Inheritance

Cho phép một preset kế thừa preset khác:

```json
{
  "id": "viral-bold-red",
  "extends": "viral-bold-yellow",
  "text": {
    "active_color": "#FF3B30"
  }
}
```

---

# 8. Animation Engine

Animation Engine chuyển caption và word timeline thành keyframe.

## 8.1. Animation Model

```json
{
  "enter": {
    "type": "pop",
    "duration_ms": 160
  },
  "word_active": {
    "type": "scale-highlight",
    "scale_from": 1.0,
    "scale_to": 1.12,
    "duration_ms": 100
  },
  "exit": {
    "type": "fade",
    "duration_ms": 100
  }
}
```

## 8.2. Animation Types

MVP:

```text
none
fade
pop
scale
slide-up
karaoke-highlight
word-scale
```

Nâng cao:

```text
bounce
spring
elastic
blur-in
typewriter
shake
rotation
mask-reveal
```

## 8.3. Keyframe Output

```json
{
  "target": "word-003",
  "property": "scale",
  "keyframes": [
    {
      "time": 1.24,
      "value": 1.0
    },
    {
      "time": 1.32,
      "value": 1.12
    },
    {
      "time": 1.49,
      "value": 1.0
    }
  ]
}
```

Animation Engine không sinh trực tiếp cú pháp ASS.

Nó sinh dữ liệu trung gian độc lập renderer.

---

# 9. Render Scene Intermediate Representation

Đây là thành phần cốt lõi giúp engine không phụ thuộc renderer.

Render Scene là một định dạng trung gian mô tả toàn bộ subtitle.

## 9.1. Scene Model

```json
{
  "version": "1.0",
  "canvas": {
    "width": 1080,
    "height": 1920,
    "fps": 30
  },
  "duration": 12.4,
  "layers": [
    {
      "id": "subtitle-layer",
      "type": "caption",
      "items": []
    }
  ]
}
```

## 9.2. Caption Item

```json
{
  "id": "caption-001",
  "start": 0.52,
  "end": 2.14,
  "position": {
    "x": 540,
    "y": 1450
  },
  "style_id": "viral-bold-yellow",
  "words": [
    {
      "id": "word-001",
      "text": "Xin",
      "start": 0.52,
      "end": 0.78,
      "style_state": {
        "default": "inactive",
        "active": "highlight"
      },
      "animations": []
    }
  ]
}
```

Renderer nhận Render Scene và chuyển thành định dạng riêng:

```text
Render Scene
    ├── ASS
    ├── SVG frames
    ├── HTML/Canvas
    ├── Remotion composition
    └── Image sequence
```

---

# 10. Renderer Adapter

## 10.1. Interface

```python
class SubtitleRenderer:
    def render(
        self,
        scene: "RenderScene",
        video_path: str,
        output_path: str
    ) -> "RenderResult":
        ...
```

## 10.2. ASS Renderer

Công nghệ:

```text
Python
ASS
libass
FFmpeg
```

Phù hợp:

- Highlight từng từ.
- Karaoke.
- Pop nhẹ.
- Fade.
- Scale.
- Outline.
- Shadow.
- Batch số lượng lớn.

Ưu điểm:

- Nhẹ.
- Nhanh.
- Chạy CLI.
- Ổn định.
- Ít tài nguyên.

Hạn chế:

- Animation phức tạp khó triển khai.
- Khó mô phỏng spring tự nhiên.
- Không phù hợp motion graphics nâng cao.

## 10.3. SVG Renderer

Pipeline:

```text
Render Scene
    ↓
Python sinh SVG theo frame hoặc theo event
    ↓
FFmpeg overlay
    ↓
MP4
```

Phù hợp:

- Typography đẹp.
- Background bo góc.
- Gradient.
- Mask.
- Layout phức tạp hơn ASS.

## 10.4. Web Renderer

Công nghệ:

```text
React
Remotion
Chromium
FFmpeg
```

Phù hợp:

- Animation nâng cao.
- Spring.
- Bounce.
- Blur.
- Emoji.
- Sticker.
- Motion graphics.
- Template gần CapCut.

Nhược điểm:

- Render chậm hơn.
- Tốn tài nguyên hơn.
- Hệ thống phức tạp hơn.

## 10.5. Renderer Selection

```json
{
  "renderer": "auto",
  "fallback": "ass"
}
```

Chế độ `auto`:

```text
Preset đơn giản
    → ASS Renderer

Preset nâng cao
    → Web Renderer

Web Renderer lỗi
    → ASS Renderer
```

---

# 11. Font Manager

Font Manager quản lý:

- Font đã cài.
- Font đi kèm dự án.
- Font fallback.
- Font hỗ trợ tiếng Việt.
- Font weight.
- Font metrics.

Cấu trúc:

```text
assets/
└── fonts/
    ├── Montserrat-ExtraBold.ttf
    ├── BeVietnamPro-Bold.ttf
    └── NotoSans-Regular.ttf
```

Font Manager kiểm tra:

- Font có hỗ trợ dấu tiếng Việt không.
- Font có weight yêu cầu không.
- Font có được renderer load thành công không.

Fallback:

```text
Montserrat ExtraBold
    ↓
Be Vietnam Pro Bold
    ↓
Noto Sans Bold
```

---

# 12. Emoji và Asset Engine

Asset Engine là module tùy chọn.

Nhiệm vụ:

- Chèn emoji.
- Chèn icon.
- Chèn sticker.
- Chèn logo.
- Quản lý asset theo preset.
- Tạo cache.

Ví dụ:

```json
{
  "emoji": {
    "enabled": true,
    "strategy": "keyword",
    "max_per_caption": 1
  }
}
```

Keyword mapping:

```json
{
  "tiền": "💰",
  "cảnh báo": "⚠️",
  "đúng": "✅",
  "sai": "❌"
}
```

MVP không cần bật module này.

---

# 13. Quality Analyzer

Quality Analyzer đánh giá subtitle trước khi render.

## 13.1. Kiểm tra

- Caption quá dài.
- Tốc độ đọc quá cao.
- Một từ đứng riêng.
- Text vượt safe area.
- Caption bị chồng.
- Timestamp âm.
- Word timestamp vượt caption.
- Khoảng trống bất thường.
- Confidence quá thấp.
- Font thiếu glyph.
- Animation quá ngắn.

## 13.2. Quality Score

```json
{
  "score": 92,
  "warnings": [
    {
      "type": "LOW_CONFIDENCE",
      "caption_id": "caption-008"
    }
  ]
}
```

Có thể đặt ngưỡng:

```text
Score >= 85
    → render

Score < 85
    → đánh dấu cần kiểm tra
```

---

# 14. Preview Engine

Preview Engine sinh video preview độ phân giải thấp.

Ví dụ:

```text
540 × 960
15 fps
CRF 30
```

Mục đích:

- Kiểm tra nhanh subtitle.
- Dùng trong web dashboard.
- Tránh render full video nhiều lần.

Lệnh:

```bash
subtitle-engine preview job.json
```

Output:

```text
preview/job-001.mp4
```

---

# 15. Batch Processing Engine

## 15.1. Job Queue

Trạng thái:

```text
PENDING
VALIDATING
PREPROCESSING
ALIGNING
RESOLVING
SEGMENTING
LAYOUTING
STYLING
ANIMATING
BUILDING_SCENE
RENDERING
VALIDATING_OUTPUT
COMPLETED
FAILED
```

## 15.2. Retry

```json
{
  "max_retries": 3,
  "retry_backoff_seconds": [10, 30, 90]
}
```

## 15.3. Idempotency

Mỗi job có hash:

```text
video hash
+
transcript hash
+
preset hash
+
engine version
```

Nếu hash không đổi, engine có thể sử dụng kết quả cache.

---

# 16. Cache Strategy

Không chạy lại các bước không cần thiết.

```text
audio.wav
alignment.json
resolved-transcript.json
captions.json
render-scene.json
subtitle.ass
preview.mp4
```

Nếu chỉ đổi màu subtitle:

```text
Không chạy lại WhisperX
Không chạy lại Transcript Resolver
Không chạy lại Caption Engine
Chỉ chạy lại Style → Animation → Renderer
```

Nếu chỉ đổi animation:

```text
Chỉ chạy lại Animation Engine và Renderer
```

---

# 17. CLI Design

## 17.1. Xử lý một video

```bash
subtitle-engine process \
  --video input/video.mp4 \
  --transcript input/script.txt \
  --preset viral-bold-yellow \
  --renderer ass \
  --output output/video.mp4
```

## 17.2. Xử lý hàng loạt

```bash
subtitle-engine batch \
  --input storage/input \
  --preset viral-bold-yellow \
  --workers 2
```

## 17.3. Chỉ tạo timeline

```bash
subtitle-engine align \
  --video input/video.mp4 \
  --transcript input/script.txt
```

## 17.4. Chỉ tạo Render Scene

```bash
subtitle-engine build-scene \
  --alignment alignment.json \
  --preset viral-bold-yellow
```

## 17.5. Render lại với style khác

```bash
subtitle-engine render \
  --scene render-scene.json \
  --preset minimal-white \
  --output output/minimal.mp4
```

## 17.6. Preview

```bash
subtitle-engine preview \
  --job job.json
```

## 17.7. Kiểm tra preset

```bash
subtitle-engine validate-preset \
  presets/viral-bold-yellow.json
```

---

# 18. API Design

Có thể bọc engine bằng FastAPI.

## 18.1. Tạo job

```http
POST /v1/jobs
```

Request:

```json
{
  "video_path": "input/video.mp4",
  "transcript_path": "input/script.txt",
  "preset": "viral-bold-yellow",
  "renderer": "auto"
}
```

## 18.2. Trạng thái job

```http
GET /v1/jobs/{job_id}
```

## 18.3. Preview

```http
POST /v1/jobs/{job_id}/preview
```

## 18.4. Render

```http
POST /v1/jobs/{job_id}/render
```

## 18.5. Preset

```http
GET /v1/presets
POST /v1/presets
PUT /v1/presets/{preset_id}
```

---

# 19. Cấu trúc mã nguồn

```text
subtitle-engine/
├── apps/
│   ├── cli/
│   │   └── main.py
│   ├── api/
│   │   └── main.py
│   └── worker/
│       └── main.py
│
├── engine/
│   ├── domain/
│   │   ├── word.py
│   │   ├── caption.py
│   │   ├── style.py
│   │   ├── animation.py
│   │   ├── scene.py
│   │   └── job.py
│   │
│   ├── media/
│   │   ├── ffmpeg.py
│   │   └── ffprobe.py
│   │
│   ├── alignment/
│   │   ├── base.py
│   │   ├── whisperx_provider.py
│   │   └── stable_ts_provider.py
│   │
│   ├── transcript/
│   │   ├── normalizer.py
│   │   ├── tokenizer.py
│   │   └── resolver.py
│   │
│   ├── captions/
│   │   ├── segmenter.py
│   │   ├── pause_detector.py
│   │   └── semantic_rules.py
│   │
│   ├── layout/
│   │   ├── engine.py
│   │   ├── text_measure.py
│   │   └── safe_area.py
│   │
│   ├── styling/
│   │   ├── preset_loader.py
│   │   ├── theme_resolver.py
│   │   └── font_manager.py
│   │
│   ├── animation/
│   │   ├── engine.py
│   │   ├── keyframes.py
│   │   └── easing.py
│   │
│   ├── scene/
│   │   ├── builder.py
│   │   └── serializer.py
│   │
│   ├── renderers/
│   │   ├── base.py
│   │   ├── ass_renderer.py
│   │   ├── svg_renderer.py
│   │   └── remotion_renderer.py
│   │
│   ├── quality/
│   │   ├── analyzer.py
│   │   └── validators.py
│   │
│   └── pipeline/
│       ├── processor.py
│       ├── stages.py
│       └── cache.py
│
├── presets/
│   ├── viral-bold-yellow.json
│   ├── minimal-white.json
│   ├── podcast-clean.json
│   └── storytelling-serif.json
│
├── assets/
│   ├── fonts/
│   ├── emoji/
│   └── stickers/
│
├── storage/
│   ├── input/
│   ├── workspace/
│   ├── cache/
│   ├── previews/
│   ├── output/
│   └── failed/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── scripts/
├── pyproject.toml
├── docker-compose.yml
└── README.md
```

---

# 20. Domain Models

## TimedWord

```python
class TimedWord(BaseModel):
    id: str
    text: str
    start: float
    end: float
    confidence: float | None = None
```

## Caption

```python
class Caption(BaseModel):
    id: str
    start: float
    end: float
    words: list[TimedWord]
    lines: list[str]
```

## StylePreset

```python
class StylePreset(BaseModel):
    id: str
    font: FontStyle
    text: TextStyle
    outline: OutlineStyle
    shadow: ShadowStyle
    layout: LayoutRules
    animation: AnimationPreset
```

## RenderScene

```python
class RenderScene(BaseModel):
    version: str
    canvas: Canvas
    duration: float
    layers: list[Layer]
```

---

# 21. Pipeline Processor

Pseudo-code:

```python
def process_job(job: Job) -> RenderResult:
    workspace = workspace_manager.create(job)

    media = media_service.prepare(
        video_path=job.video_path,
        workspace=workspace
    )

    alignment = alignment_provider.align(
        audio_path=media.audio_path,
        transcript=job.transcript,
        language=job.language
    )

    resolved_words = transcript_resolver.resolve(
        transcript=job.transcript,
        alignment=alignment
    )

    captions = caption_engine.segment(
        words=resolved_words,
        rules=job.preset.segmentation
    )

    layout = layout_engine.layout(
        captions=captions,
        canvas=media.canvas,
        rules=job.preset.layout
    )

    styled_captions = style_engine.apply(
        captions=layout,
        preset=job.preset
    )

    animated_captions = animation_engine.animate(
        captions=styled_captions,
        preset=job.preset.animation
    )

    scene = scene_builder.build(
        canvas=media.canvas,
        captions=animated_captions
    )

    quality_analyzer.validate(scene)

    return renderer.render(
        scene=scene,
        video_path=job.video_path,
        output_path=job.output_path
    )
```

---

# 22. Preset mẫu hoàn chỉnh

```json
{
  "id": "viral-bold-yellow",
  "renderer_hint": "ass",
  "segmentation": {
    "min_words": 2,
    "max_words": 5,
    "min_duration_ms": 450,
    "max_duration_ms": 2200,
    "max_chars_per_line": 18,
    "max_lines": 2,
    "pause_threshold_ms": 320
  },
  "layout": {
    "type": "bottom-center",
    "safe_bottom": 320,
    "max_width_ratio": 0.84,
    "line_spacing": 14
  },
  "font": {
    "family": "Montserrat ExtraBold",
    "size": 84,
    "weight": 800
  },
  "text": {
    "color": "#FFFFFF",
    "active_color": "#FFD400"
  },
  "outline": {
    "enabled": true,
    "color": "#000000",
    "width": 8
  },
  "shadow": {
    "enabled": true,
    "x": 0,
    "y": 5,
    "blur": 4,
    "color": "#000000AA"
  },
  "animation": {
    "caption_enter": {
      "type": "pop",
      "duration_ms": 150
    },
    "word_active": {
      "type": "scale-highlight",
      "scale": 1.12,
      "duration_ms": 100
    },
    "caption_exit": {
      "type": "fade",
      "duration_ms": 100
    }
  }
}
```

---

# 23. Chiến lược render hiệu ứng từng từ

## Phương án 1: ASS Karaoke

Phù hợp:

- Highlight màu.
- Karaoke.
- Scale nhẹ.
- Fade.
- Batch lớn.

Ví dụ:

```ass
{\k26}Xin {\k31}chào {\k25}các {\k63}bạn
```

## Phương án 2: ASS Multiple Events

Mỗi trạng thái highlight là một event riêng.

Phù hợp:

- Đổi màu từng từ.
- Scale từ đang đọc.
- Giữ nguyên bố cục câu.

Nhược điểm:

- Nhiều event.
- File ASS lớn hơn.

## Phương án 3: SVG Overlay

Mỗi caption là một SVG có style chính xác.

Phù hợp:

- Bo góc.
- Gradient.
- Shadow đẹp.
- Typography nâng cao.

## Phương án 4: Remotion

Phù hợp:

- Spring.
- Bounce.
- Emoji animation.
- Motion graphics.
- Template cao cấp.

Khuyến nghị:

```text
MVP
    → ASS Multiple Events

Phiên bản nâng cao
    → Remotion Renderer
```

---

# 24. MVP đề xuất

MVP không nên làm tất cả ngay từ đầu.

## MVP 1

Bao gồm:

- FFmpeg Media Service.
- WhisperX Provider.
- Transcript Resolver.
- Rule-based Caption Engine.
- Bottom Center Layout.
- JSON Preset.
- ASS Renderer.
- CLI.
- Batch folder.
- Cache.
- Output validation.

Chưa cần:

- Web dashboard.
- Remotion.
- Face detection.
- Emoji AI.
- Semantic segmentation bằng LLM.
- Multi-speaker.

## MVP 2

Bổ sung:

- Preview.
- Web API.
- Preset editor.
- Multiple renderer.
- SVG.
- Quality score.

## MVP 3

Bổ sung:

- Remotion.
- Spring animation.
- Face-aware layout.
- Emoji.
- Auto B-roll cue.
- Social publishing.

---

# 25. Cấu hình chạy trên MacBook Apple Silicon

Khuyến nghị ban đầu:

```text
FFmpeg workers: 2
WhisperX workers: 1
Render workers: 1 hoặc 2
Batch queue: 20 jobs
```

Nên tách:

```text
Alignment Queue
Render Queue
```

Vì alignment và render sử dụng tài nguyên khác nhau.

Nếu ASS Renderer:

- Có thể chạy nhiều render song song hơn.
- Giới hạn theo CPU và nhiệt độ máy.

Nếu Remotion Renderer:

- Nên giới hạn Chromium worker.
- Có thể cần 1 đến 2 worker.

---

# 26. Logging và Monitoring

Mỗi stage ghi:

```json
{
  "job_id": "job-001",
  "stage": "ALIGNING",
  "started_at": "2026-07-23T10:00:00+07:00",
  "duration_ms": 13420,
  "status": "COMPLETED"
}
```

Metrics:

- Thời gian alignment.
- Thời gian render.
- Real-time factor.
- Số job thành công.
- Số job lỗi.
- Cache hit rate.
- Subtitle quality score.
- Số caption mỗi phút video.

---

# 27. Test Strategy

## Unit Test

- Normalize transcript.
- Token matching.
- Caption grouping.
- Line wrapping.
- Safe area.
- Preset inheritance.
- Animation keyframes.
- ASS escaping.

## Integration Test

- Video + transcript → word-level JSON.
- Word-level JSON → Render Scene.
- Render Scene → ASS.
- ASS + video → output MP4.

## Golden Test

Lưu ảnh hoặc frame chuẩn:

```text
tests/golden/
├── viral-bold-yellow.png
├── minimal-white.png
└── podcast-clean.png
```

Render frame và so sánh với ảnh chuẩn để phát hiện lỗi layout hoặc style.

---

# 28. Kết luận

Kiến trúc cuối cùng:

```text
Video + Transcript
        ↓
Media Service
        ↓
Alignment Provider
        ↓
Transcript Resolver
        ↓
Caption Engine
        ↓
Layout Engine
        ↓
Style Engine
        ↓
Animation Engine
        ↓
Render Scene
        ↓
Renderer Adapter
        ↓
Final Video
```

Điểm quan trọng nhất là `Render Scene`.

Nó tách logic subtitle khỏi công nghệ render.

Nhờ đó hệ thống có thể:

- Dùng ASS + FFmpeg ở giai đoạn đầu.
- Nâng lên SVG khi cần typography đẹp hơn.
- Nâng lên Remotion khi cần animation cao cấp.
- Giữ nguyên toàn bộ pipeline alignment, caption, layout và preset.

Đây không còn là một script tạo subtitle, mà là một Subtitle Engine có thể mở rộng thành sản phẩm độc lập.
