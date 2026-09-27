"""Deterministic intent handling and durable per-conversation memory."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .text.normalize import fingerprint, is_auto_reply, normalize_text
from .voice import HINGLISH_MARKERS, resolve_voice


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str = ""
    customer_id: str | None = None
    trigger_id: str = ""
    cta: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    bot_bodies_sent: list[str] = field(default_factory=list)
    mode: str = "pitch"
    auto_reply_streak: int = 0
    nudge_count: int = 0
    cooldown_until: str = ""
    opts_out: bool = False
    lang_last_observed: str = ""
    closed: bool = False
    recipient: str = "merchant"
    third_person_subject: str = ""


class ConversationManager:
    def __init__(self, journal_path: str | Path | None = None) -> None:
        self.path = Path(journal_path) if journal_path else None
        self._lock = threading.RLock()
        self.conversations: dict[str, Conversation] = {}
        self.merchant_fingerprints: dict[str, set[str]] = {}
        self._merchant_auto_streak: dict[str, int] = {}
        self.merchant_openers: dict[str, set[str]] = {}
        self.suppressed_keys: set[str] = set()
        self.opted_out_merchants: set[str] = set()
        self.last_sent_at: dict[str, str] = {}
        self._rehydrate()

    def _rehydrate(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            last: dict[str, Any] | None = None
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and entry.get("state"):
                            last = entry["state"]
                    except (json.JSONDecodeError, TypeError):
                        continue
            if last:
                self.conversations = {key: Conversation(**value)
                                      for key, value in last.get("conversations", {}).items()}
                self.merchant_fingerprints = {k: set(v) for k, v in last.get("merchant_fingerprints", {}).items()}
                self._merchant_auto_streak = {k: int(v) for k, v in last.get("merchant_auto_streak", {}).items()}
                self.merchant_openers = {k: set(v) for k, v in last.get("merchant_openers", {}).items()}
                self.suppressed_keys = set(last.get("suppressed_keys", []))
                self.opted_out_merchants = set(last.get("opted_out_merchants", []))
                self.last_sent_at = dict(last.get("last_sent_at", {}))
        except (OSError, TypeError, ValueError):
            return

    def _snapshot(self) -> dict[str, Any]:
        return {"conversations": {k: asdict(v) for k, v in self.conversations.items()},
                "merchant_fingerprints": {k: sorted(v) for k, v in self.merchant_fingerprints.items()},
                "merchant_auto_streak": dict(self._merchant_auto_streak),
                "merchant_openers": {k: sorted(v) for k, v in self.merchant_openers.items()},
                "suppressed_keys": sorted(self.suppressed_keys),
                "opted_out_merchants": sorted(self.opted_out_merchants),
                "last_sent_at": dict(self.last_sent_at)}

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"state": self._snapshot()}, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
        except OSError:
            # The in-memory decision path remains available if disk is read-only.
            return

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self.conversations.get(conversation_id)

    def is_opted_out(self, merchant_id: str | None) -> bool:
        with self._lock:
            return bool(merchant_id and merchant_id in self.opted_out_merchants)

    def is_suppressed(self, key: str | None) -> bool:
        with self._lock:
            return bool(key and key in self.suppressed_keys)

    def is_closed(self, conversation_id: str) -> bool:
        with self._lock:
            item = self.conversations.get(conversation_id)
            return bool(item and item.closed)

    def all_conversation_ids(self) -> set[str]:
        with self._lock:
            return set(self.conversations)

    def mark_send(self, conversation_id: str, merchant_id: str, sent_at: str,
                  suppression_key: str = "", body: str = "", trigger_id: str = "",
                  customer_id: str | None = None, cta: str = "") -> None:
        with self._lock:
            state = self.conversations.get(conversation_id) or Conversation(
                conversation_id, merchant_id, customer_id, trigger_id, mode="pitch")
            state.merchant_id, state.customer_id, state.trigger_id = merchant_id, customer_id, trigger_id
            state.cta = cta or state.cta
            if body:
                state.bot_bodies_sent.append(body)
                state.nudge_count += 1
                opener = _opener(body)
                if opener:
                    self.merchant_openers.setdefault(merchant_id, set()).add(opener)
            state.turns.append({"from": "vera", "body": body, "at": sent_at})
            self.conversations[conversation_id] = state
            self.last_sent_at[merchant_id] = sent_at
            if suppression_key:
                self.suppressed_keys.add(suppression_key)
            self._persist()

    def closed_ids_for_merchant(self, merchant_id: str) -> set[str]:
        with self._lock:
            return {cid for cid, conv in self.conversations.items()
                    if conv.merchant_id == merchant_id and conv.closed}

    def handle(self, data: dict[str, Any], store: Any) -> dict[str, Any]:
        conversation_id = str(data.get("conversation_id") or "cold-start")
        merchant_id = str(data.get("merchant_id") or "")
        customer_id = data.get("customer_id")
        customer_id = str(customer_id) if customer_id else None
        text = str(data.get("message") or "").strip()
        received_at = str(data.get("received_at") or "")
        with self._lock:
            conv = self.conversations.get(conversation_id)
            if conv is None:
                conv = Conversation(conversation_id, merchant_id, customer_id)
                self.conversations[conversation_id] = conv
            if merchant_id:
                conv.merchant_id = merchant_id
            if customer_id:
                conv.customer_id = customer_id
            merchant_id = conv.merchant_id or merchant_id or f"unknown:{conversation_id}"
            if conv.closed or merchant_id in self.opted_out_merchants:
                return {"action": "end", "rationale": "This conversation is closed and the merchant is suppressed."}

            conv.turns.append({"from": str(data.get("from_role") or "merchant"),
                               "body": text, "at": received_at})
            category, merchant, customer, trigger = self._contexts(conv, store)
            intent = classify_intent(text)
            norm = normalize_text(text)
            fp = fingerprint(merchant_id, text)
            seen = self.merchant_fingerprints.setdefault(merchant_id, set())
            repeated = fp in seen
            if text:
                seen.add(fp)

            if intent in {"HOSTILE", "OPT_OUT"}:
                conv.mode, conv.opts_out, conv.closed = "closed", True, True
                self.opted_out_merchants.add(merchant_id)
                self._persist()
                return {"action": "end", "rationale": "Opt-out or hostile feedback received; merchant is now suppressed."}

            if intent == "AUTO_REPLY" or (repeated and is_auto_reply(text)):
                conv.auto_reply_streak = max(conv.auto_reply_streak, 1)
                # The escalation is merchant-wide so distinct conversation ids cannot reset it.
                streak = max(conv.auto_reply_streak,
                             max((item.auto_reply_streak for item in self.conversations.values()
                                  if item.merchant_id == merchant_id), default=0))
                merchant_auto = int(self._merchant_auto_streak.get(merchant_id, 0))
                streak = max(streak, merchant_auto) + (1 if repeated else 0)
                # First occurrence is nudge 1, next distinct-conversation repeat is 2.
                streak = max(1, streak)
                self._merchant_auto_streak[merchant_id] = streak
                conv.auto_reply_streak = streak
                if streak >= 3:
                    conv.mode, conv.closed = "closed", True
                    self.opted_out_merchants.add(merchant_id)
                    self._persist()
                    return {"action": "end", "rationale": "Repeated automated replies detected; conversation ended and merchant suppressed."}
                self._persist()
                if streak == 2:
                    return {"action": "wait", "wait_seconds": 86400,
                            "rationale": "Repeated automated reply; waiting for a human response."}
                return self._send(conv, "It looks like an automatic reply. Reply YES when you see this.",
                                  "binary_yes_no", "First canned reply detected; waiting for the owner.")

            conv.auto_reply_streak = 0
            if text:
                self._merchant_auto_streak[merchant_id] = 0
                conv.nudge_count = 0

            history = list(conv.turns)
            voice = resolve_voice(category, merchant, customer, trigger, history)
            if _is_hindi(text):
                voice = replace(voice, lang="hi_en")
            elif _is_pure_english(text):
                voice = replace(voice, lang="en")
            conv.lang_last_observed = voice.lang
            conv.recipient = voice.recipient
            conv.third_person_subject = voice.third_person_subject

            if not text:
                conv.nudge_count += 1
                if conv.nudge_count >= 3:
                    conv.mode, conv.closed = "closed", True
                    self.opted_out_merchants.add(merchant_id)
                    self._persist()
                    return {"action": "end", "rationale": "Three unanswered follow-ups; conversation closed."}
                self._persist()
                return {"action": "wait", "wait_seconds": 1800,
                        "rationale": "No reply content received; pausing before another follow-up."}

            if intent == "COMMIT":
                conv.mode = "action"
                body = ("Done — I’m preparing the next step now. Shall I send the draft for your confirmation?"
                        if voice.lang == "en" else
                        "Bilkul, next step ka draft taiyar kar rahi hoon. Confirmation ke liye bhej doon?")
                self._persist()
                return self._send(conv, body, "binary_yes_no", "Commitment received; moved directly to action mode.")

            if intent == "OFF_TOPIC":
                body = ("I can’t help file GST here, but I can continue with the original listing follow-up. Should I continue?"
                        if voice.lang == "en" else
                        "GST filing mein yahan madad nahi kar sakti, par original listing follow-up jaari rakh sakti hoon. Kya main aage badhun?")
                self._persist()
                return self._send(conv, body, conv.cta or "binary_yes_no",
                                  "Declined an unrelated request and redirected to the original follow-up.")

            if intent in {"OBJECT", "ASK_INFO", "QUESTION", "ACCEPT", "OTHER"}:
                body = _reply_body(intent, voice.lang, bool(merchant), conv.trigger_id)
                if normalize_text(body) in {normalize_text(x) for x in conv.bot_bodies_sent}:
                    self._persist()
                    return {"action": "wait", "wait_seconds": 1800,
                            "rationale": "Avoiding a repeated response; pausing for the merchant."}
                self._persist()
                return self._send(conv, body, "open_ended", "Responded to the merchant’s latest turn without adding unsupported claims.")

            self._persist()
            return {"action": "wait", "wait_seconds": 1800,
                    "rationale": "No actionable intent detected; pausing before follow-up."}

    def _contexts(self, conv: Conversation, store: Any) -> tuple[dict, dict, dict | None, dict]:
        merchant_record = store.get("merchant", conv.merchant_id) if conv.merchant_id else None
        merchant = merchant_record.payload if merchant_record else {}
        customer_record = store.get("customer", conv.customer_id) if conv.customer_id else None
        customer = customer_record.payload if customer_record else None
        category_id = str(merchant.get("category_slug") or "")
        category_record = store.get("category", category_id) if category_id else None
        category = category_record.payload if category_record else {}
        trigger_record = store.get("trigger", conv.trigger_id) if conv.trigger_id else None
        trigger = trigger_record.payload if trigger_record else {}
        return category, merchant, customer, trigger

    def _send(self, conv: Conversation, body: str, cta: str, rationale: str) -> dict[str, Any]:
        normalized = normalize_text(body)
        prior = {normalize_text(value) for value in conv.bot_bodies_sent}
        if normalized in prior:
            return {"action": "wait", "wait_seconds": 1800,
                    "rationale": "Repeated message suppressed; waiting instead."}
        opener = _opener(body)
        if opener and opener in self.merchant_openers.get(conv.merchant_id, set()):
            return {"action": "wait", "wait_seconds": 1800,
                    "rationale": "A repeated opener was suppressed across the merchant's conversations."}
        if opener:
            self.merchant_openers.setdefault(conv.merchant_id, set()).add(opener)
        conv.bot_bodies_sent.append(body)
        conv.turns.append({"from": "vera", "body": body, "at": ""})
        self._persist()
        return {"action": "send", "body": body, "cta": cta, "rationale": rationale}

    def reset(self) -> None:
        with self._lock:
            self.conversations.clear()
            self.merchant_fingerprints.clear()
            self._merchant_auto_streak.clear()
            self.merchant_openers.clear()
            self.suppressed_keys.clear()
            self.opted_out_merchants.clear()
            self.last_sent_at.clear()
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("", encoding="utf-8")


def classify_intent(message: str) -> str:
    text = normalize_text(message)
    if not text:
        return "SILENCE"
    if any(term in text for term in ("stop messaging", "do not message", "dont message",
                                     "unsubscribe", "stop contacting", "stop", "useless spam")):
        return "OPT_OUT" if "stop" in text or "unsubscribe" in text else "HOSTILE"
    if any(term in text for term in ("useless", "spam", "bothering me", "waste my time")):
        return "HOSTILE"
    if is_auto_reply(message):
        return "AUTO_REPLY"
    if any(term in text for term in ("gst", "tax filing", "file my tax", "income tax")):
        return "OFF_TOPIC"
    if any(term in text for term in ("ok lets do it", "lets do it", "go ahead", "yes send",
                                     "do it", "whats next", "what next", "want to join",
                                     "i want to join", "mujhe join karna hai", "proceed")):
        return "COMMIT"
    if any(term in text for term in ("not interested", "no thanks", "later", "busy", "maybe later")):
        return "OBJECT"
    if any(term in text for term in ("yes", "haan", "sure", "please send", "interested")):
        return "ACCEPT"
    if "?" in message or any(token in text.split() for token in ("what", "how", "why", "when", "where")):
        return "ASK_INFO"
    return "OTHER"


def _is_hindi(message: str) -> bool:
    if re.search(r"[\u0900-\u097f]", message):
        return True
    words = set(normalize_text(message).split())
    return bool(words & set(HINGLISH_MARKERS))


def _is_pure_english(message: str) -> bool:
    words = set(normalize_text(message).split())
    return bool(message) and not (words & set(HINGLISH_MARKERS)) and not re.search(r"[\u0900-\u097f]", message)


def _opener(body: str) -> str:
    sentence = re.split(r"[.!?]", body.strip(), maxsplit=1)[0]
    return " ".join(normalize_text(sentence).split()[:5])


def _reply_body(intent: str, lang: str, has_context: bool, trigger_id: str) -> str:
    if lang != "en":
        if intent == "ASK_INFO":
            return "Haan, main madad karungi. Aapko kis detail par clarity chahiye?"
        if intent == "ACCEPT":
            return "Theek hai, main yeh taiyar karungi. Kya main agla step bhejun?"
        return "Samajh gayi. Main isi follow-up par dhyan de rahi hoon. Kya aap ek detail share karenge?"
    if intent == "ASK_INFO":
        return "I can clarify the current follow-up. Which detail would be most useful?"
    if intent == "ACCEPT":
        return "Understood. I’ll prepare the next step for this follow-up. Shall I send it for your confirmation?"
    if intent == "OBJECT":
        return "Understood. I’ll pause this suggestion. Would a later follow-up be more useful?"
    if not has_context:
        return "I can help with a merchant listing or a customer follow-up. What would you like me to check?"
    return "I’ve noted that. I can keep this follow-up focused on your business. What should I address first?"
