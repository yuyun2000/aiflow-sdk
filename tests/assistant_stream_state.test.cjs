"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  applyAssistantStreamEvent,
  applyReasoningStreamEvent,
  blockIdentity,
} = require("../web/assistant-stream.js");

function event(sequence, data) {
  return { sequence, created_at: "2026-07-31T00:00:00Z", data };
}

test("partial and final messages share response and block identity without duplication", () => {
  const entries = new Map();

  assert.deepEqual(applyAssistantStreamEvent(entries, "assistant_message_started", event(1, {
    response_id: "msg-api-1",
    message_uuid: "final-uuid",
  })), []);
  assert.equal(entries.size, 0);

  applyAssistantStreamEvent(entries, "assistant_text_delta", event(2, {
    response_id: "msg-api-1",
    block_index: 0,
    message_uuid: "stream-uuid",
    finalized: false,
    text: "正在读取官方",
  }));
  applyAssistantStreamEvent(entries, "assistant_text_delta", event(3, {
    response_id: "msg-api-1",
    block_index: 0,
    message_uuid: "another-stream-uuid",
    finalized: false,
    text: " UIFlow2 文档。",
  }));

  assert.equal(entries.size, 1);
  assert.equal(entries.get("msg-api-1:0").text, "正在读取官方 UIFlow2 文档。");

  applyAssistantStreamEvent(entries, "assistant_message", event(4, {
    response_id: "msg-api-1",
    block_index: 0,
    message_uuid: "different-final-uuid",
    finalized: true,
    text: "正在读取官方 UIFlow2 文档。",
  }));
  const late = applyAssistantStreamEvent(entries, "assistant_text_delta", event(5, {
    response_id: "msg-api-1",
    block_index: 0,
    message_uuid: "stream-uuid",
    finalized: false,
    text: "不应出现",
  }));
  assert.deepEqual(late, []);
  assert.equal(entries.get("msg-api-1:0").text, "正在读取官方 UIFlow2 文档。");

  const finished = applyAssistantStreamEvent(entries, "assistant_message_finished", event(6, {
    response_id: "msg-api-1",
    message_uuid: "different-final-uuid",
  }));
  assert.equal(finished.length, 1);
  assert.equal(entries.get("msg-api-1:0").finished, true);
});

test("each text block remains independent within one response", () => {
  const entries = new Map();
  for (const [sequence, blockIndex, text] of [[1, 0, "first"], [2, 2, "second"]]) {
    applyAssistantStreamEvent(entries, "assistant_text_delta", event(sequence, {
      response_id: "msg-api-2",
      block_index: blockIndex,
      finalized: false,
      text,
    }));
  }

  assert.equal(entries.size, 2);
  assert.equal(entries.get("msg-api-2:0").text, "first");
  assert.equal(entries.get("msg-api-2:2").text, "second");
  assert.deepEqual(blockIdentity({response_id: "msg-api-2", block_index: 2}, {}), {
    responseId: "msg-api-2",
    blockIndex: 2,
    key: "msg-api-2:2",
  });
});

test("final message reconciles a provider block-index mismatch", () => {
  const entries = new Map();
  applyAssistantStreamEvent(entries, "assistant_text_delta", event(1, {
    response_id: "msg-provider-1",
    block_index: 1,
    finalized: false,
    text: "流式回复",
  }));

  applyAssistantStreamEvent(entries, "assistant_message", event(2, {
    response_id: "msg-provider-1",
    block_index: 0,
    finalized: true,
    text: "流式回复完成",
  }));

  assert.equal(entries.size, 1);
  assert.equal(entries.get("msg-provider-1:1").text, "流式回复完成");
  assert.equal(entries.get("msg-provider-1:1").finalized, true);
});

test("different text blocks are not merged by the mismatch fallback", () => {
  const entries = new Map();
  applyAssistantStreamEvent(entries, "assistant_text_delta", event(1, {
    response_id: "msg-provider-2",
    block_index: 1,
    finalized: false,
    text: "第一块",
  }));
  applyAssistantStreamEvent(entries, "assistant_message", event(2, {
    response_id: "msg-provider-2",
    block_index: 2,
    finalized: true,
    text: "第二块",
  }));

  assert.equal(entries.size, 2);
  assert.equal(entries.get("msg-provider-2:1").text, "第一块");
  assert.equal(entries.get("msg-provider-2:2").text, "第二块");
});

test("thinking deltas and final block render once with final calibration", () => {
  const entries = new Map();
  applyReasoningStreamEvent(entries, "agent_reasoning", event(1, {
    response_id: "msg-thinking-1",
    block_index: 0,
    finalized: false,
    thinking: "先检查",
  }));
  applyReasoningStreamEvent(entries, "agent_reasoning", event(2, {
    response_id: "msg-thinking-1",
    block_index: 0,
    finalized: false,
    thinking: "现有代码",
  }));

  assert.equal(entries.size, 1);
  assert.equal(entries.get("msg-thinking-1:0").thinking, "先检查现有代码");

  applyReasoningStreamEvent(entries, "agent_reasoning", event(3, {
    response_id: "msg-thinking-1",
    block_index: 0,
    finalized: true,
    thinking: "先检查现有代码。",
  }));
  const late = applyReasoningStreamEvent(entries, "agent_reasoning", event(4, {
    response_id: "msg-thinking-1",
    block_index: 0,
    finalized: false,
    thinking: "不应重复",
  }));

  assert.deepEqual(late, []);
  assert.equal(entries.size, 1);
  assert.equal(entries.get("msg-thinking-1:0").thinking, "先检查现有代码。");
  assert.equal(entries.get("msg-thinking-1:0").finalized, true);
});

test("partial thinking capture calibrates accumulated deltas without duplication", () => {
  const entries = new Map();
  applyReasoningStreamEvent(entries, "agent_reasoning", event(1, {
    response_id: "msg-thinking-partial",
    block_index: 2,
    finalized: false,
    thinking: "中断前",
  }));
  applyReasoningStreamEvent(entries, "agent_partial_capture", event(2, {
    response_id: "msg-thinking-partial",
    block_index: 2,
    block_type: "thinking",
    finalized: false,
    partial: true,
    thinking: "中断前完整片段",
  }));
  const late = applyReasoningStreamEvent(entries, "agent_reasoning", event(3, {
    response_id: "msg-thinking-partial",
    block_index: 2,
    finalized: false,
    thinking: "不应重复",
  }));

  assert.deepEqual(late, []);
  assert.equal(entries.size, 1);
  assert.equal(entries.get("msg-thinking-partial:2").thinking, "中断前完整片段");
  assert.equal(entries.get("msg-thinking-partial:2").partial, true);
  assert.equal(entries.get("msg-thinking-partial:2").finalized, false);
});
