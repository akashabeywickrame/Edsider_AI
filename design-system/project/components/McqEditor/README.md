Editable answer options for an extracted MCQ on the Paper intake page: one text box per option, a Correct tick, Remove, Add option, and a switch between one and several correct answers.

It follows EdSider's own question editor (`QuestionEditorForm` in OAT-WEB-2.0: "Choice N", "Correct Answer", Remove, add option, answer type One / Many) so data-entry staff meet the same controls in both tools, and so a promoted question maps straight onto EdSider's MCQ format: `content` = the stem plus one block per option, `answer` = one mark per option ("0,1,0,0").

**Consumer provides:** `options` (`[{label, text}]` from the extractor), optional `correct` (the correct label, or several comma-separated), `many` (start in "More than one" mode). Save the options' text, order and correct flags with the draft.

- Every option stays editable - OCR and PDF extraction often split, merge or garble options; the reviewer fixes them here rather than in the stem.
- The same checks EdSider's editor enforces, shown inline as `review` text: at least two options, none empty, at least one correct, exactly one when "One correct" is selected. The draft cannot be ticked Reviewed while one fails.
- Switching back to "One correct" keeps the first ticked option and clears the rest.
- Read-only places (teacher review, the phone view) use `McqOptions` instead.
