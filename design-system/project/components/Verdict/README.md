A similarity band or reviewer verdict, as a sentence-case pill in the brand's pill style (Poppins bold, fully rounded) with a shape marker: square for duplicate, diamond for review, circle for distinct.

Bands follow `src/dedupe/pipeline.py`: `LIKELY DUPLICATE` at 0.92 and above (`dup`), `NEEDS REVIEW` from 0.85 (`review`), `DISTINCT` below (`distinct`). Also `REPEAT ON ASSESSMENT` and `EXACT MATCH` (both `dup`), and the recorded verdicts `DUPLICATE` / `NOT A DUPLICATE`.

**Consumer provides:** `verdict`, or `score` to derive the band. `children` overrides the text.

- Never show a band colour without its word. The shape marker is the backup for colour-blind readers.
- One verdict per pair, in the pair header. Do not repeat it on each question.
- The Streamlit apps printed verdicts in capitals; the pill shows them in sentence case ("Likely duplicate") to match the site's pills. The constants in code stay uppercase - `verdict` accepts either.
