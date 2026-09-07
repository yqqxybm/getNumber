"""Strict normalization for short, spoken input codes.

The parser intentionally accepts only payloads it can account for in full.  It
does not search a transcript for something that merely looks like a code.
"""

import re
import unicodedata


_RAW_MAX_LENGTH = 256
_OUTPUT_MAX_LENGTH = 64

_FRAMING = ("口令是", "数字是", "请输入", "答案是", "输入")
_TERMINAL_PUNCTUATION = "。！？!?；;,，"
_INLINE_SEPARATORS = frozenset(",，、")
_CORRECTION_OR_NEGATION = (
    "不是",
    "不对",
    "不要",
    "别输入",
    "取消",
    "更正",
    "改成",
    "改为",
    "应该是",
    "其实是",
    "或者",
    "还是",
)

_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "幺": 1,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CANONICAL_DIGITS = "零一二三四五六七八九"
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_NUMBER_CHARACTERS = frozenset(_CHINESE_DIGITS) | frozenset("十百千万")

# Only conventional multi-syllable names are accepted.  Common filler words
# such as 啊、爱、嗯 must never become letters merely because they sound alike.
_LETTER_ALIASES = {
    "达不溜": "w",
    "艾克斯": "x",
    "贼德": "z",
    "阿尔": "r",
    "艾尔": "r",
    "艾弗": "f",
    "艾尺": "h",
    "艾勒": "l",
    "艾姆": "m",
    "艾斯": "s",
}
_SORTED_ALIASES = tuple(sorted(_LETTER_ALIASES, key=len, reverse=True))
_SPOKEN_SYMBOLS = {"下划线": "_", "小数点": ".", "横杠": "-", "负号": "-"}
_SORTED_SYMBOLS = tuple(sorted(_SPOKEN_SYMBOLS, key=len, reverse=True))

_ALPHANUMERIC_RE = re.compile(r"^[0-9A-Za-z_.-]+$")
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

_REASON_TEXT = {
    "ok": "已接受",
    "invalid_text_type": "输入必须是文本",
    "invalid_mode": "不支持的输入模式",
    "invalid_length_bounds": "长度设置无效",
    "raw_too_long": "原始文本过长",
    "correction_or_negation": "检测到否定或更正表达",
    "empty_payload": "未检测到口令内容",
    "ambiguous_separator": "数字分隔方式有歧义",
    "unrecognized_payload": "包含无法识别的内容",
    "ambiguous_number": "中文数字表达有歧义",
    "output_too_short": "规范化结果过短",
    "output_too_long": "规范化结果过长",
    "invalid_numeric_value": "结果不是有效数字",
    "invalid_alphanumeric_value": "结果包含不允许的字符",
}

_CHANGE_TEXT = {
    "numbers_mode_normalized": "已将 numbers 模式按 numeric 处理",
    "unicode_normalized": "已统一全角字符",
    "outer_whitespace_removed": "已移除首尾空白",
    "terminal_punctuation_removed": "已移除句末标点",
    "framing_removed": "已移除口令提示语",
    "separators_removed": "已移除字符间分隔符",
    "explicit_case_applied": "已应用指定大小写",
    "spoken_letters_converted": "已转换字母读音",
    "chinese_number_converted": "已转换中文数字",
    "spoken_symbols_converted": "已转换口述符号",
    "repetition_expanded": "已展开重复表达",
    "case_normalized": "已将未指定大小写的字母转为小写",
}


def _result(accepted, value, reason, changes):
    return {
        "accepted": accepted,
        "value": value,
        "reason": _REASON_TEXT.get(reason, reason),
        "changes": list(changes),
    }


def _add_change(changes, change):
    display = _CHANGE_TEXT.get(change, change)
    if display not in changes:
        changes.append(display)


def _render_small_number(number, omit_leading_one_for_ten):
    if number == 0:
        return "零"

    pieces = []
    zero_pending = False
    for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, number = divmod(number, divisor)
        if digit:
            if zero_pending:
                pieces.append("零")
            if not (
                divisor == 10
                and digit == 1
                and omit_leading_one_for_ten
                and not pieces
            ):
                pieces.append(_CANONICAL_DIGITS[digit])
            pieces.append(unit)
            zero_pending = False
        elif pieces and number:
            zero_pending = True
    return "".join(pieces)


def _render_chinese_number(number):
    if number < 10000:
        return _render_small_number(number, True)

    high, low = divmod(number, 10000)
    rendered = _render_small_number(high, True) + "万"
    if low:
        if low < 1000:
            rendered += "零"
        rendered += _render_small_number(low, False)
    return rendered


