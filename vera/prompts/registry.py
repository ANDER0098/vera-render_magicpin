"""Hot-reloadable category and trigger-kind prompt pack registry."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class PromptPack:
    system: str
    user_template: str
    version: str

    def render(self, facts: list[Any], voice: Any, trigger: dict | None,
               customer: dict | None, now_iso: str = "") -> str:
        values: dict[str, str] = {"salutation": str(getattr(voice, "salutation", "")),
                                  "owner": "", "business": "", "locality": "", "city": "",
                                  "lang": str(getattr(voice, "lang", "en")),
                                  "emoji": str(getattr(voice, "emoji", "") or ""),
                                  "cta": str((trigger or {}).get("_cta") or ""), "citation": "", "now_iso": now_iso,
                                  "kind": str((trigger or {}).get("kind") or "generic")}
        merchant = (trigger or {}).get("_merchant") or {}
        identity = merchant.get("identity") or {}
        values.update(owner=str(identity.get("owner_first_name") or ""),
                      business=str(identity.get("name") or ""),
                      locality=str(identity.get("locality") or ""),
                      city=str(identity.get("city") or ""))
        allowed = set(values)
        for fact in facts:
            values[fact.key] = str(fact.phrase)
            allowed.add(fact.key)
            if fact.citation and not values["citation"]:
                values["citation"] = str(fact.citation)
        # Unknown braces stay literal; pack values cannot synthesize new fields.
        template = self.user_template
        rendered = re.sub(r"\{([A-Za-z0-9_]+)\}",
                          lambda m: values.get(m.group(1), m.group(0)) if m.group(1) in allowed else m.group(0),
                          template)
        grounded = [{"key": fact.key, "phrase": fact.phrase,
                     "value": fact.value, "source": fact.json_path}
                    for fact in facts]
        return rendered + "\n\nGrounded facts (use only these claims):\n" + json.dumps(
            grounded, ensure_ascii=False, separators=(",", ":"), default=str)


class PromptRegistry:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or Path(__file__).parent / "packs")
        self._cache: dict[str, tuple[int, PromptPack]] = {}
        self._lock = RLock()

    def get(self, category: str | None, kind: str | None) -> PromptPack:
        name = (category or "generic").lower()
        path = self.root / f"{name}.md"
        if not path.exists():
            path = self.root / "generic.md"
        stamp = path.stat().st_mtime_ns if path.exists() else 0
        key = f"{path}:{kind or 'generic'}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
            source = path.read_text(encoding="utf-8") if path.exists() else ""
            system, blocks = _parse(source)
            user = blocks.get(str(kind or ""), blocks.get("generic", "{{facts}}"))
            pack = PromptPack(system, user, f"{path.name}:{stamp}:{kind or 'generic'}")
            self._cache[key] = (stamp, pack)
            return pack


def _parse(source: str) -> tuple[str, dict[str, str]]:
    lines = source.splitlines()
    system_lines: list[str] = []
    blocks: dict[str, list[str]] = {}
    active = "generic"
    in_body = False
    for line in lines:
        if line.startswith("## KIND:"):
            active = line.split(":", 1)[1].strip()
            blocks.setdefault(active, [])
            in_body = True
        elif line.startswith("## SYSTEM"):
            active = "__system__"
            in_body = True
        elif in_body:
            (system_lines if active == "__system__" else blocks.setdefault(active, [])).append(line)
    return "\n".join(system_lines).strip(), {k: "\n".join(v).strip() for k, v in blocks.items()}
