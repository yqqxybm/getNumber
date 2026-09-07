import unittest

from backend.normalizer import normalize


class NormalizeTests(unittest.TestCase):
    def assertAccepted(self, text, expected, **kwargs):
        result = normalize(text, **kwargs)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(expected, result["value"])
        self.assertEqual("已接受", result["reason"])
        self.assertIsInstance(result["changes"], list)

    def assertRejected(self, text, **kwargs):
        result = normalize(text, **kwargs)
        self.assertFalse(result["accepted"], result)
        self.assertEqual("", result["value"])
        self.assertTrue(result["reason"])
        self.assertIsInstance(result["changes"], list)

    def test_repetition_and_spoken_letters(self):
        for spoken in ("2个m", "两个m", "两个艾姆"):
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, "mm")

    def test_repetition_is_distinct_from_plain_digit_and_letter(self):
        self.assertAccepted("2m", "2m")

    def test_explicit_letter_case(self):
        self.assertAccepted("两个大写a", "AA")
        self.assertAccepted("大写m小写m", "Mm")
        self.assertAccepted("大写艾姆小写艾姆", "Mm")

    def test_default_letter_case_is_lowercase(self):
        result = normalize("ＡbＣ")
        self.assertTrue(result["accepted"], result)
        self.assertEqual("abc", result["value"])
        self.assertIn("已统一全角字符", result["changes"])
        self.assertIn("已将未指定大小写的字母转为小写", result["changes"])

    def test_chinese_digit_sequence_preserves_leading_zeros(self):
        self.assertAccepted("零〇幺二", "0012")
        self.assertAccepted("数字是零零八。", "008")

    def test_unambiguous_positional_numbers(self):
        cases = {
            "十": "10",
            "一十二": "12",
            "二十": "20",
            "一百零二": "102",
            "一千零二十": "1020",
            "一万零一": "10001",
            "十二万三千四百五十六": "123456",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, expected)

    def test_ambiguous_or_malformed_positional_numbers_rejected(self):
        for spoken in ("一百二", "一万二", "一二十", "十百", "百", "一百零二十"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_framing_terminal_punctuation_and_safe_spaces(self):
        self.assertAccepted("口令是 两个 大写 A。", "AA")
        self.assertAccepted("请输入 m 艾姆！", "mm")
        self.assertAccepted("答案是abc?", "abc")

    def test_realistic_mixed_asr_separators(self):
        result = normalize("数字是二五八，两个 M")
        self.assertTrue(result["accepted"], result)
        self.assertEqual("258mm", result["value"])
        self.assertIn("已将未指定大小写的字母转为小写", result["changes"])
        self.assertAccepted("一，二，三", "123")
        self.assertAccepted("一 二 三", "123")
        self.assertAccepted("abc，def", "abcdef")

    def test_spoken_symbols(self):
        self.assertAccepted("a横杠b下划线c小数点d", "a-b_c.d")
        self.assertAccepted("负号m", "-m")

    def test_numeric_mode_allows_signed_decimal(self):
        self.assertAccepted("负号零小数点五", "-0.5", mode="numeric")
        self.assertAccepted("-12.50", "-12.50", mode="numeric")
        result = normalize("数字是 1，2，3", mode="numbers")
        self.assertTrue(result["accepted"], result)
        self.assertEqual("123", result["value"])
        self.assertIn("已将 numbers 模式按 numeric 处理", result["changes"])

    def test_numeric_mode_rejects_non_numbers_and_bad_decimals(self):
        for spoken in ("12m", "1.2.3", "--1", "负号负号一", ".5", "1."):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken, mode="numeric")

    def test_alphanumeric_character_allowlist(self):
        self.assertAccepted("aZ09_.-", "az09_.-")
        for unsafe in ("abc/def", "a@b", "甲"):
            with self.subTest(unsafe=unsafe):
                self.assertRejected(unsafe)

    def test_unrelated_chatter_prices_countdown_and_corrections_rejected(self):
        samples = (
            "今天的口令是123",
            "价格是三块五",
            "倒计时三二一",
            "口令不是123",
            "不对，应该是456",
            "输入123改成456",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertRejected(sample)

    def test_whole_payload_must_parse(self):
        self.assertRejected("口令是abc谢谢")
        self.assertRejected("请帮我输入123")

    def test_numeric_separator_ambiguity_policy(self):
        for spoken in ("1,234", "1，234", "1 234", "1,2,3"):
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, spoken.replace(",", "").replace("，", "").replace(" ", ""), mode="numeric")
        self.assertRejected("12,34", mode="numeric")
        self.assertAccepted("数字是12,34", "1234", mode="numeric")

    def test_filler_words_are_not_letter_aliases(self):
        for filler in ("啊", "嗯", "爱"):
            with self.subTest(filler=filler):
                self.assertRejected(filler)
        self.assertAccepted("阿尔艾姆", "rm")

    def test_repetition_is_bounded_and_not_nested(self):
        self.assertAccepted("九个m", "mmmmmmmmm")
        self.assertAccepted("三个1", "111")
        self.assertAccepted("两个横杠", "--")
        for spoken in ("十个m", "零个m", "两个两个m", "一百个m"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_raw_output_and_setting_bounds(self):
        self.assertRejected("a" * 257)
        self.assertRejected("a", min_length=0)
        self.assertRejected("a", min_length=3, max_length=2)
        self.assertRejected("a", max_length=65)
        self.assertRejected("ab", max_length=1)
        self.assertRejected("a", min_length=2)

    def test_invalid_types_and_modes_return_rejection(self):
        self.assertRejected(None)
        self.assertRejected("1", mode="digits")
        self.assertRejected("1", max_length=True)

    def test_empty_payload_rejected(self):
        for text in ("", "   ", "口令是。"):
            with self.subTest(text=text):
                self.assertRejected(text)

    def test_reprocessing_applies_default_lowercase_policy(self):
        for source in ("两个m", "大写m小写m", "零零一二", "a横杠b"):
            first = normalize(source)
            self.assertTrue(first["accepted"], first)
            second = normalize(first["value"])
            self.assertTrue(second["accepted"], second)
            self.assertEqual(first["value"].lower(), second["value"])


if __name__ == "__main__":
    unittest.main()
