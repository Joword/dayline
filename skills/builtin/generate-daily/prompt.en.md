# Task: write a personal daily report

Turn the user's own account of today's work into a structured daily report.

## Steps

1. **Check the last plan**: first call `get_recent_reports` with `days` = 1 to fetch the previous confirmed report. If it returns one, compare each of its items whose `kind` is `plan` with today's account. If there is no earlier report or storage is unavailable, skip this step.
2. **Split and classify**: break the account into separate items, each of one kind:
   - `done`: finished today, with a clear result.
   - `progress`: started but not finished. Fill `progress` / `eta` only when the account shows them.
   - `blocker`: stuck and needing someone else. Say where it is stuck, put who is needed in `need_from`, and judge `severity`.
   - `plan`: what the user intends to do next. Fill `priority` when the account makes it clear.
3. **Wording**: one sentence per item, leading with the result rather than the activity. "Shipped payment-callback retries; failure rate down from 2% to 0.3%" beats "Worked on payment callbacks". Keep the concrete numbers, system names and objects from the account; never add numbers, results or progress the account does not give. Do not split one piece of work into several items.
4. **Source**: every item needs `source`. The user's own account is the main source. The input `own_activity` may carry today's git commits / PRs (`git`) and meetings (`calendar`), already preprocessed:
   - work the account mentions and the git data confirms (a matching PR / commits): `source` = `git`, `ref_url` = that link, `evidence.commits` = the number of related commits;
   - work that appears only in the git data: you may list it (`source` = `git`), but set `pending_confirmation` to `true` when the result is unclear;
   - meetings with a clear outcome (reviews, decisions) may become items with `source` = `calendar` and `evidence` holding the meeting count and hours; routine stand-ups need no item;
   - git and calendar data show activity, not results: never infer output from commit counts and never invent meeting outcomes;
   - when `own_activity` is unavailable (`available: false`) or empty, every item is `manual`. Use only links that appear in the input for `ref_url`.
5. **Plan check** `plan_check`: list each plan item of the previous report with its status from today's account: `done`, `in_progress`, `not_started` or `dropped`. If today's account does not mention it at all, use `unknown`; do not guess. Empty array when there is no earlier report.
6. **Questions** `questions`: when something vague or missing would clearly hurt the report, ask - **at most 3**, only the most important, each concrete and directly answerable. For example: "Which endpoint did 'improved performance' refer to, and what were the timings before and after?" A plan item from last time that today's account leaves out, or a long meeting in the calendar the account does not mention ("The calendar shows a 2-hour reconciliation review this afternoon - what was decided?"), is also worth one question. Empty array when the information is enough. If the user already answered in `answers`, fold the answers into the items and do not ask again.
7. **Cross-team dependencies**: set `cross_team` to `true` when an item depends on another team (waiting for their API, docs, approval) or affects another team. Peer team leads can see these items, and only these.
8. **Uncertain content**: set `pending_confirmation` to `true` on any item that relies on your inference (e.g. inferred progress or completion).

## Output

- `date` is the report date; `lang` is `en`.
- Output only the result, with no greeting or explanation.
