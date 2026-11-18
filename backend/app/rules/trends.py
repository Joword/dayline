"""Cross-day trend signals, computed by code (plan §4.2 "跨日趋势").

The model gets these as facts to interpret; it does not have to find them itself:

* ``delayed_plan``     a plan item from the previous report that today's report does not
                       show as done (``days`` = how many reports in a row it was planned)
* ``stalled``          the same in-progress item in N consecutive reports with no progress change
* ``overdue``          an in-progress item whose ETA has passed
* ``repeated_done``    a "done" item already reported as done earlier
* ``lingering_blocker`` the same blocker in N consecutive reports

"Consecutive" counts submitted reports, not calendar days (weekends do not break a streak).
Text matching uses character-bigram Jaccard similarity, which works for Chinese
without a tokenizer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)

SIMILAR = 0.5
SAME = 0.8
STALLED_REPORTS = 3
BLOCKER_REPORTS = 2


def _norm(s: str) -> str:
    return _STRIP.sub("", (s or "").lower())


def _grams(s: str) -> set[str]:
    n = _norm(s)
    return {n[i : i + 2] for i in range(len(n) - 1)} or ({n} if n else set())


def similarity(a: str, b: str) -> float:
    A, B = _grams(a), _grams(b)
    union = A | B
    return len(A & B) / len(union) if union else 0.0


@dataclass
class Signal:
    type: str
    text: str
    reports: int  # length of the streak (in reports)
    dates: list[str]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _items(report: dict[str, Any] | None, kind: str) -> list[dict[str, Any]]:
    return [i for i in (report or {}).get("own_items") or [] if i.get("kind") == kind]


def _find(items: list[dict[str, Any]], text: str, threshold: float = SIMILAR) -> dict[str, Any] | None:
    best, score = None, threshold
    for i in items:
        s = similarity(i.get("text", ""), text)
        if s >= score:
            best, score = i, s
    return best


def _streak(text: str, kind: str, reports: list[dict[str, Any]]) -> list[str]:
    """Dates of consecutive reports (from the start of `reports`) that contain a similar item."""
    dates = []
    for r in reports:
        if _find(_items(r, kind), text) is None:
            break
        dates.append(r["date"])
    return dates


def compute_trends(
    current: dict[str, Any] | None, history: list[dict[str, Any]], *, today: str
) -> list[Signal]:
    """`current`: the report under review (or None); `history`: earlier reports, newest first."""
    signals: list[Signal] = []
    reports = ([current] if current else []) + list(history)
    if not reports:
        return signals
    newest = reports[0]

    if current and history:
        done_now = _items(current, "done")
        for p in _items(history[0], "plan"):
            text = p.get("text", "")
            if _find(done_now, text) is not None:
                continue
            dates = _streak(text, "plan", history)
            signals.append(
                Signal(
                    "delayed_plan",
                    text,
                    len(dates),
                    dates,
                    {"in_progress_today": _find(_items(current, "progress"), text) is not None},
                )
            )

    for item in _items(newest, "progress"):
        text = item.get("text", "")
        dates = [newest["date"]]
        for r in reports[1:]:
            match = _find(_items(r, "progress"), text)
            if match is None:
                break
            if item.get("progress") and match.get("progress") and item["progress"] != match["progress"]:
                break
            dates.append(r["date"])
        if len(dates) >= STALLED_REPORTS:
            signals.append(Signal("stalled", text, len(dates), dates, {"progress": item.get("progress")}))
        eta = item.get("eta")
        if eta and eta < today:
            signals.append(Signal("overdue", text, 1, [newest["date"]], {"eta": eta}))

    for item in _items(newest, "done"):
        text = item.get("text", "")
        earlier = [r["date"] for r in reports[1:] if _find(_items(r, "done"), text, SAME) is not None]
        if earlier:
            signals.append(Signal("repeated_done", text, len(earlier) + 1, [newest["date"], *earlier]))

    for item in _items(newest, "blocker"):
        text = item.get("text", "")
        dates = _streak(text, "blocker", reports)
        if len(dates) >= BLOCKER_REPORTS:
            signals.append(
                Signal("lingering_blocker", text, len(dates), dates, {"severity": item.get("severity")})
            )
    return signals


def on_track(report: dict[str, Any] | None, signals: list[Signal]) -> bool:
    """Code-side status used for team stats: submitted, no high-severity blocker, no
    stalled/overdue work and no plan delayed twice or more."""
    if report is None:
        return False
    if any(b.get("severity") == "high" for b in _items(report, "blocker")):
        return False
    for s in signals:
        if s.type in ("stalled", "overdue") or (s.type == "delayed_plan" and s.reports >= 2):
            return False
    return True
