# Evals

Regression cases for the skills, run against real LLM endpoints.

- Every `skills/*/<skill>/examples/*.json` is a case. Few-shot examples are included,
  so a prompt change that breaks the reference output shows up here.
- Extra cases go in `evals/cases/<skill>/*.json` (same format; put the 20-30 anonymised
  real reports here - good, vague and contradictory ones). Do not commit
  un-anonymised data.
- Each endpoint is evaluated on its own with failover off.

```bash
cd backend
python -m evals --list                          # show cases, no calls
python -m evals                                 # all skills x all ready endpoints (billed)
python -m evals --skill review-member --endpoint corp-gateway
```

`expect` keys (all optional): `status`, `min_items` (`{kind: n}`), `questions`
(`nonempty` / `empty`), `max_questions`, `contains`, `not_contains` (checked against
the rendered Markdown), `all_sources`, `min_issues`, `max_issues`.

Results are written to `evals/results/` (git-ignored). The runner exits non-zero
when a case fails, so CI can gate on it once the endpoint keys are available as
CI secrets.
