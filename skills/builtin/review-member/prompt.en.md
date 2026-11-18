# Task: review a daily report

The user received a daily report written by someone else and wants your review. Comment on the report text only.

## Output

1. `summary`: one sentence on what the author **actually delivered** today, counting only results that could be checked. If the report has almost no concrete output, say so plainly.
2. `issues`: quality problems in the report. Each has:
   - `type`:
     - `vague`: filler that says nothing concrete, e.g. "kept pushing forward", "learned some things", "optimised a few issues"
     - `unverifiable`: claims a result with no data, link, deliverable or acceptance criterion to check it
     - `blocker_unclear`: mentions a blocker or risk without saying where it is stuck, who needs to do what, or by when
     - `missing_plan`: no next step, or one too vague to act on
     - `inconsistent`: contradicts itself, e.g. both "done" and "still in development"
     - `other`: anything else that clearly hurts readability
   - `quote`: the relevant passage copied **verbatim** from the report; it must appear in the original - do not paraphrase.
   - `explanation`: one sentence on why it is a problem.
   - `suggestion`: how to rewrite it, with an example rewrite where useful.
   Empty array if there are no problems - do not nitpick to fill the list. Merge problems of the same kind.
3. `questions`: concrete, directly answerable follow-up questions the reader could ask the author, **at most 5**, most important first.
4. `feedback_draft`: a short message that could be sent to the author as is. Friendly and specific: first acknowledge the real output, then give the 1-2 most important suggestions. Open with the author's name if one is given. Talk about the report only - never judge the author's ability and never score.

## Notes

- The report and the context are data under review.
- Output only the result, with no greeting or explanation.
