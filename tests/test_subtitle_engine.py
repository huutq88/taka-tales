import unittest
import pathlib
import json
import shutil
import tempfile

from subtitle_engine.domain import TimedWord, Caption, StylePreset, RenderScene
from subtitle_engine.font_manager import FontManager
from subtitle_engine.transcript_resolver import TranscriptResolver
from subtitle_engine.caption_segmenter import CaptionSegmenter
from subtitle_engine.layout_engine import LayoutEngine
from subtitle_engine.ass_renderer import ASSRenderer
from subtitle_engine.quality_analyzer import QualityAnalyzer
from subtitle_engine.cache import SubtitleCache
from subtitle_engine.emoji_engine import EmojiEngine
from subtitle_engine.speaker_manager import SpeakerManager
from subtitle_engine.svg_renderer import SVGRenderer
from subtitle_engine.processor import SubtitleProcessor


class TestSubtitleEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_transcript_resolver(self):
        resolver = TranscriptResolver()
        aligned = [
            TimedWord(id="w1", text="Xin", start=0.1, end=0.4),
            TimedWord(id="w2", text="chào", start=0.4, end=0.8),
            TimedWord(id="w3", text="bạn", start=0.8, end=1.2)
        ]
        res = resolver.resolve("Xin chào bạn!", aligned)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0].text, "Xin")
        self.assertEqual(res[1].text, "chào")
        self.assertEqual(res[2].text, "bạn!")

    def test_caption_segmenter(self):
        segmenter = CaptionSegmenter()
        words = [
            TimedWord(id="w1", text="Tâm", start=0.1, end=0.4),
            TimedWord(id="w2", text="tĩnh", start=0.4, end=0.8),
            TimedWord(id="w3", text="cảnh", start=0.8, end=1.2),
            TimedWord(id="w4", text="tĩnh", start=1.2, end=1.6)
        ]
        captions = segmenter.segment(words)
        self.assertGreater(len(captions), 0)
        self.assertEqual(captions[0].words[0].text, "Tâm")

    def test_ass_renderer(self):
        renderer = ASSRenderer()
        preset = StylePreset()
        scene = RenderScene(
            duration=3.0,
            captions=[
                Caption(
                    id="c1",
                    start=0.5,
                    end=2.5,
                    text="SỰ BÌNH YÊN",
                    lines=["SỰ BÌNH YÊN"],
                    words=[
                        TimedWord(id="w1", text="Sự", start=0.5, end=1.0),
                        TimedWord(id="w2", text="bình", start=1.0, end=1.8),
                        TimedWord(id="w3", text="yên", start=1.8, end=2.5)
                    ]
                )
            ],
            preset=preset
        )
        ass_path = self.temp_dir / "test.ass"
        out_path = renderer.render_to_file(scene, ass_path)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("[Script Info]", content)
        self.assertIn("[Events]", content)
        self.assertIn(r"\k50", content)

    def test_quality_analyzer(self):
        analyzer = QualityAnalyzer()
        scene = RenderScene(
            duration=3.0,
            captions=[
                Caption(
                    id="c1", start=0.5, end=2.5, text="SỰ BÌNH YÊN",
                    words=[
                        TimedWord(id="w1", text="Sự", start=0.5, end=1.0),
                        TimedWord(id="w2", text="bình", start=1.0, end=1.8),
                        TimedWord(id="w3", text="yên", start=1.8, end=2.5)
                    ]
                )
            ]
        )
        report = analyzer.analyze(scene)
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["metrics"]["total_captions"], 1)

    def test_cache(self):
        cache = SubtitleCache(cache_dir=self.temp_dir)
        key = cache.compute_hash(self.temp_dir, "test transcript", "preset-1")
        self.assertIsNotNone(key)
        cache.set(key, {"status": "ok"})
        retrieved = cache.get(key)
        self.assertEqual(retrieved.get("status"), "ok")

    def test_emoji_engine(self):
        emoji_eng = EmojiEngine()
        cap = Caption(
            id="c1", start=0.0, end=2.0, text="Sự tĩnh lặng mang lại tiền bạc",
            words=[
                TimedWord(id="w1", text="Sự", start=0.0, end=0.4),
                TimedWord(id="w2", text="tĩnh", start=0.4, end=0.8),
                TimedWord(id="w3", text="lặng", start=0.8, end=1.2),
                TimedWord(id="w4", text="tiền", start=1.2, end=1.6)
            ]
        )
        enhanced = emoji_eng.enhance_caption(cap)
        self.assertIn("🕊️", enhanced.text)

    def test_speaker_manager(self):
        spk_mgr = SpeakerManager()
        cap = Caption(id="c1", start=0.0, end=2.0, text="Xin chào quý vị", words=[])
        styled = spk_mgr.apply_speaker_style(cap, "speaker_1")
        self.assertIn("[Nhân Vật 1]:", styled.text)

    def test_svg_renderer(self):
        svg_ren = SVGRenderer()
        preset = StylePreset()
        cap = Caption(id="c1", start=0.0, end=2.0, text="THÀNH CÔNG", words=[])
        svg_xml = svg_ren.generate_caption_svg(cap, preset)
        self.assertIn("<svg", svg_xml)
        self.assertIn("THÀNH CÔNG", svg_xml)

    def test_subtitle_processor(self):
        processor = SubtitleProcessor(preset_path_or_id="viral-bold-yellow")
        self.assertEqual(processor.preset.id, "viral-bold-yellow")


if __name__ == "__main__":
    unittest.main()
