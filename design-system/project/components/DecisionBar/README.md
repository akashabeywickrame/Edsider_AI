The three reviewer choices for a flagged pair: Confirm duplicate (primary), Not a duplicate (secondary), Skip (ghost).

**Consumer provides:** `onConfirm`, `onDistinct`, `onSkip`, optional `hint`.

- Confirm and Not a duplicate each write a `decisions` row. Skip saves nothing and hides the pair for this session only - say so if asked.
- Never add a Merge or Delete action here. The system suggests; a person decides; nothing is removed automatically.
- After a verdict, remove the pair from the list and show a success `Notice` naming the pair and where it is listed.
