"""parse_extraction_json: LLM 输出 JSON 损坏时的分层抢救。

回归 2026-08-27 生产故障：提取响应中段损坏（截断类）时原实现两级
json.loads 全失败即 extracted_memories=[]，整批记忆白跑
（日志 "JSON parse failed after remove_code_blocks/extract_json"）。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "mem0"))

from mem0.memory.utils import (  # noqa: E402
    parse_extraction_json,
    remove_code_blocks,
    strip_prose_prefix,
)


class TestDirectParse:
    def test_valid_json(self):
        items = [{"id": "0", "text": "a"}, {"id": "1", "text": "b"}]
        assert parse_extraction_json('{"memory": %s}' % _dumps(items)) == items

    def test_no_memory_key(self):
        assert parse_extraction_json('{"foo": 1}') == []

    def test_memory_not_a_list(self):
        assert parse_extraction_json('{"memory": "x"}') == []

    def test_garbage(self):
        assert parse_extraction_json("not json at all") == []


class TestTrailingComma:
    """层2：剥尾逗号。故障日志实测形态（line 83/84 ',' delimiter）。"""

    def test_element_comma(self):
        assert parse_extraction_json('{"memory": [{"id": "0", "text": "a"},]}') == [
            {"id": "0", "text": "a"}
        ]

    def test_brace_comma(self):
        assert parse_extraction_json('{"memory": [],}') == []


class TestPrefixSalvage:
    """层3：元素级前缀抢救——损坏点之前完整对象全部保留。"""

    def test_truncated_middle_object(self):
        raw = '{"memory": [{"id": "0", "text": "a"}, {"id": "1", "text": "b'
        assert parse_extraction_json(raw) == [{"id": "0", "text": "a"}]

    def test_truncated_first_object(self):
        assert parse_extraction_json('{"memory": [{"id": "0"') == []

    def test_all_items_intact_despite_tail(self):
        raw = '{"memory": [{"id": "0", "text": "a"}, {"id": "1"}] extra junk'
        # 直接/剥逗号均非法（'] extra' 截断闭合）；元素级逐个 raw_decode
        # 恰好能在 ']' 处自然停止，两个对象都救回。
        assert parse_extraction_json(raw) == [{"id": "0", "text": "a"}, {"id": "1"}]

    def test_wrapped_in_code_fence(self):
        raw = '```json\n{"memory": [{"id": "0", "text": "a"},\n```'
        assert parse_extraction_json(raw) == [{"id": "0", "text": "a"}]

    def test_corrupt_middle_then_good_object_unreachable(self):
        # 损坏在两个对象之间：能救回第一个，第二个开始的垃圾不阻塞
        raw = '{"memory": [{"id": "0", "text": "a"}, {oops}, {"id": "2"}]}'
        assert parse_extraction_json(raw) == [{"id": "0", "text": "a"}]


class TestStripProsePrefix:
    """strip_prose_prefix：剥除 JSON 前的叙述前缀（2026-09-02 生产形态）。"""

    def test_chinese_prose_before_json(self):
        raw = '观察到的新事实与现有记忆 id=0 高度重叠：\n\n{"memory": [{"id": "0", "text": "a"}]}'
        assert strip_prose_prefix(raw) == '{"memory": [{"id": "0", "text": "a"}]}'

    def test_english_prose_before_json(self):
        raw = 'Looking at the new messages, I need to extract facts.\n{"memory": []}'
        assert strip_prose_prefix(raw) == '{"memory": []}'

    def test_prose_before_array(self):
        raw = '分析如下：\n[{"id": "0"}]'
        assert strip_prose_prefix(raw) == '[{"id": "0"}]'

    def test_pure_json_unchanged(self):
        raw = '{"memory": []}'
        assert strip_prose_prefix(raw) == '{"memory": []}'

    def test_no_json_unchanged(self):
        raw = "纯叙述，没有任何结构。"
        assert strip_prose_prefix(raw) == raw

    def test_empty_unchanged(self):
        assert strip_prose_prefix("") == ""

    def test_think_tags_stripped_first(self):
        raw = '<think>reasoning</think>{"memory": []}'
        assert strip_prose_prefix(raw) == '{"memory": []}'

    def test_production_shape_prose_plus_fence(self):
        # 生产实证形态：叙述前缀 + 围栏包裹 JSON（remove_code_blocks 正则不匹配）
        raw = (
            "观察到的新事实与现有记忆 id=0 高度重叠，但新消息补充了细节：\n\n"
            "```json\n"
            '{"memory": [{"id": "0", "text": "a", "event": "NONE"}]}\n'
            "```"
        )
        out = strip_prose_prefix(remove_code_blocks(raw))
        assert json.loads(out)["memory"][0]["id"] == "0"

    def test_end_to_end_with_parse_extraction_json(self):
        # 叙述前缀 + 中段截断 → 剥前缀后 salvage 仍能收回完整对象
        raw = '观察到的新事实与现有记忆 id=0 高度重叠。\n{"memory": [{"id": "0", "text": "a"}, {"id": "1", "text": "b'
        assert parse_extraction_json(strip_prose_prefix(raw)) == [{"id": "0", "text": "a"}]


def _dumps(obj):
    import json

    return json.dumps(obj)
