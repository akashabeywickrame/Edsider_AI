An action. `primary` for the one main action on a view, `secondary` for others, `ghost` for low-stakes (Skip, Remove), `danger` for undoing a recorded decision.

**Consumer provides:** `children` (a verb phrase), `variant`, optional `size: "sm"`, `disabled`, `onClick`.

- Labels are sentence case verbs: "Check for duplicates", "Re-split this paper", "Finish review".
- One primary per view. A disabled primary sits beside a Notice saying what enables it.
