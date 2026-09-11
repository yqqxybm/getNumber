"""Conservative normalization for spoken, digits-only input codes.

The parser accepts a transcript only when it can account for the whole numeric
payload. An explicit ``扣`` or ``飘`` command may select that payload after
unrelated livestream chatter; otherwise no numeric-looking substring is
extracted from unknown text.
"""

import re
import unicodedata


_RAW_MAX_LENGTH = 256
_OUTPUT_MAX_LENGTH = 32

_FRAMING = ("口令是", "数字是", "请输入", "答案是", "输入", "验证码是")
# Match instructions only at the start; the remaining payload must still parse
# in full. The singular classifier belongs to the instruction, not the code.
_LIVE_PROMPT_RE = re.compile(
    r"(?:请\s*)?(?:大家\s*[,，]?\s*)?(?:飘|扣(?!除)|打|发)\s*"
    r"(?:[:,，]\s*)?"
    r"(?:(?:一|1)?\s*个\s*)?(?:数字\s*)?"
    r"(?:[:,，]\s*)?"
)
_CUED_PROMPT_RE = re.compile(
    r"(?<![折纽抵回克查])(?:飘|扣(?!除))\s*(?:[:,，]\s*)?"
    r"(?:(?:一|1)?\s*个\s*)?(?:数字\s*)?(?:[:,，]\s*)?"
    r"(?=[0-9零〇幺一二两三四五六七八九十百千万洞拐勾]|"
    r"(?i:zero|one|two|three|four|five|six|seven|eight|nine))"
)
_RAW_CUED_PROMPT_RE = re.compile(r"(?<![折纽抵回克查])(?:飘|扣(?!除))")
_MULTIPLICATION_RE = re.compile(r"乘以|乘|×|\*")
_TERMINAL_PUNCTUATION = "。！？!?；;,，"
_INLINE_SEPARATORS = frozenset(",，、")
_CLAUSE_PUNCTUATION = "。！？!?；;,，"
_COMMAND_NEGATION = (
    "不要",
    "别",
    "不是",
    "不对",
    "不能",
    "禁止",
    "不用",
    "无需",
    "不必",
    "不准",
    "不可",
    "取消",
)
_PURE_COMMAND_NEGATION_RE = re.compile(
    r"(?:(?:请|大家)\s*)*"
    r"(?:不要|别|不是|不对|不能|禁止|不用|无需|不必|不准|不可|取消|不)"
)
_PROMPT_ONLY_RE = re.compile(r"(?:(?:请|大家)\s*)*")
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
    "或",
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
_PHONE_DIGITS = {"洞": 0, "拐": 7, "勾": 9}
_ENGLISH_DIGITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_CANONICAL_DIGITS = "零一二三四五六七八九"
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_NUMBER_CHARACTERS = frozenset(_CHINESE_DIGITS) | frozenset("十百千万")
_COUNT_CHARACTERS = _NUMBER_CHARACTERS

_ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")
_ASCII_COUNT_RE = re.compile(r"[0-9]{1,3}")
_ASCII_WORD_RE = re.compile(r"[A-Za-z]+")

_REASON_TEXT = {
    "ok": "已接受",
    "invalid_text_type": "输入必须是文本",
    "invalid_length_bounds": "长度设置无效",
    "raw_too_long": "原始文本过长",
    "missing_live_command": "未检测到“扣”或“飘”口令",
    "correction_or_negation": "检测到否定或更正表达",
    "empty_payload": "未检测到口令内容",
    "invalid_multiplication": "乘法口令不完整或有歧义",
    "ambiguous_number": "中文数字表达有歧义",
    "ambiguous_repetition": "重复表达有歧义",
    "unrecognized_payload": "包含无法识别的内容",
    "output_too_short": "规范化结果过短",
    "output_too_long": "规范化结果过长",
    "invalid_numeric_value": "结果不是有效数字口令",
}

