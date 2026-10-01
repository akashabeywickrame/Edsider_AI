An extracted question on the Paper intake page: its label, marks, editable text, answer, MCQ options, and the Reviewed tick.

**Consumer provides:** `label` ("Part II Q01", "01(a)"), `text`, `marks`. Optional: `depth` (0 question, 1-2 parts - indents), `kind: "stem"` (shared stimulus of a structured question), `options` + `correct` (MCQ), `answer`, `reviewed`, `editable` (false for the teacher view).

- An MCQ's options render as an editable `McqEditor` (fix each option, tick the correct one, add or remove) unless `editable` is false, where they fall back to read-only `McqOptions`.
- A stem is labelled as a shared stem and is not duplicate-checked on its own.
- The duplicate check stays disabled until every draft is ticked reviewed.
- Parts render directly under their question, in tree order, not upload order.
