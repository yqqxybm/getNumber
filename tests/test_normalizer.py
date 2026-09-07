import inspect
import re
import unittest

from backend.normalizer import normalize


class NormalizeTests(unittest.TestCase):
    def assertAccepted(self, text, expected, **kwargs):
        result = normalize(text, **kwargs)
        self.assertEqual(
            {"accepted", "value", "reason", "changes"}, set(result), result
        )
        self.assertTrue(result["accepted"], result)
        self.assertEqual(expected, result["value"])
        self.assertEqual("已接受", result["reason"])
        self.assertIsInstance(result["changes"], list)
        self.assertRegex(result["value"], r"^[0-9]+$")

    def assertRejected(self, text, **kwargs):
        result = normalize(text, **kwargs)
        self.assertEqual(
            {"accepted", "value", "reason", "changes"}, set(result), result
        )
        self.assertFalse(result["accepted"], result)
        self.assertEqual("", result["value"])
        self.assertTrue(result["reason"])
        self.assertIsInstance(result["changes"], list)

    def test_public_signature_is_digits_only(self):
        self.assertEqual(
            ["text", "min_length", "max_length"],
            list(inspect.signature(normalize).parameters),
        )

    def test_ascii_and_fullwidth_digits(self):
        self.assertAccepted("00129", "00129")
        result = normalize("１２３０")
        self.assertTrue(result["accepted"], result)
        self.assertEqual("1230", result["value"])
        self.assertIn("已统一全角字符", result["changes"])

    def test_chinese_digit_sequence_preserves_leading_zeros(self):
        self.assertAccepted("零〇幺二两", "00122")
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

    def test_safe_numeric_separators(self):
        for spoken in ("12 34", "12，34", "12,34", "一 二 三", "1、2、3"):
            with self.subTest(spoken=spoken):
                expected = re.sub(r"[\s,，、]", "", spoken)
                if any(character in "一二三" for character in spoken):
                    expected = "123"
                self.assertAccepted(spoken, expected)

    def test_known_frames_optional_colon_and_terminal_punctuation(self):
        cases = {
            "口令是： 123。": "123",
            "数字是:零零八！": "008",
            "请输入 1 2 3": "123",
            "答案是：九八七?": "987",
            "输入：456；": "456",
            "验证码是 2468。": "2468",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, expected)

    def test_repetition_expands_only_one_digit_atom(self):
        cases = {
            "两个零": "00",
            "2个0": "00",
            "三个八": "888",
            "十个零": "0000000000",
            "两个零，八": "008",
            "两个零三个八": "00888",
            "两个零 三个八": "00888",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, expected)

    def test_repetition_count_supports_one_through_thirty_two(self):
        self.assertAccepted("一个九", "9")
        self.assertAccepted("三十二个零", "0" * 32, max_length=32)
        for spoken in ("零个一", "33个0", "三十三个零"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken, max_length=32)

    def test_repetition_rejects_multidigit_or_nested_atoms(self):
        for spoken in ("两个12", "两个一二", "两个零八", "两个三个八"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_english_digit_words_require_whole_word_boundaries(self):
        self.assertAccepted("zero one two nine", "0129")
        self.assertAccepted("数字是: seven, eight", "78")
        for spoken in ("stone", "oneight", "zeroX", "Xone", "none"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_phone_readings_only_survive_an_entirely_numeric_payload(self):
        self.assertAccepted("洞幺拐勾", "0179")
        self.assertAccepted("验证码是 洞 1 拐 9", "0179")
        self.assertAccepted("一个洞", "0")
        for spoken in ("拐弯", "挂勾", "请拐7"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_letters_symbols_and_homophones_are_never_output(self):
        for spoken in (
            "O",
            "I",
            "l",
            "S",
            "abc",
            "12m",
            "两个m",
            "艾姆",
            "a横杠b",
            "-12.5",
            "负号一",
            "一小数点五",
        ):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_chatter_correction_negation_and_units_are_rejected(self):
        for spoken in (
            "今天的口令是123",
            "请帮我输入123",
            "价格是三块五",
            "三块五",
            "12元",
            "倒计时三二一",
            "口令不是123",
            "不对，应该是456",
            "输入123改成456",
            "123或456",
            "123还是456",
        ):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_no_partial_extraction_padding_or_truncation(self):
        for spoken in ("code 123", "123谢谢", "@123", "123/456"):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)
        self.assertRejected("12", min_length=3)
        self.assertRejected("1234", max_length=3)

    def test_length_settings_are_restricted_to_one_through_thirty_two(self):
        self.assertAccepted("1", "1", min_length=1, max_length=32)
        for kwargs in (
            {"min_length": 0},
            {"max_length": 33},
            {"min_length": 3, "max_length": 2},
            {"min_length": True},
            {"max_length": False},
            {"min_length": 1.0},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertRejected("1", **kwargs)

    def test_invalid_types_raw_limit_and_empty_payload_are_rejected(self):
        self.assertRejected(None)
        self.assertRejected("1" * 257)
        for spoken in ("", "   ", "口令是。", "验证码是："):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_reprocessing_is_stable(self):
        for source in ("两个零三个八", "一百零二", "１２３", "zero one"):
            first = normalize(source)
            self.assertTrue(first["accepted"], first)
            second = normalize(first["value"])
            self.assertTrue(second["accepted"], second)
            self.assertEqual(first["value"], second["value"])


if __name__ == "__main__":
    unittest.main()