def _parse_positional_number(source):
    """Return an integer only when *source* is an unambiguous numeral."""
    normalized = "".join(
        _CANONICAL_DIGITS[_CHINESE_DIGITS[char]] if char in _CHINESE_DIGITS else char
        for char in source
    )
    if normalized.count("万") > 1:
        return None

    total = 0
    section = 0
    pending_digit = None
    zero_pending = False
    last_small_unit = 10000
    seen_wan = False

    for char in normalized:
        if char in _CANONICAL_DIGITS:
            digit = _CANONICAL_DIGITS.index(char)
            if digit == 0:
                if zero_pending or pending_digit is not None:
                    return None
                zero_pending = True
            else:
                if pending_digit is not None:
                    return None
                pending_digit = digit
            continue

        if char in _SMALL_UNITS:
            unit = _SMALL_UNITS[char]
            if unit >= last_small_unit:
                return None
            if pending_digit is None:
                if unit != 10 or section:
                    return None
                pending_digit = 1
            section += pending_digit * unit
            pending_digit = None
            zero_pending = False
            last_small_unit = unit
            continue

        if char == "万":
            if seen_wan or zero_pending:
                return None
            section += pending_digit or 0
            if section == 0:
                return None
            total = section * 10000
            section = 0
            pending_digit = None
            zero_pending = False
            last_small_unit = 10000
            seen_wan = True
            continue

        return None

    if zero_pending and pending_digit is None:
        return None
    value = total + section + (pending_digit or 0)
    if value <= 0 or value >= 100000000:
        return None

    canonical = _render_chinese_number(value)
    if normalized == canonical:
        return value
    # Speech recognizers sometimes preserve the explicit leading "一" in
    # 一十/一十二; that form is still numerically unambiguous.
    if normalized.startswith("一十") and normalized[1:] == canonical:
        return value
    return None


def _parse_chinese_number(source):
    if not any(char in _SMALL_UNITS or char == "万" for char in source):
        return "".join(str(_CHINESE_DIGITS[char]) for char in source)
    value = _parse_positional_number(source)
    return None if value is None else str(value)


def _match_letter(text, index):
    char = text[index]
    if char.isascii() and char.isalpha():
        return char.lower(), index + 1, False
    for alias in _SORTED_ALIASES:
        if text.startswith(alias, index):
            return _LETTER_ALIASES[alias], index + len(alias), True
    return None


def _is_inline_separator(char):
    return char.isspace() or char in _INLINE_SEPARATORS


def _ascii_digit_run_length(text, index, step):
    length = 0
    while 0 <= index < len(text) and text[index].isascii() and text[index].isdigit():
        length += 1
        index += step
    return length


def _remove_safe_separators(text, changes, mode, framed):
    output = []
    index = 0
    while index < len(text):
        if not _is_inline_separator(text[index]):
            output.append(text[index])
            index += 1
            continue

        start = index
        while index < len(text) and _is_inline_separator(text[index]):
            index += 1
        previous = text[start - 1] if start else ""
        following = text[index] if index < len(text) else ""
        if not previous or not following:
            return None

        if (
            mode == "numeric"
            and not framed
            and previous.isascii()
            and previous.isdigit()
            and following.isascii()
            and following.isdigit()
        ):
            left_length = _ascii_digit_run_length(text, start - 1, -1)
            right_length = _ascii_digit_run_length(text, index, 1)
            character_sequence = left_length == right_length == 1
            thousands_group = 1 <= left_length <= 3 and right_length == 3
            if not character_sequence and not thousands_group:
                return None
        _add_change(changes, "separators_removed")
    return "".join(output)


