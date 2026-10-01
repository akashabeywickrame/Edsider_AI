The core review unit: two flagged questions side by side, their similarity, any safety checks that fired, and the verdict buttons.

Replaces the `### :red[LIKELY DUPLICATE] 97% similar` header + `st.columns(2)` + `st.warning` + three buttons pattern repeated on Find duplicates, Duplicates within a paper, and the intake duplicate check.

**Consumer provides:** `score` (0-1, after guards), optional `raw` (before guards), `a` and `b` as `QuestionCard` props, `reasons` (guard reasons), and `onConfirm` / `onDistinct` / `onSkip`. Optional: `verdict` (defaults to the band for `score`; pass `"REPEAT ON ASSESSMENT"` on the within-paper page), `context` ("Assessment #1127"), `decision` (an existing verdict, shows the Already reviewed note), `passageNote` (shared-passage annotation), `readOnly`.

- Always show both full texts. A reviewer decides from the words, never from the number.
- When `raw` > `score`, the guard note must be visible: a demoted pair explains itself.
- Left is the lower id. Keep that stable so a pair reads the same on every visit.
- Stack the questions below 720px; never truncate question text.
