# Project context for Claude Code

## What this is

An AI duplicate-detection and semantic-search system for **EdSider**, a Sri
Lankan online exam-practice platform (Grades 6-13, English/Sinhala/Tamil).

Built by Akash Abeywickrama as an internship assignment. It is a working
prototype of "Project 1" from a three-part proposal:

1. **Duplicate detection and semantic search** ← this project
2. Reading question papers and auto-filling labels (not built)
3. Generating new questions (not built)

Projects 2 and 3 both depend on Project 1, which is why it was built first.

## The problem

EdSider's teachers write questions well. The slow part is everything after:
splitting files into questions, labelling them, checking they aren't already
in the bank, and uploading. Duplicate checking is effectively impossible by
hand - checking one new question against a bank of 20,000 means reading
20,000 questions.

## How it works - three passes, cheapest first

**Pass 1 - exact match.** Normalise the text (case, whitespace, punctuation,
Unicode NFC) and hash it with SHA-256. Identical questions share a hash.
Free, instant, exact. No AI.

**Pass 2 - meaning fingerprints.** Embed each question with
`intfloat/multilingual-e5-base` and compare with cosine similarity. Catches
questions that mean the same thing but share almost no words.

**Pass 3 - the guard layer.** This is the important part. Embeddings have a
dangerous failure mode: questions worded almost identically but testing
different things score very high.

    "Calculate the AREA of a rectangle 12 cm by 5 cm"      \
    "Calculate the PERIMETER of a rectangle 12 cm by 5 cm" /  score 0.977

They are NOT duplicates. Four guards lower a pair's score when a warning
sign appears - numbers differ, opposing concepts, different named entities,
mismatched grade/subject. Each returns a human-readable reason.

## The key measured finding

Measured early on, over a 30-question hand-written sample set (since
removed from the repo - see "Two databases" below for why, and for what
replaced it):

- Real duplicates scored **0.894 - 1.000**
- Hard negatives scored **0.914 - 0.977**

**These ranges overlap.** A real duplicate scores *below* a look-alike. No
threshold can separate them. Only rules that read what the questions
actually say can.

Threshold sweep from 0.70 to 0.99, on that same sample:
- Best achievable F1 **without** guards: **0.667**
- Best achievable F1 **with** guards: **1.000**

That is the central claim of the project, and it was measured, not
asserted - the sample set and `tune.py` that produced these two numbers
are gone now that the project is real-data-only, but the numbers stood
up again on the real bank in every subsequent measurement: the
whole-bank sweep, `find_within_assessment_duplicates()`, and
`find_cross_passage_duplicates()` each independently found that the
raw embedding score alone floods a reviewer with false positives, and
each needed its own threshold and (for two of the three) a filter,
found by measuring the real bank rather than assumed - see those
sections further down for the actual numbers each one turned out to
need.

## Layout

```
app.py                  Streamlit UI (5 pages, guard on/off toggle)
service.py              FastAPI service - the check, always on
                        (model + fingerprints load once, at startup)
src/
  normalize.py          text extraction, normalisation, hashing
  embeddings.py         model wrapper (E5 prefix handling)
  guards.py             the four guards + contrast-term mining
  dedupe.py             full pipeline (whole-bank, within-paper,
                        cross-passage) + thresholds
  store.py              ALL database access - the only file that
                        changes when moving to PostgreSQL + pgvector
scripts/
  import_edsider.py     load a real EdSider export (CSV or JSON)
  embed.py              compute fingerprints
  find_duplicates.py    Pass 1 only
  check_new.py          one question against the bank, all three passes
  sync.py               pull new questions from EdSider's own Postgres
tests/                  148 tests
data/                   the real database lives here (gitignored -
                        EDSIDER_DB must point at it explicitly, see
                        "Two databases")
```

## Running it

```
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install -r requirements.txt

.\venv\Scripts\python.exe -m pytest tests\ -v

$env:EDSIDER_DB = "data/edsider_real.db"
.\venv\Scripts\python.exe scripts\import_edsider.py path\to\export.csv
.\venv\Scripts\python.exe scripts\embed.py --language English
.\venv\Scripts\streamlit.exe run app.py
```

