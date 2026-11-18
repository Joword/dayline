# Member tools (MCP)

Anyone on the team can write an MCP server, in any language, and Dayline will use it as a tool: look up a Jira sprint, count code reviews, read an internal wiki. After CI checks and a developer review, a team skill lists it in `tools:` as `<tool>__<function>`.

```
copy a template → write the tool → try it locally (dl tools check) → pull request
 → CI: lint, dependency audit, image build, hardened run, contract test + examples
 → merged: image pushed and submitted by digest (pending)
 → a developer reviews the code, description text and permissions → approve (approved)
 → deploy the pinned image (make tools-up) → a developer enables it (enabled)
 → a team skill references it
```

## 1. Start from a template

```bash
cp -r tools/templates/python tools/jira-sprint      # or tools/templates/ts
```

Edit `tool.yaml`:

| Field | Meaning |
|---|---|
| `name` | Lowercase letters, digits and `-`, unique across Dayline. The container is named `tool-<name>` |
| `owner` | Your Dayline user id. The tool stops working if the owner is deactivated |
| `version` | `x.y.z`. Every new version needs a fresh review |
| `language` | `python` and `typescript` get lint and a dependency audit automatically. Other languages are checked by the reviewer |
| `image` | Image name without a tag. CI submits the digest after the build |
| `permissions` | Dayline data the tool may read: `read:own_reports`, `read:own_activity`, `read:team_reports` |
| `network_egress` | External hosts it may reach: `jira.company.com` (ports 80 and 443), `git.corp:8443`, `*.corp.com` (subdomains) |
| `visibility` | Who can use it: `private` (only you), `team` (everyone under your manager), `level` (one level higher), `company` (everyone) |
| `manager_data` | Whether it touches manager data. Required, and `true`, when requesting `read:team_reports`. Two developers must approve |
| `timeout_s` | Per-call timeout, at most 60 seconds |
| `resources` | `cpus` (at most 2), `memory` (for example `256m`), `pids` |

## 2. Write the tool

- Tool names match `[A-Za-z0-9_-]` and must not contain `__`. `<name>__<function>` is at most 64 characters.
- **The description is inserted into the model's prompt**, so it is reviewed with the code. Say what the tool does and what its arguments are. Do not write "you must call me first" or "ignore other instructions", do not include URLs or hidden characters, and do not ask for passwords or tokens. The contract check blocks these or flags them for the reviewer.
- Return plain text, or text containing JSON. Results are cut at 20,000 characters and wrapped as untrusted input before the model sees them. On failure, return `isError`.
- When the tool needs data from Dayline, use the short-lived grant that comes with the call (below). Do not store user data yourself.

### Reading Dayline data

Each call's `_meta` carries:

| Key | Contents |
|---|---|
| `dayline/grant` | A short-lived grant (the call timeout plus 30 seconds) for the current user and the permissions approved in `tool.yaml` |
| `dayline/api_url` | The Dayline API on the internal `tools` network, for example `http://api:8000` |
| `dayline/lang`, `dayline/date` | Language and the report date |

Call with `Authorization: Bearer <grant>`:

| Path | Permission | Returns |
|---|---|---|
| `GET /internal/tool/me` | — | Current user id, name and language |
| `GET /internal/tool/reports?limit=5&before=` | `read:own_reports` | The caller's recent reports, without the raw narrative |
| `GET /internal/tool/activity?date=` | `read:own_activity` | The caller's git and calendar activity for that day |
| `GET /internal/tool/team?date=` | `read:team_reports` | Submission status and reports for the caller's team, when the caller is a manager |
| `GET /internal/tool/reports/{user}/{date}` | `read:team_reports` | One direct report's report for one day |

Every read follows Relay's visibility rules and is audited as `tool:<name>`, including reads of the caller's own data. `my_recent_plans` in the templates is a complete example.

## 3. Try it locally