_CHANGE_TEXT = {
    "unicode_normalized": "已统一全角字符",
    "outer_whitespace_removed": "已移除首尾空白",
    "terminal_punctuation_removed": "已移除句末标点",
    "framing_removed": "已移除口令提示语",
    "separators_removed": "已移除数字间分隔符",
    "chinese_number_converted": "已转换中文数字",
    "english_number_converted": "已转换英文数字词",
    "phone_reading_converted": "已转换电话读法",
    "repetition_expanded": "已展开重复表达",
    "multiplication_evaluated": "已计算乘法口令",
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
        _CANONICAL_DIGITS[_CHINESE_DIGITS[char]]
        if char in _CHINESE_DIGITS
        else char
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
    if normalized.startswith("一十") and normalized[1:] == canonical:
        return value
    return None


def _parse_chinese_number(source):
    if not any(char in _SMALL_UNITS or char == "万" for char in source):
        return "".join(str(_CHINESE_DIGITS[char]) for char in source)
    value = _parse_positional_number(source)
    return None if value is None else str(value)


def _is_separator(char):
    return char.isspace() or char in _INLINE_SEPARATORS


def _is_word_character(char):
    return char.isalnum() or char == "_"


def _match_english_digit(text, index):
    match = _ASCII_WORD_RE.match(text, index)
    if match is None:
        return None
    word = match.group(0).lower()
    if word not in _ENGLISH_DIGITS:
        return None
    if index and _is_word_character(text[index - 1]):
        return None
    if match.end() < len(text) and _is_word_character(text[match.end()]):
        return None
    return str(_ENGLISH_DIGITS[word]), match.end()


def _parse_count(source):
    if source.isascii() and source.isdigit():
        if len(source) > 1 and source.startswith("0"):
            return None
        value = int(source)
    else:
        if not any(char in _SMALL_UNITS or char == "万" for char in source):
            if len(source) != 1 or source not in _CHINESE_DIGITS:
                return None
            value = _CHINESE_DIGITS[source]
        else:
            parsed = _parse_chinese_number(source)
            if parsed is None:
                return None
            value = int(parsed)
    return value if 1 <= value <= _OUTPUT_MAX_LENGTH else None


def _count_phrase_end(text, index):
    ascii_count = _ASCII_COUNT_RE.match(text, index)
    if ascii_count is not None:
        end = ascii_count.end()
    else:
        end = index
        while end < len(text) and text[end] in _COUNT_CHARACTERS:
            end += 1
        if end == index:
            return None

    while end < len(text) and text[end].isspace():
        end += 1
    return end + 1 if end < len(text) and text[end] == "个" else None


def _starts_repetition(text, index):
    return _count_phrase_end(text, index) is not None


def _match_single_digit(text, index):
    if index >= len(text):
        return None
    char = text[index]
    if char.isascii() and char.isdigit():
        return char, index + 1, None
    if char in _CHINESE_DIGITS:
        return str(_CHINESE_DIGITS[char]), index + 1, "chinese_number_converted"
    if char in _PHONE_DIGITS:
        return str(_PHONE_DIGITS[char]), index + 1, "phone_reading_converted"
    english = _match_english_digit(text, index)
    if english is not None:
        value, end = english
        return value, end, "english_number_converted"
    return None


def _parse_repetition(text, index, changes):
    phrase_end = _count_phrase_end(text, index)
    if phrase_end is None:
        return None

    count_source_end = phrase_end - 1
    while count_source_end > index and text[count_source_end - 1].isspace():
        count_source_end -= 1
    count = _parse_count(text[index:count_source_end])
    if count is None:
        return False, index, "ambiguous_repetition"

    atom_index = phrase_end
    while atom_index < len(text) and text[atom_index].isspace():
        atom_index += 1
    atom = _match_single_digit(text, atom_index)
    if atom is None:
        return False, index, "ambiguous_repetition"
    digit, atom_end, conversion = atom

    if atom_end < len(text):
        has_boundary = _is_separator(text[atom_end]) or _starts_repetition(
            text, atom_end
        )
        if not has_boundary:
            return False, index, "ambiguous_repetition"

    if conversion is not None:
        _add_change(changes, conversion)
    if atom_index != phrase_end:
        _add_change(changes, "separators_removed")
    _add_change(changes, "repetition_expanded")
    return True, atom_end, digit * count


def _parse_payload(text, changes):
    output = []
    index = 0
    while index < len(text):
        if _is_separator(text[index]):
            if not output:
                return None, "unrecognized_payload"
            end = index
            while end < len(text) and _is_separator(text[end]):
                end += 1
            if end == len(text):
                return None, "unrecognized_payload"
            _add_change(changes, "separators_removed")
            index = end
            continue

        repetition = _parse_repetition(text, index, changes)
        if repetition is not None:
            accepted, index, value_or_reason = repetition
            if not accepted:
                return None, value_or_reason
            output.append(value_or_reason)
            continue

        char = text[index]
        if char.isascii() and char.isdigit():
            end = index + 1
            while end < len(text) and text[end].isascii() and text[end].isdigit():
                end += 1
            output.append(text[index:end])
            index = end
            continue

        if char in _NUMBER_CHARACTERS:
            end = index + 1
            while end < len(text) and text[end] in _NUMBER_CHARACTERS:
                if _starts_repetition(text, end):
                    break
                end += 1
            converted = _parse_chinese_number(text[index:end])
            if converted is None:
                return None, "ambiguous_number"
            output.append(converted)
            _add_change(changes, "chinese_number_converted")
            index = end
            continue

        if char in _PHONE_DIGITS:
            output.append(str(_PHONE_DIGITS[char]))
            _add_change(changes, "phone_reading_converted")
            index += 1
            continue

        english = _match_english_digit(text, index)
        if english is not None:
            value, index = english
            output.append(value)
            _add_change(changes, "english_number_converted")
            continue

        return None, "unrecognized_payload"

    return "".join(output), "ok"


def _parse_numeric_expression(text, changes):
    operators = list(_MULTIPLICATION_RE.finditer(text))
    if not operators:
        return _parse_payload(text, changes)
    if len(operators) != 1:
        return None, "invalid_multiplication"

    operator = operators[0]
    left_source = text[: operator.start()].strip()
    right_source = text[operator.end() :].strip()
    if not left_source or not right_source:
        return None, "invalid_multiplication"
    if _ASCII_WORD_RE.search(left_source) or _ASCII_WORD_RE.search(right_source):
        return None, "unrecognized_payload"
    if (
        left_source != text[: operator.start()]
        or right_source != text[operator.end() :]
    ):
        _add_change(changes, "separators_removed")

    left, left_reason = _parse_payload(left_source, changes)
    if left is None:
        return None, left_reason
    right, right_reason = _parse_payload(right_source, changes)
    if right is None:
        return None, right_reason

    _add_change(changes, "multiplication_evaluated")
    return str(int(left) * int(right)), "ok"


def _is_negated_cue(text, cue_start):
    clauses = re.split(
        "[" + re.escape(_CLAUSE_PUNCTUATION) + "]", text[:cue_start]
    )
    command_context = clauses[-1].strip()
    if any(marker in command_context for marker in _COMMAND_NEGATION):
        return True
    if command_context.endswith("不"):
        return True
    if not _PROMPT_ONLY_RE.fullmatch(command_context):
        return False

    for preceding_clause in reversed(clauses[:-1]):
        preceding_clause = preceding_clause.strip()
        if preceding_clause:
            return _PURE_COMMAND_NEGATION_RE.fullmatch(preceding_clause) is not None
    return False


def _normalize(text, min_length, max_length, require_cue):
    changes = []
    if not isinstance(text, str):
        return _result(False, "", "invalid_text_type", changes)
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

    without_punctuation = normalized.rstrip(_TERMINAL_PUNCTUATION)
    if without_punctuation != normalized:
        _add_change(changes, "terminal_punctuation_removed")
    normalized = without_punctuation.rstrip()

    cued_prompts = list(_CUED_PROMPT_RE.finditer(normalized))
    if len(cued_prompts) > 1:
        return _result(False, "", "correction_or_negation", changes)

    live_prompt = None
    if cued_prompts:
        live_prompt = cued_prompts[0]
        if _is_negated_cue(normalized, live_prompt.start()):
            return _result(False, "", "correction_or_negation", changes)
    elif not require_cue:
        live_prompt = _LIVE_PROMPT_RE.match(normalized)

    if require_cue and live_prompt is None:
        reason = (
            "unrecognized_payload"
            if _RAW_CUED_PROMPT_RE.search(normalized)
            else "missing_live_command"
        )
        return _result(False, "", reason, changes)

    framing_end = 0
    if live_prompt:
        framing_end = live_prompt.end()
    else:
        if any(marker in normalized for marker in _CORRECTION_OR_NEGATION):
            return _result(False, "", "correction_or_negation", changes)
        for prefix in _FRAMING:
            if normalized.startswith(prefix):
                framing_end = len(prefix)
                break
    if framing_end:
        normalized = normalized[framing_end:].lstrip()
        if normalized.startswith(":"):
            normalized = normalized[1:].lstrip()
        if live_prompt and normalized.endswith(("吧", "啊", "呀")):
            normalized = normalized[:-1].rstrip()
        _add_change(changes, "framing_removed")

    if any(marker in normalized for marker in _CORRECTION_OR_NEGATION):
        return _result(False, "", "correction_or_negation", changes)

    if not normalized:
        return _result(False, "", "empty_payload", changes)

    if cued_prompts:
        value, reason = _parse_numeric_expression(normalized, changes)
    else:
        value, reason = _parse_payload(normalized, changes)
    if value is None:
        return _result(False, "", reason, changes)
    if not _ASCII_DIGITS_RE.fullmatch(value):
        return _result(False, "", "invalid_numeric_value", changes)
    if len(value) < min_length:
        return _result(False, "", "output_too_short", changes)
    if len(value) > max_length:
        return _result(False, "", "output_too_long", changes)
    return _result(True, value, "ok", changes)


def normalize(text: str, min_length: int = 1, max_length: int = 16) -> dict:
    """Normalize a complete ASR payload into an ASCII digits-only code.

    Rejected results always carry an empty value so callers cannot type a
    partial parse. Length bounds are inclusive and limited to 1 through 32.
    """
    return _normalize(text, min_length, max_length, require_cue=False)


def normalize_cued(text: str, min_length: int = 1, max_length: int = 16) -> dict:
    """Normalize one explicit, non-negated ``扣`` or ``飘`` command."""
    return _normalize(text, min_length, max_length, require_cue=True)
