One question from the bank: id, full text in `body-lg`, grade - subject - topic, contributor, and other assessments it appears on.

**Consumer provides:** `id`, `text`, `grade`, `subject`, `topic`. Optional: `number` (position on the paper; omit EdSider's int32-max sentinel), `contributor`, `assessments` (ids), `lang` (`si`, `ta`, `en` - shows a `LanguagePill` for Sinhala and Tamil), `badge` (a node for the head's right side).

- Missing metadata reads "not set" or "untagged", never blank.
- Text keeps its own line breaks (`pre-wrap`).
