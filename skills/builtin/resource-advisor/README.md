# resource-advisor (reserved, plan §4.6)

Not implemented in this phase. This directory has no `skill.yaml` on purpose, so the
registry does not load it. The `allocations` collection schema is defined in
`backend/app/db/models.py` (`AllocationDoc`).

Design principles already agreed:

1. **Metrics are computed by code** (load per person from submitted reports, blockers,
   overdue / stalled items from `app/rules/trends.py`), never estimated by the model.
2. **The model only advises at team / task level**, each suggestion with its evidence
   (which items, which signals). No ranking or scoring of individuals.
3. **A person decides**: a manager records the decision as an allocation; nothing is
   assigned automatically.
4. **The outcome is followed up**: the decision is carried into the next day's plan and
   tracked (`AllocationDoc.follow_up`).

To build it: add `skill.yaml` (roles: [manager], a `team_load` context provider computed
by code, output schema with suggestions + evidence), a confirm flow that writes
`allocations`, and a hook that adds decided allocations to the affected people's next
`generate-daily` context.
