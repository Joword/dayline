# Task: map Dayline reports onto the company's DingTalk log template

The company's DingTalk log template has a set of fields (e.g. "Done today", "Not finished", "Needs coordination", "Plan for tomorrow"). Map the content of a Dayline report onto these fields and produce a mapping draft for an administrator to confirm.

## Content available in a Dayline report (pick `source` values only from here)

| source | meaning |
|---|---|
| `own_items[kind=done]` | completed items |
| `own_items[kind=progress]` | in-progress items (with progress and ETA) |
| `own_items[kind=blocker]` | blockers / items needing someone else |
| `own_items[kind=plan]` | plan for tomorrow |
| `plan_check` | check against the last plan |
| `team_summary.summary` | team overview (manager reports only) |
| `team_summary.highlights` | team highlights (manager) |
| `team_summary.risks` | team risks (manager) |
| `team_summary.escalations` | escalations (manager) |
| `team_summary.missing` | members without a report (manager) |

## Rules

1. `dingtalk_field` must match a template field name **exactly**; if `fields` is given, it is authoritative.
2. One mapping entry per template field; a field may combine several sources, e.g. "Needs coordination" = blockers + escalations.
3. `format`: lists of items use `numbered_list` or `bullet_list`; a paragraph (e.g. the team overview) uses `text`.
4. Content with no matching field is handled by `unmapped_policy`: default `append_to_last` (appended to the last field) so nothing is lost.
5. Put unclear fields, or content without a good home, into `notes` for the administrator. Never invent fields the template does not have.
