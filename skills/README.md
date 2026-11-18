# Skills

A skill is a directory. The system scans three layers:

| Layer | Who writes it | How it ships |
|---|---|---|
| `builtin/` | core developers | baked into the image |
| `team/` | anyone on the team | volume mount, hot-reloaded |
| `user/` | individuals | volume mount, hot-reloaded (not in git) |

Names must be unique across layers: a `team/` or `user/` skill cannot reuse a built-in name.

## Adding a skill (no Python needed)

```
skills/team/weekly-rollup/
├─ skill.yaml        # manifest (format below)
├─ prompt.zh.md      # instructions in Chinese
├─ prompt.en.md      # optional: English instructions
├─ schema.json       # optional: JSON Schema of the output
└─ examples/         # optional: few-shot examples and eval cases
   └─ basic.json
```

Hot reload picks it up within a couple of seconds; `dl skills list` shows it (and any
validation problems), and it is runnable with `dl run weekly-rollup` and
`POST /skills/weekly-rollup/run`.

### skill.yaml

```yaml
name: weekly-rollup              # must equal the directory name
version: 1.0.0
description: Turn this week's reports into a weekly summary   # shown in lists, used for routing
roles: [manager]                 # people with direct reports only
triggers:
  commands: ["/weekly", "dl weekly"]
  intents: ["write the weekly report", "weekly summary"]
inputs:
  - {name: date_range, type: date_range, required: true}   # text|string|date|date_range|enum|int|bool|json
  - {name: notes, type: text}
context: [team_snapshot]         # data fetched by code through Relay before the model runs
tools: [get_recent_reports]      # whitelist, enforced at call time
rules: [input-size]              # deterministic checks (app/rules)
model: large                     # small | large
fallback: allow                  # allow | deny (deny = primary endpoint only)
max_tool_turns: 2
output:
  schema: schema.json            # built-in skills may use schema.py:Model instead
  render: [markdown]
  template: generic              # daily_report | review | generic
permissions: [read:own_reports, read:team_reports]   # what the context and tools need
```

### Data: `context` vs `tools`

Prefer `context`. A context provider (`backend/app/skills/providers.py`) is called by
the runner *as the requesting user*, so Relay's visibility rules decide what the skill
gets; the model never chooses whose data it sees. Providers can also return fields the
code owns (counts, missing members, source report versions), merged over the model's
output after validation.

| Provider | Gives | Permission |
|---|---|---|
| `member_report` | a direct report's report for `date` (input `member`) | `read:team_reports` |
| `member_trends` | cross-day signals for that member | `read:team_reports` |
| `team_snapshot` | all direct reports' submitted reports for `date` + stats / missing | `read:team_reports` |
| `own_activity` | the user's own preprocessed git commits / PRs and meetings for `date` | `read:own_activity` |
| `member_activity` | a direct report's activity, per source only with their consent | `read:team_activity` |

`{name: own_activity, optional: true}` lets a skill run when the data is unavailable (no
storage, user not in the org, no consent): the context then says `available: false` and why.

Examples of skills with `context:` carry the recorded provider data under `"context"`;
few-shot prompts show it and evals replay it without a database.

Tools are for data the model decides it needs mid-task (`get_recent_reports`,
permission `read:own_reports`). A skill must list in `permissions` every permission its
context providers and tools need; the registry rejects it otherwise.

`roles: [manager]` (without `member`) limits a skill to people with direct reports.

`schema.py` is accepted only for built-in skills: team and user skills are mounted
from volumes, and running their Python inside the API process would give them its
secrets and database access. Use `schema.json` (JSON Schema) there.

### examples/*.json

```json
{
  "name": "basic",
  "lang": "zh",
  "fewshot": true,
  "inputs": {"notes": "..."},
  "output": {"...": "expected output, used as a few-shot example when fewshot is true"},
  "expect": {"status": "ok", "contains": ["..."]}
}
```

Every example is also a regression case for `dl eval`.

### Prompt variants

`prompt.zh.corp.md` is used instead of `prompt.zh.md` when the request is served by an
endpoint with `prompt_variant: corp` (see `config/llm_endpoints.yaml`).
