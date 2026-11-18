"""Local tool: the current user's own recent reports (for plan-vs-actual comparison)."""

from __future__ import annotations

from app.tools.registry import ToolContext, tool


@tool(
    name="get_recent_reports",
    description=(
        "Read the current user's own most recent submitted daily reports (before the report date), "
        "newest first. Use it to compare today's work with the previous 'plan' items and spot delays. "
        "Returns only the current user's reports."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 7,
                "description": "How many reports to return",
            },
        },
        "required": ["days"],
        "additionalProperties": False,
    },
    permission="read:own_reports",
)
async def get_recent_reports(ctx: ToolContext, days: int) -> object:
    if ctx.relay is None or ctx.viewer is None:
        return {"available": False, "reason": "report storage is not connected; no history"}
    # Identity comes from the run context, never from the model: only the caller's own reports.
    views = await ctx.relay.history(ctx.viewer, ctx.viewer.user_id, before=str(ctx.date), limit=days)
    if not views:
        return {"available": True, "reports": [], "note": "no earlier reports"}
    return {
        "available": True,
        "reports": [
            {"date": v.date, "items": [{"kind": i.get("kind"), "text": i.get("text")} for i in v.own_items]}
            for v in views
        ],
    }
