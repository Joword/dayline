"""SkillRouter: map user text to a skill.

1. Explicit commands (``/gen ...``, ``dl review ...``) - deterministic, no model.
2. Intent recognition with the small model. It sees only each skill's name,
   description and example intents (progressive disclosure), never prompts or
   schemas, and only the skills the user's role allows. Low confidence -> no route.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.llm.types import LLMError, LLMRequest, Message, SystemPart
from app.skills.manifest import Skill
from app.skills.registry import SkillRegistry
from app.skills.runner import extract_json

MIN_CONFIDENCE = 0.6

ROUTER_PROMPT = {
    "zh": (
        "你是 Dayline 的意图路由。根据用户的话,从下面的 skill 列表中选出最合适的一个;都不合适时选 none。"
        "只做选择,不要回答用户的问题。confidence 填 0 到 1 之间的把握程度。\n"
    ),
    "en": (
        "You route requests for Dayline. Pick the one skill from the list below that fits the user's message, "
        "or none if nothing fits. Only choose - do not answer the request. confidence is 0 to 1.\n"
    ),
}


@dataclass
class Route:
    skill: Skill
    rest: str  # the text after the command (or the whole text for intents)
    via: str  # "command" | "intent"
    confidence: float = 1.0


def primary_text_input(skill: Skill) -> str | None:
    """The input that free text should go to (e.g. `narrative`, `report`); None if the
    skill needs structured inputs only."""
    for spec in skill.manifest.inputs:
        if spec.required and spec.type == "text":
            return spec.name
    return None


class SkillRouter:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def route(self, text: str) -> Route | None:
        """Explicit commands only."""
        t = text.strip()
        best: tuple[int, Skill, str] | None = None
        for s in self.registry.list():
            for cmd in s.manifest.triggers.commands:
                c = cmd.strip()
                if t == c or t.startswith(c + " ") or t.startswith(c + "\n"):
                    if best is None or len(c) > best[0]:  # longest command wins
                        best = (len(c), s, t[len(c) :].strip())
        return Route(skill=best[1], rest=best[2], via="command") if best else None

    async def resolve(
        self, text: str, gateway, *, lang: str = "zh", is_manager: bool = False
    ) -> Route | None:
        """Command first, then intent recognition with the small model."""
        if r := self.route(text):
            return r
        candidates = [s for s in self.registry.list() if is_manager or "member" in s.manifest.roles]
        if not candidates or not text.strip():
            return None
        catalog = "\n".join(
            f"- {s.name}: {s.manifest.description}"
            + (f" (e.g. {'; '.join(s.manifest.triggers.intents[:4])})" if s.manifest.triggers.intents else "")
            for s in candidates
        )
        names = [s.name for s in candidates]
        req = LLMRequest(
            tier="small",
            system=[
                SystemPart(
                    text=ROUTER_PROMPT.get(lang, ROUTER_PROMPT["en"])
                    + UNTRUSTED_RULES[lang if lang in UNTRUSTED_RULES else "en"]
                )
            ],
            messages=[
                Message.user(f"skills:\n{catalog}\n\n{wrap_untrusted(text, kind='input', name='message')}")
            ],
            output_schema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": [*names, "none"]},
                    "confidence": {"type": "number"},
                },
                "required": ["skill", "confidence"],
                "additionalProperties": False,
            },
            max_tokens=2048,
            purpose="router",
        )
        try:
            data = extract_json((await gateway.complete(req)).text)
        except (LLMError, ValueError):
            return None
        skill, conf = data.get("skill"), float(data.get("confidence") or 0)
        if skill not in names or conf < MIN_CONFIDENCE:
            return None
        return Route(skill=self.registry.get(skill), rest=text.strip(), via="intent", confidence=conf)
