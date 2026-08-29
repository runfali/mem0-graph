"""分块提取的「无益即不分块」回归（2026-08-29 毒桶事故）。

现场：MEM0_LLM_CONTEXT_WINDOW=10000，而提取模板固定开销（system prompt +
existing memories + last-k）tiktoken 估算就有 ~9000 tokens；output_reserve
= max_tokens(8192)+512 = 8704 直接把窗口吃干。于是 per_chunk_budget 被夹到
512，任何两条消息的载荷都被切成 chunks=2 —— 而每一块的真实 prompt 仍是
「9000 开销 + 内容」≈ 10605 tokens，照样超窗。推理模型（sensenova flash-lite）
把 max_tokens 全花在 reasoning 上、content 为空 → finish_reason=length →
整单 502。客户端因此无限重投同一个桶。

修复：开销装不下时不再分块（一次诚实的调用胜过 N 次必死的调用）。
"""

from mem0.memory.main import _build_extraction_chunks


def _fake_estimator(base_tokens, content_tokens):
    """替换 _estimate_extraction_prompt_tokens：new_messages 为空即测固定开销。"""

    def fake(system_prompt, existing_memories, recently_extracted_memories, new_messages,
             last_k_messages, custom_instructions):
        return base_tokens if not new_messages else base_tokens + content_tokens

    return fake


def _messages(*contents):
    return [{"role": i % 2 and "assistant" or "user", "content": c} for i, c in enumerate(contents)]


def test_bails_out_when_overhead_does_not_fit(monkeypatch):
    import mem0.memory.main as m

    monkeypatch.setattr(m, "_estimate_extraction_prompt_tokens", _fake_estimator(9000, 1605))
    msgs = _messages("u" * 2000, "a" * 2000)

    # 事故现场参数：窗口 10000、reserve 5000（封顶后）、固定开销 9000 → 无剩余预算
    chunks = _build_extraction_chunks(msgs, "sys", [], [], None, context_window=10000, output_reserve=5000)

    assert chunks == [msgs], "开销装不下时必须退回单次调用，而不是切成必然超窗的多块"


def test_still_chunks_when_budget_is_real(monkeypatch):
    import mem0.memory.main as m

    monkeypatch.setattr(m, "_estimate_extraction_prompt_tokens", _fake_estimator(9000, 40000))
    msgs = _messages("u" * 20000, "a" * 20000, "u" * 20000, "a" * 20000)

    # 窗口 32768：32768 - 8704 - 9000 - 4096 = 10968 预算，每条 ~5000 tokens → 逐条成块
    chunks = _build_extraction_chunks(msgs, "sys", [], [], None, context_window=32768, output_reserve=8704)

    assert len(chunks) > 1, "预算真实存在时仍应分块（大载荷不能被合并成一次调用）"
    assert sum(len(c) for c in chunks) == len(msgs), "分块不得丢消息"
    assert all(len(c) == 1 for c in chunks), "每条消息各自成块（逐条粒度）"