`embed.py` downloads the model on first run, then caches it. There is no
default database any more - `EDSIDER_DB` must be set before any script
that touches storage, or `store.connect()` refuses to run (see "Two
databases").

## Conventions to follow

- **No emoji anywhere** - not in code, comments, output or UI.
- **Comments explain WHY, not what.** The reader can see what the code does.
- **`store.py` is the only module that touches the database.** Keep it that
  way; it is the seam for swapping to PostgreSQL.
- **Cheapest technique first.** Rules before embeddings, embeddings before
  any LLM. Of thirteen capabilities in the proposal, only three genuinely
  need a language model.
- **Every guard returns a readable reason**, so a reviewer sees *why* a
  pair was demoted rather than a bare number.
- **Nothing is merged or deleted automatically.** The system suggests; a
  person decides. Every decision goes to the `decisions` table.
- Tests must pass before anything is considered done.

## Things that were learned the hard way

**The E5 prefix is mandatory.** `multilingual-e5-*` models require a
`query:` or `passage:` prefix on every input. Omitting it degrades quality
noticeably. Handled in `Embedder._prefix()`.

**Unicode NFC matters for Sinhala and Tamil.** The same visible character
can have more than one byte representation. Python's `\w` also strips their
vowel signs and viramas - `ලකුණු` becomes `ලකණ` - so the normalisation regex
explicitly keeps the Sinhala (0D80-0DFF) and Tamil (0B80-0BFF) blocks.

**EdSider stores some fields as Draft.js rich-text JSON**, and may hold a
mix of that and plain text. `extract_text()` handles either, so formatting
differences never affect the fingerprint.

**Duplicates are transitive.** If A=B and B=C then A=C. The old
sample-set scorer (`evaluate.py`, removed - see "Two databases")
computed this closure with union-find; without it, it would have
punished the system for being correct. Worth remembering when a
decisions-table-based scorer eventually replaces it (see "Known
limitations").

**The entity guard needs a conflict on both sides.** One question mentioning
an extra place is not evidence of difference - a test caught this using
"independence from British rule" against "independent from Britain".

## The real EdSider bank - what is actually in it

Measured on a 684 MB export, 41,139 rows, all English. This is the shape
the code now has to survive, and most of it was not what the sample set
implied. Everything below still holds - it describes structure (parent
rows, sub-questions, MCQ layout, `NULL`-as-string) that is the same
regardless of language. The live database, reached directly via
`sync.py`, turned out to be over twice this size and 60% non-English -
see "the language note" further down for what that changed.

**A question is not always a row.** EdSider stores a multi-part question
as a parent row holding the shared stimulus and the instruction, plus one
child row per part:

    id=842  sub_questions: 837,838,839,840,841
            "Write the answer ... using below food chain
             Grass -> Grass hopper -> Frog -> Snake"
    id=837  "13.How many links of the above food chain"

    standalone    17,874 rows   median 149 chars
    sub-question  19,244 rows   median  58 chars
    parent         3,824 rows   median  44 chars

A child cannot be judged on its own text - "How many links of the above
food chain" appears under six different food chains. A parent is an
instruction, and parents are **43% redundant** simply because "Fill in
the blanks" recurs on every paper. Only standalone rows without figures
are compared; `store.COMPARABLE` is that rule, and `shape` is set at
import by `row_shape()`.

**The passage problem is real here, just under another name.** It arrives
as `sub_questions` rather than `passage_id`. Prepending shared context to
the question is NOT the fix - measured, three different questions about
one passage score 0.978-0.987 against each other once the passage is
concatenated, so an 8-question comprehension paper yields 28 false
"likely duplicate" pairs. Scope by parent instead.

**MCQ options live inside `content`.** For `qtype=1` the field is a list
of Draft.js states: state 0 is the stem, the rest are the options.
`extract_text()` joins them, so an MCQ fingerprint covers stem + options.

**`NULL` arrives as the four characters N-U-L-L**, unquoted. Left alone
it becomes a string, two unset grades compare equal, and every fill rate
reads 100%. `_str()` in the importer converts it.

**Cells reach 6.5 MB** (base64 images), against a default `csv` field
limit of 128 KB. The importer raises the limit and streams rather than
building a list - 684 MB of dicts is several GB.

Pass 1 on the comparable subset of the CSV (English only): **348 duplicate
groups, 367 redundant copies, 2.3%** - and they are real questions,
entered by different contributors, not boilerplate. On the full live bank
via `sync.py` (all three languages, 41,712 comparable rows): **879 groups,
913 redundant, 2.2%** - consistent with the English-only figure, and
proof Pass 1 needs no embedding to already work across Sinhala and Tamil.

**Metadata cannot override identical text.** The sample set implied that a
grade or subject mismatch is evidence of a different question. On the real
bank it is usually evidence of inconsistent tagging: 42 exact-duplicate
groups span more than one grade and 17 more than one subject, and every one
inspected was the same question filed twice - the same computing question
under grades 10 and 11, "UPS stands for..." under two subjects. Guard 4 was
demoting 63 character-identical pairs. It now stays silent when the
normalised text matches.

**Penalties and thresholds are one setting, not two.** A 0.15 penalty at a
threshold of 0.78 demotes a pair; at 0.95 it needs a raw score of 1.10 to
survive, so it deletes it. Every guard becomes a kill switch as the
threshold rises. The sample set tuned to 0.78, but 0.85 on the real bank
yields **1.46 million** candidate pairs - unreviewable - so real thresholds
sit near 0.95, where the current penalties are far too strong. Re-tune both
together, on real labels, once there are enough - the tool that would do
this (`tune.py`) has been removed along with the sample answer key it
swept against; a real version needs building against the `decisions`
table instead (see "Known limitations").

Sweep scale, measured: the whole 128-million-pair matmul takes **1.6
seconds** once `VectorIndex` holds float32. Candidates above 0.97 / 0.95 /
0.92 are 2,222 / 5,735 / 25,670; after guards, 1,455 / 2,873 / 8,546.

## Known limitations

- **The concept-contrast list in `guards.py` is hand-written** and does not
  scale by itself, but the app now captures its own raw material for
  growing it. `guards.candidate_contrast_terms(text_a, text_b)` diffs the
  vocabulary of a pair - words in one text but not the other - and every
  "Not a duplicate" click on a flagged pair, in both `app.py` review pages,
  passes that diff into `store.record_decision()`'s new
  `candidate_contrast_terms` column (JSON, `decisions` table). Nothing
  reads this column automatically - it is deliberately just a capture
  step, following the same "the system suggests, a person decides" rule
  as everything else in this project. What is still missing: a review
  step (a script or a page listing the most common candidate pairs across
  many rejections) where a person promotes the real ones into
  `CONTRAST_GROUPS` by hand. That needs real rejections to accumulate
  first - as of this writing there are still zero in either database, all
  existing `decisions` rows being confirms - so the column exists and is
  wired in, but nothing has populated it yet.
- **Thresholds were originally tuned on 30 English sample questions**, via
  `tune.py`, which is now removed along with that sample set - the
  project is real-data-only from here on. They must be re-derived on
  EdSider's real bank instead, which needs a few hundred pairs labelled
  by a teacher (via the `decisions` table) and a new version of `tune.py`
  built to sweep against that instead of `ground_truth.json`.
- **Sinhala and Tamil are synced but deliberately not embedded yet.** The
  684 MB CSV export was English-only, which is why earlier notes here said
  the assumption was untested - but that was the export, not the bank.
  `scripts/sync.py`, pulling directly from EdSider's live Postgres, found
  the real bank is 103,337 rows: 41,139 English, 31,269 Sinhala (සිංහල),
  28,769 Tamil (தமிழ்), 1,395 with no language set. Over 60% of the real
  bank is Sinhala or Tamil. `embed.py --language English` scopes which
  language actually gets fingerprinted - deliberate, so English gets
  proven out first: `comparable_questions()` joins on the `embeddings`
  table, so an un-embedded language is simply invisible to duplicate
  checking until `embed.py` is run again for it. Nothing else needed to
  change for this scoping - Pass 1 (exact-hash) is language-agnostic and
  already runs across all three languages for free, since it needs no
  embedding at all.
- **The full sweep is still O(n^2).** `find_pairs` now compares a block of
  512 against the bank at a time, so memory is flat rather than the 2 GB a
  16,000 x 16,000 grid would need, and `VectorIndex` parses the vectors once
  instead of per call. But 16,000 questions is still 128 million pairs. An
  ANN index (pgvector HNSW) is the real answer. `check_one` does not have
  this problem - it is one question against n.
- **Sub-questions are not deduplicated.** 19,244 rows, 47% of the bank, are
  excluded because they need their parent for context. Doing them properly
  means scoping comparison to the same parent, which is the guard that was
  designed for passages and then dropped.
- **Only one language in the export.** All 41,139 rows are `language_id=1`,
  so Sinhala and Tamil still cannot be measured. The assumption stands
  untested.
- **`check_new.py` loads the model per invocation.** Acceptable for a CLI,
  wrong for a save-time hook. In EdSider it becomes an endpoint holding a
  warm model, and the score bands move behind an API rather than a print.

## The one-question check

`scripts/check_new.py` and `dedupe.check_one()` are the shape EdSider
actually needs - a teacher adds one question and asks whether the bank
already has it. Three things about it are deliberate:

- **Pass 1 runs before the model loads.** An exact match is one indexed
  lookup on `content_hash`, so the common case never pays for an embedding.
- **The new question is embedded as a `passage:`, not a `query:`.** The bank
  was built with passage vectors, and a duplicate check compares like with
  like. The `query:` prefix belongs to search, where a short query meets a
  longer document.
- **Demoted matches are still returned, with their reason.** A reviewer
  needs to see "this looked close, and here is why it is not the same"
  rather than silence. `find_pairs` drops them; `check_one` keeps them and
  lets the caller filter on `score >= threshold`.

`--add` refuses to write when anything was flagged. Nothing is added over
a flag without a person.

## Two databases (there used to be)

There used to be a 30-question hand-written sample bank
(`data/questions.json`), an answer key describing it
(`data/ground_truth.json`), and machinery built around scoring against
that answer key (`scripts/load.py`, `scripts/tune.py`,
`scripts/find_similar.py`, `src/evaluate.py`). All five are gone now -
removed once the project moved to being real-data-only, rather than
left around as dead weight nothing could run any more (`tune.py` has no
answer key to sweep against without a labelled set; there is no
decisions-table-based replacement yet - see "Known limitations").

That leaves one database, but the discipline that motivated keeping two
apart still matters, in a stricter form now: `store.DB_PATH` reads
`EDSIDER_DB`, and **there is no default any more**:

```
$env:EDSIDER_DB = "d:\EdsiderAI\data\edsider_real.db"
.\venv\Scripts\python.exe scripts\import_edsider.py edsider_questions.csv
```

Unset, `DB_PATH` is `None`, and `store.connect()` raises immediately
with a message naming `EDSIDER_DB`, rather than silently creating a
brand new empty database at a guessed path - which would be a worse
failure than the old one: every check would silently report "no
duplicates found" for everything, with no error and no visible sign
anything was wrong. The old sample-bank fallback was at least visibly a
demo; a silently-empty real bank is not.

The app still names the open database in the sidebar and offers a
selector over every `data/*.db`, for whenever more than one real bank
exists side by side (a staging copy, say) - `store.use_database()` is
the only supported way to repoint storage at runtime, and remains so.

The duplicates page defaults to a threshold of **0.95** on a bank over a
thousand questions rather than the old sample-tuned 0.85, and renders the
top 50 pairs. At 0.85 the real bank returns 1.46 million pairs, which is
neither reviewable nor renderable.

## The live service

`service.py` is `check_one()` wrapped as a FastAPI process instead of a
CLI invocation. The difference is entirely about *when* the expensive
part happens:

- **`check_new.py`** loads the model fresh every run - fine once from a
  terminal, wrong for anything asking "is this a duplicate?" repeatedly.
- **`service.py`** loads the model and builds `VectorIndex` once, in a
  `lifespan` hook, when the process starts. Every request after that is
  an embed-and-compare against memory - measured **4.6ms** for an exact
  match (Pass 1, no model touched) and **157ms** for a reworded question
  against all 15,998 comparable real questions (Pass 2+3, real model).

Three routes: `POST /check` (the work), `GET /health` (what it is
currently serving against - which database, how many comparable
questions, whether the model has finished loading), `POST /reload`
(re-reads OUR OWN database and rebuilds the in-memory index, without
restarting the process - cheap, because embedding is cached by
`content_hash` the same way `embed.py` already caches it). `GET /docs`
is FastAPI's own generated form for trying it by hand.

**`/reload` does not talk to EdSider.** It is the last, cheap step after
new rows have already landed in OUR database, whether that was
`import_edsider.py` on a fresh CSV or `sync.py` (below) pulling directly
from EdSider's own Postgres.

## Pulling directly from EdSider's database

`scripts/sync.py` is the live-connection path `/reload`'s docs used to
promise and didn't have: it queries EdSider's own PostgreSQL with a
read-only account and pulls rows newer than the last successful sync,
instead of someone exporting a CSV by hand each time.

It is NOT a second importer. `to_question()`, `row_shape()`, and
`COLUMN_MAP` in `import_edsider.py` already treat a row as nothing more
than "a dict with `.items()`" - true of a `csv.DictReader` row and a
`psycopg2` row alike - so `sync.py` calls straight into that same
mapping. The only new code is *where the rows come from* and *how far
the last pull got*:

- **Watermark, not full re-scan.** `sync_state` (one row per source) in
  OUR OWN database tracks the highest `question_id` already pulled.
  Each run asks Postgres for `WHERE question_id > last_seen` and, only
  after every row in that batch saves without error, advances the
  watermark to the highest id actually seen. A crash mid-batch leaves
  the watermark untouched, so the next run safely re-pulls and
  re-upserts the same rows rather than skipping any.
- **Read-only, twice over.** The account EdSider grants should have
  `SELECT` only - and `sync.py` also opens the session itself with
  `set_session(readonly=True)`, so a mistake in the query can't become a
  write even if the account somehow could. It only ever runs `SELECT`,
  and closes with `rollback()`, never `commit()`.
- **Streamed, not loaded.** A named (server-side) cursor with a
  `itersize` of 500 means the first-ever sync - potentially the whole
  41,000+ row bank, some cells 6.5 MB - never sits in memory at once,
  the same reasoning `import_edsider.py`'s CSV reader already uses.
- **The join fixes the "subject shows as 7" problem going forward.**
  `sync.py`'s query LEFT JOINs grade/subject/language lookup tables, so
  a live sync gets real names automatically - the CSV path only got
  numeric ids because that particular export never included the join.
  Table names in the join are unconfirmed and marked as such in the
  query's own comment; the id/name fallback in `COLUMN_MAP` means it
  still works correctly even if that join turns out wrong.

Needs `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD` - see
`.env.example`. `.env` itself is gitignored. This still does not close
the real-time gap by itself - something has to actually run `sync.py`
on a schedule, and that, plus whether the `question_id` gaps mean
hard-delete and whether `updated_at` can be added, is still the open
conversation with EdSider's backend team.

`tests/test_sync.py` fakes psycopg2's connection and cursor rather than
requiring a real server - same reasoning as everywhere else in this
suite - and proves the watermark, read-only, and crash-safety behavior
above concretely rather than by inspection.

`tests/test_service.py` calls the route functions directly with a fake
embedder rather than starting a server or loading the real model -
same reasoning as `test_similarity.py` and `test_check_new.py`: prove
the logic, not the model.

## Assessments - a question can belong to more than one paper

`title` on a question is a per-question code, not a paper title - it
was tried and ruled out as a way to identify which assessment a
question belongs to. The real link lives in a separate junction table,
`assessment.assessment_question` (columns confirmed directly against
the live schema: `assessment_question_id` PK, `assessment_id`,
`question_id`, `question_number`, `mark_allocation`, `section_id`,
`added_by`, `is_optional`). Junction table means many-to-many: the
same question legitimately appears on more than one paper - the same
item reused across two Grade 10 Term 1 papers is normal, not a bug.

That rules out a plain `assessment_id` column on `questions` (a
question in three papers has nowhere to put the second and third id),
and rules out folding it into `sync.py`'s existing `QUERY` (a `JOIN`
there would fan a question out into one row per assessment it belongs
to, breaking the one-row-per-question assumption `to_question()` and
the watermark both rely on). Instead:

- **`question_assessments`**, a local table mirroring the real one,
  keyed on `link_id` (their own `assessment_question_id`) so a re-synced
  row upserts rather than duplicates - same idempotency reasoning as
  `save()`'s `content_hash` keying.
- **A second query and a second watermark in `sync.py`**
  (`SOURCE_LINKS = "edsider_assessment_links"`), independent of the
  question sync - it can run, fail, or be re-run without touching
  question rows, and vice versa.
- **This data only reaches OUR database through `sync.py`.** The
  original CSV export never included `assessment_question` - it is a
  different schema entirely - so `import_edsider.py`'s CSV path has no
  way to populate it. A CSV-imported bank will show no assessment
  links until it is later kept in sync live.

Two different checks fall out of this, and only one is new logic:

- **Across assessments** - the same question, reworded or with
  different numbers, on two different papers - is already handled
  correctly by the existing whole-bank sweep: Guard 1 (`numbers_differ`)
  already stops a template-with-different-values from being flagged.
  `question_assessments` just adds visible context here (`store.
  assessments_for_question()`, surfaced in the UI as "Also appears on
  assessment(s): #6, #9") rather than changing any scoring.
- **Within one assessment** - the same question appearing twice on the
  SAME paper, MCQ or structured or essay - is a real problem.
  `dedupe.find_within_assessment_duplicates()` groups comparable
  questions by `assessment_id` (`store.questions_in_assessment()`) and
  runs the existing `find_pairs()` on each small group. Surfaced in the
  app as its own page, "Duplicates within a paper," separate from the
  whole-bank sweep.

  The first version of this skipped guards and used `REVIEW_THRESHOLD`
  (0.85), on the reasoning that a paper is small enough that a false
  alarm costs a reviewer nothing to dismiss. Measured against the real
  bank instead of assumed: **wrong**. 650 real papers with more than one
  comparable, embedded question produced **34,648** "repeats" at 0.85 -
  89% of them sitting at 0.85-0.90 and looking exactly like the
  whole-bank sweep's known false-positive shape, e.g. an MCQ about the
  Russian Revolution scored 0.90 against one about the French Revolution
  because their answer OPTIONS share vocabulary even though the
  questions do not. Guards barely help here (34,648 -> 27,829 with
  guards on, at 0.85) - MCQ option text carries too much shared
  vocabulary for the guard list to catch. The threshold is what matters:
  at **0.95**, 650 papers produced 194 pairs (guards on), and every one
  spot-checked was a genuine word-for-word repeat, e.g. the same
  economics MCQ, stem and all four options identical, saved onto one
  paper twice. Guards stayed on anyway - no reason not to, since they
  still remove real noise (323 -> 194 at 0.95) at no cost, and they
  correctly stop two DIFFERENT-but-similar items (same template,
  different numbers, deliberately placed on one paper as two separate
  questions) from being flagged as if they were a copy-paste mistake.
  `find_within_assessment_duplicates()` therefore defaults to
  `use_guards=True, threshold=0.95` - not `REVIEW_THRESHOLD`.

## Cross-passage duplicates - the comprehension-set problem

There is no `passage` field to key off. `passage` is `NULL` for every
row in the export; EdSider stores a multi-part comprehension question
as a **parent** row (the shared stimulus) plus **child** rows (each
actual question) - the same `shape` structure that already excludes
sub-questions from every other check in this codebase, because a child
means nothing on its own ("How many links does the food chain have?"
fits six different food chains).

That exclusion was correct as far as it went, but it threw out
something real along with the noise: the same sub-question, reused
under a **different** parent on a different paper, was invisible to
every check. Fixing that without reintroducing the false-positive storm
concatenation would cause (measured earlier: 0.978-0.987 for genuinely
different questions sharing one passage, once glued together) needed
the parent link to be explicit rather than assumed:

- **`parent_id`** on the child, **`children_raw`** (verbatim) on the
  parent. `store.link_children_to_parents()` derives the former from the
  latter, and is deliberately idempotent and order-independent - a child
  synced before its parent gets linked the next time this runs, and
  `save()` was changed from `INSERT OR REPLACE` to `ON CONFLICT DO
  UPDATE` naming every column explicitly, so a re-synced child no longer
  silently loses a link a previous run already made.
- **`dedupe.find_cross_passage_duplicates()`** compares linked children,
  but only ever ACROSS different parents - two children sharing a
  parent are never compared at all, not merely demoted, so the
  concatenation failure mode cannot recur even by accident.
- **`embed.py --children`** is a separate embedding pass - a
  sub-question is only safe to fingerprint once it has a `parent_id`.

**The first version of this, measured against the real bank, was
noisy in a new way** - not the MCQ-option-vocabulary noise the
assessment page had, but two different problems specific to
sub-questions:

- **Junk fragments.** A meaningful fraction of "sub-question" rows are
  not question text at all - MCQ option letters (`"c__________"`), raw
  numbers, fill-in-the-blank stubs. 2,241 of 16,967 real linked,
  English, embedded children (13%) fell under 10 real letters, and they
  dominated the highest-scoring false "matches" - score 1.0, because
  the text really is identical, but "5" matching "5" means nothing.
  Fixed with a `min_letters` filter in `find_cross_passage_duplicates()`.
- **Generic recurring prompts.** The sub-question equivalent of the
  already-documented "Fill in the blanks" parent-row redundancy - some
  prompts ("What is the title of the poem?") are asked verbatim on
  dozens of unrelated comprehension papers by design, not copied from
  one to another. Fixed with a `max_parents` filter: a text recurring
  under more than 4 distinct parents is treated as boilerplate, not a
  duplicate.
- **Threshold, same as everywhere else on the real bank.** Even after
  both filters, 0.85 produced 171,032 pairs on 14,721 real children,
  92% sitting in the same 0.85-0.90 raw-embedding-noise band every
  other real-bank sweep has shown. At 0.95: 2,055 pairs, and ten random
  spot-checks were all genuine - exact repeats, and legitimate
  rewordings ("Explain the process of credit creation by commercial
  banks." vs "Explain how commercial banks 'create money' through the
  credit creation process."). `find_cross_passage_duplicates()`
  defaults to `threshold=0.95, min_letters=10, max_parents=4`.

Not yet done: a UI page for this (the assessment-scoping page is the
template to follow), and the `min_letters`/`max_parents` filters could
move earlier - into `COMPARABLE_CHILD` itself, via a precomputed column
like `has_figure` - so a junk fragment is never embedded in the first
place rather than filtered out after. Not done yet because it would
mean a second re-embed of the already-embedded real children; worth
doing the next time this needs a full re-sync anyway.

**WAL mode** (`PRAGMA journal_mode=WAL`, set in `store.connect()`) went
in alongside this. The default SQLite journal mode blocks a reader
behind an in-progress writer, which is invisible in a one-shot CLI run
and a real problem in a process serving requests continuously while a
decision gets recorded or the poller writes newly imported questions.

**What "usable as a service" still needs**, roughly in order:

1. A decisions-table-based scorer and threshold sweep, replacing the
   removed `evaluate.py`/`tune.py` pair (which read `ground_truth.json`,
   the sample answer key - both gone now, see "Two databases"), so
   thresholds can be re-derived from real teacher verdicts once enough
   accumulate, instead of reasoned from the real bank's statistics
   alone.
2. `sync.py` has now run against the real database - `question.question`,
   schema-qualified, joined against `settings.grade`/`subject`/`language`.
   103,337 rows, 60% of them Sinhala or Tamil (see the language note
   above). Still open: whether the ~60,000 missing ids in `question_id`
   mean hard-delete or something else, whether `updated_at` can be added
   (only `created_at` exists, so an edited question cannot be detected,
   only a new one), and something has to actually run `sync.py` on a
   schedule - it is a script to invoke by hand right now, not a
   background job of its own yet.
3. Basic auth, structured logging, and a real deployment (this runs
   under `uvicorn` locally; it needs a process manager and a real host
   before anything external can reach it).
4. Re-tune thresholds on real EdSider data (depends on 1), for English
   first - Sinhala and Tamil are deliberately not embedded yet, see the
   language note above. `embed.py --language Sinhala` (or `Tamil`) is
   the entire mechanism to bring one in; nothing else changes.
5. Once English is trusted, embed and measure Sinhala and Tamil -
   `check_new.py` and `guards.py` are already Unicode-aware (see
   "Things that were learned the hard way"), but the guard *behavior*
   - the contrast list, the entity list - is still English-only text
   and has not been proven, or even looked at, for either script.
6. Mine the contrast list from the bank instead of hand-writing it.
7. Move to PostgreSQL + pgvector - deferred until concurrent *write*
   volume actually needs it; WAL mode covers the read-heavy service
   traffic for now, and `store.py` stays the only file that changes
   when this happens.
