import unittest
from core.text_formatter import format_for_voice, normalize_units_for_tts


class TestTextFormatterUnits(unittest.TestCase):

    def test_vietnamese_units(self):
        text = "Tăng trưởng 50% năm nay. Giá $100 hoặc 500000đ. Tốc độ 120km/h ở 37°C."
        result = format_for_voice(text, language="vi")
        self.assertIn("năm mươi phần trăm", result)
        self.assertIn("một trăm đô-la", result)
        self.assertIn("năm trăm nghìn đồng", result)
        self.assertIn("một trăm hai mươi ki-lô-mét trên giờ", result)
        self.assertIn("ba mươi bảy độ C", result)

    def test_english_units(self):
        text = "Growth is 50% this year. Price is $100. Speed is 120km/h at 37°C."
        result = format_for_voice(text, language="en")
        self.assertIn("50 percent", result)
        self.assertIn("100 dollars", result)
        self.assertIn("120 kilometers per hour", result)
        self.assertIn("37 degrees Celsius", result)

    def test_french_units(self):
        text = "Taux de 50%. Prix de 100€. Vitesse 120km/h."
        result = format_for_voice(text, language="fr")
        self.assertIn("50 pour cent", result)
        self.assertIn("100 euros", result)
        self.assertIn("120 kilomètres par heure", result)

    def test_spanish_units(self):
        text = "Tasa del 50%. Precio de $100. Velocidad 120km/h a 37°C."
        result = format_for_voice(text, language="es")
        self.assertIn("50 por ciento", result)
        self.assertIn("100 dólares", result)
        self.assertIn("120 kilómetros por hora", result)
        self.assertIn("37 grados Celsius", result)

    def test_japanese_units(self):
        text = "成長率は50%です。120km/hで37°Cです。"
        result = format_for_voice(text, language="ja")
        self.assertIn("50 パーセント", result)
        self.assertIn("120 時速キロ", result)
        self.assertIn("37 度", result)

    def test_standalone_normalize_units(self):
        text = "Memory 16GB, Storage 1TB, weight 50kg, freq 50Hz."
        result = normalize_units_for_tts(text, language="en")
        self.assertIn("16 gigabytes", result)
        self.assertIn("1 terabytes", result)
        self.assertIn("50 kilograms", result)
        self.assertIn("50 Hertz", result)


if __name__ == "__main__":
    unittest.main()
