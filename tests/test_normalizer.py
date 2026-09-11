import inspect
import re
import unittest

from backend.normalizer import normalize, normalize_cued


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

    def test_live_prompts_separate_instruction_from_numeric_payload(self):
        cases = {
            "飘一个数字9": "9",
            "飘一个数字九": "9",
            "飘个9": "9",
            "扣一个数字零零八": "008",
            "打个数字一2三": "123",
            "发数字００８": "008",
            "请大家飘一个数字：9吧！": "9",
            "大家，扣个九啊": "9",
            "请 飘 1 个 数字 9": "9",
            "飘1": "1",
            "飘两个9": "99",
            "扣三个八": "888",
            "打数字两个零，八": "008",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, expected)
                self.assertIn("已移除口令提示语", normalize(spoken)["changes"])

    def test_cued_prompts_take_priority_over_sales_preamble_numbers(self):
        cases = {
            "这件29，扣一个00": "00",
            "这件29扣一个00": "00",
            "这件29，飘一个00。": "00",
            "库存还剩18，扣：零0九": "009",
            "倒计时3秒，飘一个数字9": "9",
            "不要错过这件29，扣一个00": "00",
            "红色或者蓝色，飘9": "9",
            "折扣00，扣11": "11",
            "扣除成本29，飘8": "8",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertAccepted(spoken, expected)

    def test_cued_prompts_reject_negation_multiple_targets_and_false_cues(self):
        for spoken in (
            "不要扣00",
            "别飘9",
            "不是扣00",
            "不能扣00",
            "禁止飘9",
            "不要，扣00",
            "这件29，不要扣00",
            "扣00或者11",
            "扣00再飘11",
            "这件29，扣00元",
            "折扣00",
            "纽扣00",
            "抵扣00",
            "回扣00",
            "扣除00",
        ):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)

    def test_normalize_cued_requires_one_complete_kou_or_piao_command(self):
        self.assertEqual(
            ["text", "min_length", "max_length"],
            list(inspect.signature(normalize_cued).parameters),
        )
        for spoken, expected in (
            ("这件29，扣一个00", "00"),
            ("飘一2三", "123"),
        ):
            with self.subTest(spoken=spoken):
                result = normalize_cued(spoken)
                self.assertTrue(result["accepted"], result)
                self.assertEqual(expected, result["value"])
                self.assertEqual(
                    {"accepted", "value", "reason", "changes"}, set(result), result
                )
        for spoken in (
            "0012",
            "数字是0012",
            "打个9",
            "不要扣00",
            "不是扣00",
            "不要，扣00",
            "纽扣00",
            "扣00再飘11",
        ):
            with self.subTest(spoken=spoken):
                result = normalize_cued(spoken)
                self.assertFalse(result["accepted"], result)
                self.assertEqual("", result["value"])

        for spoken in ("0012", "2乘3"):
            with self.subTest(missing_cue=spoken):
                missing_cue = normalize_cued(spoken)
                self.assertEqual("", missing_cue["value"])
                self.assertEqual(
                    "未检测到“扣”或“飘”口令", missing_cue["reason"]
                )

    def test_cued_multiplication_evaluates_two_complete_integer_operands(self):
        cases = {
            "这件30扣一个2乘3": "6",
            "库存18，飘二乘以3": "6",
            "扣 2 × 3": "6",
            "飘4*零": "0",
            "扣0012": "0012",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                result = normalize_cued(spoken)
                self.assertTrue(result["accepted"], result)
                self.assertEqual(expected, result["value"])
                if "乘" in spoken or "×" in spoken or "*" in spoken:
                    self.assertIn("已计算乘法口令", result["changes"])

    def test_cued_multiplication_rejects_incomplete_or_ambiguous_expressions(self):
        for spoken in (
            "扣2乘",
            "扣乘3",
            "扣2乘3乘4",
            "扣2乘以乘3",
            "扣2乘3元",
            "扣2.5乘3",
            "扣-2乘3",
            "扣2乘three",
            "扣two乘3",
            "扣2乘threeX",
            "扣2乘两个12",
            "扣2乘3或者4",
        ):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)
                self.assertFalse(normalize_cued(spoken)["accepted"])
        self.assertRejected("扣2乘3", min_length=2)
        self.assertRejected("扣9乘9", max_length=1)
        self.assertFalse(normalize_cued("扣2乘3", min_length=2)["accepted"])
        self.assertFalse(normalize_cued("扣9乘9", max_length=1)["accepted"])

    def test_live_prompts_do_not_extract_unrelated_or_ambiguous_numbers(self):
        for spoken in (
            "飘一个数字", "飘个", "飘吧", "飘一个数字9元",
            "飘一个数字9.9", "飘一个数字O9", "飘一个数字两个m",
            "不要飘一个数字9", "大家别飘9", "飘9或者8", "飘9改成8",
            "今天有9个人", "飘9再发8",
            "飘一个数字9谢谢", "飘一个数字9可以吗", "飘两个12",
            "飘两个数字9", "飘数字一百二", "9吧",
        ):
            with self.subTest(spoken=spoken):
                self.assertRejected(spoken)
        self.assertRejected("飘一个数字9", min_length=2)
        self.assertRejected("飘一个数字1234", max_length=3)

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