def _parse_payload(text, changes):
    output = []
    index = 0
    while index < len(text):
        # A repetition count is intentionally a single digit followed by 个.
        count = None
        count_end = index
        if text[index].isascii() and text[index] in "123456789":
            count = int(text[index])
            count_end += 1
        elif text[index] in _CHINESE_DIGITS and 1 <= _CHINESE_DIGITS[text[index]] <= 9:
            count = _CHINESE_DIGITS[text[index]]
            count_end += 1

        if count is not None and text.startswith("个", count_end):
            atom_index = count_end + 1
            uppercase = None
            if text.startswith("大写", atom_index):
                uppercase = True
                atom_index += 2
            elif text.startswith("小写", atom_index):
                uppercase = False
                atom_index += 2
            letter = _match_letter(text, atom_index) if atom_index < len(text) else None
            if letter is not None:
                value, atom_end, spoken = letter
                if uppercase is not None:
                    value = value.upper() if uppercase else value.lower()
                    _add_change(changes, "explicit_case_applied")
                if spoken:
                    _add_change(changes, "spoken_letters_converted")
                elif uppercase is None and text[atom_index] != value:
                    _add_change(changes, "case_normalized")
            elif uppercase is not None or atom_index >= len(text):
                return None, "unrecognized_payload"
            elif text[atom_index].isascii() and text[atom_index] in "0123456789_.-":
                value = text[atom_index]
                atom_end = atom_index + 1
            elif text[atom_index] in _CHINESE_DIGITS:
                value = str(_CHINESE_DIGITS[text[atom_index]])
                atom_end = atom_index + 1
                _add_change(changes, "chinese_number_converted")
            else:
                value = None
                atom_end = atom_index
                for symbol in _SORTED_SYMBOLS:
                    if text.startswith(symbol, atom_index):
                        value = _SPOKEN_SYMBOLS[symbol]
                        atom_end += len(symbol)
                        _add_change(changes, "spoken_symbols_converted")
                        break
                if value is None:
                    return None, "unrecognized_payload"
            output.append(value * count)
            _add_change(changes, "repetition_expanded")
            index = atom_end
            continue

        uppercase = None
        if text.startswith("大写", index):
            uppercase = True
            index += 2
        elif text.startswith("小写", index):
            uppercase = False
            index += 2
        if uppercase is not None:
            if index >= len(text):
                return None, "unrecognized_payload"
            letter = _match_letter(text, index)
            if letter is None:
                return None, "unrecognized_payload"
            value, index, spoken = letter
            value = value.upper() if uppercase else value.lower()
            output.append(value)
            _add_change(changes, "explicit_case_applied")
            if spoken:
                _add_change(changes, "spoken_letters_converted")
            continue

        symbol_matched = False
        for symbol in _SORTED_SYMBOLS:
            if text.startswith(symbol, index):
                output.append(_SPOKEN_SYMBOLS[symbol])
                index += len(symbol)
                _add_change(changes, "spoken_symbols_converted")
                symbol_matched = True
                break
        if symbol_matched:
            continue

        letter = _match_letter(text, index)
        if letter is not None:
            value, index, spoken = letter
            output.append(value)
            if spoken:
                _add_change(changes, "spoken_letters_converted")
            elif text[index - 1] != value:
                _add_change(changes, "case_normalized")
            continue

        if text[index] in _NUMBER_CHARACTERS:
            end = index + 1
            while end < len(text) and text[end] in _NUMBER_CHARACTERS:
                if (
                    text[end] in _CHINESE_DIGITS
                    and 1 <= _CHINESE_DIGITS[text[end]] <= 9
                    and text.startswith("个", end + 1)
                ):
                    break
                end += 1
            converted = _parse_chinese_number(text[index:end])
            if converted is None:
                return None, "ambiguous_number"
            output.append(converted)
            _add_change(changes, "chinese_number_converted")
            index = end
            continue

        char = text[index]
        if char.isascii() and (char.isdigit() or char in "_.-"):
            output.append(char)
            index += 1
            continue
        return None, "unrecognized_payload"

    return "".join(output), "ok"


def normalize(
    text: str,
    mode: str = "alphanumeric",
    min_length: int = 1,
    max_length: int = 16,
) -> dict:
    """Normalize a short ASR payload into a conservatively parsed input code.

    The return shape is stable for both accepted and rejected input.  Rejected
    results always carry an empty value so callers cannot accidentally type a
    partial parse.
    """
    changes = []
    if not isinstance(text, str):
        return _result(False, "", "invalid_text_type", changes)
    if mode == "numbers":
        mode = "numeric"
        _add_change(changes, "numbers_mode_normalized")
    if mode not in ("alphanumeric", "numeric"):
        return _result(False, "", "invalid_mode", changes)
    if (
        isinstance(min_length, bool)
        or isinstance(max_length, bool)
        or not isinstance(min_length, int)
        or not isinstance(max_length, int)
        or min_length < 1
        or max_length < min_length
        or max_length > _OUTPUT_MAX_LENGTH
    ):
        return _result(False, "", "invalid_length_bounds", changes)
    if len(text) > _RAW_MAX_LENGTH:
        return _result(False, "", "raw_too_long", changes)

    normalized = unicodedata.normalize("NFKC", text)
    if normalized != text:
        _add_change(changes, "unicode_normalized")

    stripped = normalized.strip()
    if stripped != normalized:
        _add_change(changes, "outer_whitespace_removed")
    normalized = stripped
    if any(marker in normalized for marker in _CORRECTION_OR_NEGATION):
        return _result(False, "", "correction_or_negation", changes)

    without_punctuation = normalized.rstrip(_TERMINAL_PUNCTUATION)
    if without_punctuation != normalized:
        _add_change(changes, "terminal_punctuation_removed")
    normalized = without_punctuation.rstrip()

    framed = False
    for prefix in _FRAMING:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            _add_change(changes, "framing_removed")
            framed = True
            break

    normalized = normalized.strip()
    if not normalized:
        return _result(False, "", "empty_payload", changes)

    normalized = _remove_safe_separators(normalized, changes, mode, framed)
    if normalized is None:
        return _result(False, "", "ambiguous_separator", changes)

    value, reason = _parse_payload(normalized, changes)
    if value is None:
        return _result(False, "", reason, changes)
    if len(value) < min_length:
        return _result(False, "", "output_too_short", changes)
    if len(value) > max_length:
        return _result(False, "", "output_too_long", changes)

    if mode == "numeric":
        if not _NUMERIC_RE.fullmatch(value):
            return _result(False, "", "invalid_numeric_value", changes)
    elif not _ALPHANUMERIC_RE.fullmatch(value):
        return _result(False, "", "invalid_alphanumeric_value", changes)

    return _result(True, value, "ok", changes)
