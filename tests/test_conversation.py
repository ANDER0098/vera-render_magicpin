from __future__ import annotations

from vera.conversation import ConversationManager, classify_intent
from vera.store import ContextStore


def reply(state, text, cid="c1", merchant="m1", turn=2):
    return state.handle({"conversation_id": cid, "merchant_id": merchant,
                         "from_role": "merchant", "message": text,
                         "received_at": "2026-09-26T10:00:00Z", "turn_number": turn},
                        ContextStore("missing-journal.jsonl"))


def test_cross_conversation_auto_reply_escalates_nudge_wait_end(tmp_path):
    state = ConversationManager(tmp_path / "conversation.jsonl")
    text = "Thank you for contacting our clinic! Our team will respond shortly."
    results = [reply(state, text, f"conv_auto_{turn}", "m-auto", turn + 1)
               for turn in range(1, 4)]
    assert [item["action"] for item in results] == ["send", "wait", "end"]
    assert results[1]["wait_seconds"] >= 86400
    assert state.is_opted_out("m-auto")
    restored = ConversationManager(tmp_path / "conversation.jsonl")
    assert restored.is_opted_out("m-auto")
    assert restored._merchant_auto_streak["m-auto"] == 3


def test_commit_hostile_offtopic_and_cold_start():
    state = ConversationManager()
    commitment = reply(state, "Ok lets do it. Whats next?", "conv_intent_1", "m-commit")
    assert commitment["action"] == "send"
    lower = commitment["body"].lower()
    assert any(word in lower for word in ("done", "sending", "draft", "here", "confirm", "proceed", "next"))
    assert not any(term in lower for term in ("would you", "do you", "can you tell", "what if", "how about"))
    assert classify_intent("Ok lets do it. Whats next?") == "COMMIT"
    assert reply(state, "Stop messaging me. This is useless spam.", "conv_hostile", "m-hostile")["action"] == "end"
    off_topic = reply(state, "Can you help me file my GST?", "conv-gst", "m-gst")
    assert off_topic["action"] == "send" and "GST" in off_topic["body"]
    cold = reply(state, "Can you tell me what this is about?", "cold", "")
    assert cold["action"] == "send" and cold["body"]


def test_language_switch_nudge_limit_and_no_repeated_body():
    state = ConversationManager()
    first = reply(state, "Please update me", "language", "m-lang")
    assert first["action"] == "send"
    assert state.get("language").lang_last_observed == "en"
    second = reply(state, "Haan mujhe bataiye", "language", "m-lang", 3)
    assert second["action"] == "send"
    assert state.get("language").lang_last_observed == "hi_en"
    repeated = reply(state, "Haan mujhe bataiye", "language", "m-lang", 4)
    assert repeated["action"] == "wait"

    nudge_state = ConversationManager()
    assert [reply(nudge_state, "", "silent", "m-silent")["action"] for _ in range(3)] == ["wait", "wait", "end"]


def test_planned_cta_survives_off_topic_redirect_and_reply_resets_nudge_count():
    state = ConversationManager()
    state.mark_send("cta-conv", "m-cta", "2026-09-27T10:00:00Z", body="A grounded follow-up.",
                    trigger_id="trigger-1", customer_id="customer-1", cta="show_availability")
    assert state.get("cta-conv").nudge_count == 1
    result = reply(state, "Can you help me file my GST?", "cta-conv", "m-cta")
    assert result["action"] == "send"
    assert result["cta"] == "show_availability"
    assert state.get("cta-conv").nudge_count == 0
