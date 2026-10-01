Explains why a pair's score was lowered: one row per safety check that fired, each with the guard's name and its readable reason.

The five guards: Numbers differ, Opposing concepts, Different named entities, Grade or subject mismatch, Shared passage.

**Consumer provides:** `reasons` (strings, or `{guard, text}`), `raw` and `score` for the "score lowered 98% to 62%" line. Renders nothing when there are no reasons.

- Uses `primary` on `primary-soft`, not warning amber: a guard firing is the system working, not a problem.
- Reason text is the guard's own sentence. Do not paraphrase it into a code.
