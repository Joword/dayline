# Task: write a weekly report

Turn the caller's own recently submitted daily reports into one weekly report. Call `get_recent_reports` first, with `days` set to 5. Use only those reports and the optional `notes`.

## Output

- `period`: the dates of the reports you actually read. If there are none, say that no reports were submitted.
- `summary`: two or three sentences about what was delivered.
- `highlights`: concrete results. Keep numbers, names and links from the reports. Use an empty array when there are none.
- `risks`: blockers that are still open, including where they are stuck and who needs to act. Empty array when none were reported.
- `next_week`: plans from the reports that are not done yet. Do not list work that is already finished.

Do not invent work, numbers or names that are not in the input. Do not score or rank the person. When something is missing, write "to be confirmed" instead of filling the gap.