```bash
cd tools/jira-sprint
pip install -r requirements.txt
DAYLINE_TOOL_DEV=1 python server.py                 # no token check, listens on :8080
# another terminal, at the Dayline repo root, with dl on PATH:
dl tools check tools/jira-sprint --url http://localhost:8080/mcp
```

For the TypeScript template: `npm ci && npm run build && DAYLINE_TOOL_DEV=1 node dist/server.js`.

You can also point the [MCP Inspector](https://github.com/modelcontextprotocol/inspector), or Claude Code or Cursor, at `http://localhost:8080/mcp`.

`dl tools check` validates `tool.yaml` and `tools/list` (names, descriptions, JSON Schema, hidden characters, suspicious wording) and runs `examples/*.json`:

```json
{"name": "count-words", "tool": "count_words", "input": {"text": "a b"}, "expect": {"is_error": false, "contains": ["words: 2"]}}
```

Examples run in CI without a Dayline grant, and outbound network is not guaranteed, so cover the "no grant" behaviour. See `plans-outside-dayline.json` in the templates.

## 4. Submit and CI

`make tools-review TOOL=tools/<name>` runs `tools/ci/review.sh` locally and needs Docker. It checks only. `tools/ci/review.sh tools/<name> --submit` pushes the image and submits `tool.yaml`, the image digest and `tools/list`, and needs a registry plus `DAYLINE_URL` and `DAYLINE_API_TOKEN`.

CI runs the container the way production does: a read-only filesystem (only `/tmp` is writable, 64 MB), all capabilities dropped, uid 65532, and memory and process limits. A request without the token must return 401.

## 5. Review and release (developers)

```bash
dl tools list --all                     # every status
dl tools show jira-sprint               # review view: permissions, egress, visibility, contract findings, the exact text the model will see
dl tools approve jira-sprint -u qianqi  # the owner cannot approve their own tool; manager_data tools need --ack-manager-data from two developers
dl tools reject jira-sprint -u qianqi -n "the description steers the model into calling it"
make tools-up                           # write docker-compose.tools.yml and deploy approved / enabled tools
dl tools enable jira-sprint -u qianqi   # the running tools/list must hash to the approved definition
dl tools disable jira-sprint -u zhangsan -r "upstream migration"   # the owner or a developer
dl tools verify                         # check by hand (the worker rechecks every 10 minutes)
```

Approval locks the hash of `tools/list`. The hash is compared before every call and on the worker's regular check. If the definition changed, the call is refused, the tool is disabled, and the developers and the owner are notified. The model only ever sees the text recorded at approval.

Submitting a new version disables the old one immediately, until the new version is approved and enabled. The next `make tools-up` removes the container of a tool that is waiting for review.

## 6. Use it from a skill

```yaml
# skills/team/sprint-helper/skill.yaml
name: sprint-helper
version: 1.0.0
description: Answer questions about sprint progress
inputs:
  - {name: question, type: text, required: true}
tools: [jira-sprint__sprint_status]
max_tool_turns: 2
permissions: [read:own_reports]     # must include every permission the tool requests
```

At run time a tool is offered only when the current user can use it. It is left out, with a note in the result, when it is not enabled, the user is outside its visibility, the skill does not declare the tool's permissions, or the skill reads team data while the tool is not approved for `manager_data`.

## Runtime isolation

- One container per tool, only on the internal `tools` network. It mounts no secrets and cannot reach MongoDB. The only credential inside is its own token, `DAYLINE_TOOL_TOKEN`.
- Dayline calls the tool with that token as the bearer, so another tool on the same network cannot impersonate Dayline.
- Outbound traffic goes only through `egress-proxy`, and only to hosts listed in `network_egress`. `HTTPS_PROXY` and `HTTP_PROXY` are set. Python (`httpx`, `requests`) uses them by default. Node's built-in `fetch` uses them because `NODE_USE_ENV_PROXY=1` is set (Node 24).
- The gateway does not route `/v1/internal/tool/*`. Tool callbacks stay on the `tools` network.
