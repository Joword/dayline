"""Deterministic assertions for eval cases (the ``expect`` block of an example)."""

from __future__ import annotations

from typing import Any

from app.skills.runner import SkillRunResult


def check(expect: dict[str, Any], r: SkillRunResult) -> list[str]:
    fails: list[str] = []
    out = r.output if isinstance(r.output, dict) else {}
    items = out.get("own_items") or []
    text = r.markdown

    if (want := expect.get("status")) and r.status != want:
        fails.append(f"status {r.status} != {want} ({'; '.join(r.errors)})")
    for kind, n in (expect.get("min_items") or {}).items():
        got = sum(1 for i in items if i.get("kind") == kind)
        if got < n:
            fails.append(f"expected >= {n} '{kind}' items, got {got}")
    q = out.get("questions") or []
    if expect.get("questions") == "nonempty" and not q:
        fails.append("expected follow-up questions, got none")
    if expect.get("questions") == "empty" and q:
        fails.append(f"expected no questions, got {len(q)}")
    if (mq := expect.get("max_questions")) is not None and len(q) > mq:
        fails.append(f"expected <= {mq} questions, got {len(q)}")
    for s in expect.get("contains") or []:
        if s not in text:
            fails.append(f"missing text: {s!r}")
    for s in expect.get("not_contains") or []:
        if s in text:
            fails.append(f"must not contain: {s!r}")
    if (src := expect.get("all_sources")) and any(i.get("source") != src for i in items):
        fails.append(f"all items should have source={src}")
    if want := expect.get("rating"):
        allowed = [want] if isinstance(want, str) else want
        if out.get("rating") not in allowed:
            fails.append(f"rating {out.get('rating')} not in {allowed}")
    flags = out.get("trend_flags") or []
    if (n := expect.get("min_trend_flags")) is not None and len(flags) < n:
        fails.append(f"expected >= {n} trend flags, got {len(flags)}")
    if (n := expect.get("max_trend_flags")) is not None and len(flags) > n:
        fails.append(f"expected <= {n} trend flags, got {len(flags)}")
    esc = out.get("escalation_candidates") or []
    if (n := expect.get("min_escalations")) is not None and len(esc) < n:
        fails.append(f"expected >= {n} escalation candidates, got {len(esc)}")
    if (n := expect.get("max_escalations")) is not None and len(esc) > n:
        fails.append(f"expected <= {n} escalation candidates, got {len(esc)}")
    sections = [e for key in ("highlights", "risks", "escalation_candidates") for e in out.get(key) or []]
    for m in expect.get("members_exclude") or []:
        if any(m in (e.get("members") or []) for e in sections):
            fails.append(f"{m} must not appear in highlights/risks/escalations")
    for s in expect.get("not_in_sections") or []:
        if any(s in (e.get("text", "") + e.get("reason", "")) for e in sections) or s in out.get(
            "summary", ""
        ):
            fails.append(f"{s!r} must not be written up")
    for k, v in (expect.get("stats") or {}).items():
        if (out.get("stats") or {}).get(k) != v:
            fails.append(f"stats.{k} = {(out.get('stats') or {}).get(k)} != {v}")
    notes = out.get("consistency_notes") or []
    if (n := expect.get("min_consistency_notes")) is not None and len(notes) < n:
        fails.append(f"expected >= {n} consistency notes, got {len(notes)}")
    if (n := expect.get("max_consistency_notes")) is not None and len(notes) > n:
        fails.append(f"expected <= {n} consistency notes, got {len(notes)}")
    issues = out.get("issues") or []
    if (n := expect.get("min_issues")) is not None and len(issues) < n:
        fails.append(f"expected >= {n} issues, got {len(issues)}")
    if (n := expect.get("max_issues")) is not None and len(issues) > n:
        fails.append(f"expected <= {n} issues, got {len(issues)}")
    return fails
