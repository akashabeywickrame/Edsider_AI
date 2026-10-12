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

They are NOT duplicates. Five guards lower a pair's score when a warning
sign appears - numbers differ, opposing concepts, different named entities,
mismatched grade/subject, or a long passage shared verbatim while the
actual question differs. Each returns a human-readable reason.

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
app.py                  Streamlit UI (6 pages, guard on/off toggle)
service.py              FastAPI service - the check, always on
                        (model + fingerprints load once, at startup)
src/
  normalize.py          text extraction, normalisation, hashing - SHARED
                        infrastructure, not part of either feature
  store.py              ALL database access (PostgreSQL) - the only
                        module that knows what storage is underneath
  dedupe/               the matching engine (passes 1-3)
    embeddings.py       model wrapper (E5 prefix handling)
    guards.py           the five guards + contrast-term mining
    pipeline.py         whole-bank, within-paper, cross-passage sweeps
                        + thresholds  (re-exported, so `from src import
                        dedupe; dedupe.find_pairs(...)` is unchanged)
  intake/              paper intake, "Project 2" - upload a paper,
    extract.py          extract its questions, review/correct them,
    review.py           then check that batch for repeats. Local-only
                        extraction (pdfplumber / python-docx / Tesseract).
                        Depends on src/dedupe/, never the reverse.
scripts/
  import_edsider.py     load a real EdSider export (CSV or JSON)
  embed.py              compute fingerprints
  find_duplicates.py    Pass 1 only
  check_new.py          one question against the bank, all three passes
  sync.py               pull new questions from EdSider's own Postgres
  migrate_to_postgres.py  one-off: copy an old SQLite bank into PostgreSQL
tests/                  247 tests, each in a throwaway PostgreSQL schema
                        (conftest.py)
data/                   the old SQLite bank, kept only as the migration
                        source (gitignored)
```

## Running it

```
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install -r requirements.txt

.\venv\Scripts\python.exe -m pytest tests\ -v

.\venv\Scripts\python.exe scripts\import_edsider.py path\to\export.csv
.\venv\Scripts\python.exe scripts\embed.py --language English
.\venv\Scripts\streamlit.exe run app.py
```

Storage is **PostgreSQL** (see "One database technology" below).
`EDSIDER_DB` is a connection string and lives in `.env`, which
`store.py` loads itself - there is no `$env:` line to repeat in every
terminal, and the password stays out of shell history. `EDSIDER_TEST_DB`
points the test suite at a database it may create and drop schemas in.
Both are in `.env.example`.

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
0.92 are 2,222 / 5,735 / 25,670; after guards, 1,455 / 2,873 / 8,546. (Measured on 15,998 comparable rows, before Guard 5. On today's 16,084
with every guard: 5,932 pairs at 0.95 without guards, 3,045 with.)

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

## Reviewed decisions - one verdict per pair, never shown twice

Every "Confirm duplicate" / "Not a duplicate" click still inserts its own
`decisions` row - history is kept, a changed mind is a new row - but the
review pages now read `store.current_decisions()`, the newest verdict per
pair keyed by `store.pair_key()` (sorted ids, so A-B and B-A are one
pair). A pair with a verdict is hidden on "Find duplicates" and
"Duplicates within a paper" (a checkbox shows it again, labelled with its
verdict), because the same sweep returns the same pairs every visit and a
reviewer was re-reading pairs already decided. "Skip" saves nothing - a
skip is not a verdict - and hides the pair for the session only. A
verdict reruns the page, so the pair leaves the list at once, with a
confirmation naming the contrasting words captured.

The "Reviewed decisions" page lists the current verdicts with both
questions' text and the captured words, downloads them as CSV, shows the
table's real `CREATE TABLE` (`store.table_schema()`, migrated columns
included), explains how `candidate_contrast_terms()` picks words with a
live two-box demo, and has an Undo per pair (`store.undo_decision()`,
which deletes that pair's rows in either order - leaving an older one
would resurface the pair under a verdict just taken back). Empty
`([], [])` contrast terms are now stored as NULL, not `"[[], []]"`.
`decided_by` is still the literal "reviewer" - a real name needs a
sign-in (Easy Auth on Azure).

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
apart still matters, in a stricter form now: `store.DSN` reads
`EDSIDER_DB`, and **there is no default any more**:

```
EDSIDER_DB=host=... port=5432 dbname=edsider_ai user=... password='...'
.\venv\Scripts\python.exe scripts\import_edsider.py edsider_questions.csv
```

Unset, `DSN` is `None`, and `store.connect()` raises immediately
with a message naming `EDSIDER_DB`, rather than silently creating a
brand new empty database at a guessed path - which would be a worse
failure than the old one: every check would silently report "no
duplicates found" for everything, with no error and no visible sign
anything was wrong. The old sample-bank fallback was at least visibly a
demo; a silently-empty real bank is not.

The app names the open database in the sidebar. The selector over every
`data/*.db` went with SQLite - a PostgreSQL database is configured, not
discovered by looking in a folder - and a second bank (a staging copy)
is now a different `EDSIDER_DB`, or a different `EDSIDER_SCHEMA` on the
same server. `store.use_database(dsn, schema=...)` is still the only
supported way to repoint storage at runtime.

The duplicates page defaults to a threshold of **0.95** on a bank over a
thousand questions rather than the old sample-tuned 0.85, and renders the
top 50 pairs. At 0.85 the real bank returns 1.46 million pairs, which is
neither reviewable nor renderable.

## One database technology - PostgreSQL, not SQLite

EdSider runs PostgreSQL, and their team did not want one application
carrying two database technologies. This is the move `store.py` was
written to make: every table, every query and every connection changed,
and nothing outside that one module did.

- **The seam held.** All the SQL was already in `store.py`, so the
  application's other 3,000-odd lines needed nothing beyond renaming
  `DB_PATH` to `DSN` (a connection string, not a file) and asking
  `store.display_name()` what to show. `Connection`, a thin wrapper,
  keeps sqlite3's `conn.execute(sql, params)` shape over psycopg2 -
  which is cursors all the way down - and translates `?` to `%s`.
- **What SQL had to change:** identity columns for `AUTOINCREMENT`,
  `RETURNING id` for `lastrowid`, `ON CONFLICT DO UPDATE` for `INSERT OR
  REPLACE`, `string_agg` for `GROUP_CONCAT`, `HAVING COUNT(*)` where an
  alias was allowed before, real `TIMESTAMPTZ` columns, and `ALTER TABLE
  ... ADD COLUMN IF NOT EXISTS` in place of reading `PRAGMA table_info`
  first.
- **Fingerprints are pgvector where it is allowed, `bytea` where it is
  not.** Both hold the same float32 numbers and cut the bank from 600 MB
  to about 110 MB - SQLite held each vector as ~17 KB of JSON text, 519
  MB in total, which also had to be parsed back on every load. pgvector
  additionally allows the index that would end the O(n^2) sweep, so it
  is preferred; an existing `embeddings` table decides its own format,
  because changing a column type under live rows is a migration, not a
  startup guess. EdSider's Azure server offers pgvector 0.8.2 but has
  not allow-listed it (`azure.extensions`), so it is `bytea` there for
  now.
- **Setting up a connection happens outside a transaction**, and that is
  not a detail: PostgreSQL rolls back DDL and even `SET` when a statement
  fails, so the pgvector probe - which is *expected* to fail where the
  extension is not allowed - took `CREATE SCHEMA` and the `search_path`
  down with it, and the next statement reported "no schema has been
  selected". The schema itself is then applied in one transaction, one
  disk flush; statement by statement it cost a quarter of a second per
  `ALTER TABLE` on every connection.
- **`EDSIDER_SCHEMA`** picks a schema. That is how the tests get
  isolation: each one works in `t_<random>`, created and dropped around
  it (`tests/conftest.py`), on a database the suite creates if it is
  missing. The old per-test SQLite file in `tmp_path` has no equivalent -
  and tests that run against a different database than production prove
  less than they look like they do.
- **`store.py` loads `.env` itself.** A connection string is a
  credential; it belongs in a gitignored file rather than in a `$env:`
  line repeated in every terminal. An explicitly set environment
  variable still wins.
- **`scripts/migrate_to_postgres.py`** moves an existing SQLite bank
  across: columns matched by name, batched inserts, every fingerprint
  re-encoded, and the identity sequences reset afterwards - the step
  that is easy to forget and shows up later as a duplicate key error. It
  reads the old file read-only and is safe to re-run.
- **What this fixed for free:** `startup.sh` no longer copies a database
  to `/tmp`, so reviewers' decisions and uploaded papers on the server
  are no longer lost on every restart. That limitation was SQLite's.

Still open: the extension allow-list above, and a role that owns only
`edsider_ai` - the connection string currently names the server
administrator, which can read and write every database on that server.

**Then the app took minutes to load, and none of it was the queries.**
Measured from this laptop to EdSider's Azure server: opening a connection
costs 2.5 s, every statement about 0.35 s, and the fingerprints download
at about 1 MB/s. Code written against SQLite on local disk, where all
three are free, spent them freely:

| | before | after |
|---|---|---|
| first connection (schema check) | 58.8 s | 3.5 s |
| each further connection | 2.5 s | 0.00 s |
| load the bank (16,084 questions + 49 MB of fingerprints) | 95 s | 9.7 s |
| every paper's questions, within-paper page | ~15 min | 7 s |
| "also appears on assessment" notes | 0.37 s each | 3 s for all |

- **A connection pool** (`store._Pool`), not psycopg2's: that one keeps a
  returned connection only while fewer than `minconn` are idle, and opens
  `minconn` up front - 2.5 s each. Ours opens lazily, keeps up to 8,
  checks one with a round trip only after a minute idle, and sets the
  schema with a `-c search_path` connection option so a pooled connection
  can never be handed out pointing at the wrong one.
- **The schema is checked, not re-applied.** One catalogue query; running
  the DDL took 58 s on every start for a schema that had not changed.
- **Fingerprints are cached on local disk** (`data/cache/`, gitignored,
  `EDSIDER_CACHE_DIR` to move it). A fingerprint never changes for a
  given text and model, so each is downloaded once and only new ones are
  fetched after that; the database stays the only source of truth and
  deleting the cache costs one slow start. The first start after this
  took 157 s to fill it. `save_embedding()` drops a re-embedded text's
  entry; the tests set `CACHE_DIR = None` so made-up vectors never reach
  the real file.
- **One query instead of one per item:** `questions_in_assessments()` and
  `assessment_links()` replaced 1,960 and ~100 round trips.
- **The app holds the bank** with `cache_resource` for 30 minutes (it was
  `cache_data` with a 5-second lifetime - a 95-second reload every few
  clicks) and builds `VectorIndex` once, which `check_one`, `search` and
  `find_pairs` all accept instead of re-stacking 16,084 rows per call.
  "Reload from database" in the sidebar clears the bank but not the
  embedding model.

Verified equivalent, not assumed: today's code on the old SQLite file and
on PostgreSQL gives the same 16,084 rows, fingerprints identical to the
last digit, and the same 3,045 whole-bank pairs at 0.95 with identical
scores; 182 within-paper repeats on 650 papers, as before. Every page
renders against the live database: app start 12.6 s, "Find duplicates"
11.3 s first visit and 1.3 s after. Deployed in the same Azure region as
the database, round trips drop from ~350 ms to a few, and most of what
remains goes with them.

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

**WAL mode** (`PRAGMA journal_mode=WAL`) went in alongside this, back
when storage was SQLite, whose default journal blocks a reader behind an
in-progress writer - invisible in a one-shot CLI run, a real problem in a
process serving requests while a decision is recorded. PostgreSQL has
never had that problem (readers are never blocked by writers), so the
pragma went with the move and nothing replaced it.

## Guard 5 - a passage shared inside one row, not split into parent/child

Cross-passage duplicates (above) assume the passage lives in a
separate parent row. Plenty of real rows do not use that structure at
all - a `standalone` row can hold a long reading passage AND the actual
question together in one `text` field, because whoever entered it
pasted both into the same box instead of using EdSider's parent/child
split. Nothing above catches that shape, because `COMPARABLE` already
treats it as one ordinary standalone question - which is correct; the
problem is what happens next; once two such rows share the same pasted
passage, the passage dominates the embedding regardless of what is
actually asked:

    "Read the passage below ... What is the main theme?"
    "Read the passage below ... What is the author's tone?"

These are different questions, and scored **0.927 - 0.986** against
each other on the real bank - inside or above `AUTO_THRESHOLD` (0.92),
which is exactly "LIKELY DUPLICATE" for a pair that is not one. Guards
1-4 have nothing to find here: no numbers conflict, no contrast term
or entity differs, because all of that lives in the identical passage
both rows share - the guards were reading the SAME words that made the
raw score high in the first place.

`guards.shared_passage(text_a, text_b)` finds the longest block of text
the two rows share verbatim (`difflib.SequenceMatcher`), and only acts
when that block is at least 200 characters and covers most of the
shorter text - short shared phrasing ("calculate the area of a
rectangle") is normal and must not trigger this. Guard 5 then looks at
what is left on each side once that block is set aside, and demotes
the pair only if the LEFTOVER text is actually about something
different - compared as stopword-filtered word overlap
(`_residual_differs`), not a character ratio, because a character ratio
over the whole leftover is dominated by a frame both sides happen to
reuse ("What is the ... this passage?") and buries the one distinguishing
word pair (main theme / author's tone) under a shared prefix and
suffix neither side chose. If nothing meaningful is left outside the
shared block on either side, or the leftover text still overlaps
enough to be a paraphrase of the same question, the guard stays silent
- the same "absence of evidence is not evidence of difference"
reasoning every other guard already follows.

Measured against the real bank (16,084 comparable rows, 5,954 of them
200+ characters - long enough for the guard to even look): among 5,885
raw pairs scoring 0.90 or higher, Guard 5 alone demoted **27** out of
the results, every one spot-checked was the same shape - most were
MCQs sharing an identical answer-options block (A/B/C/D) while asking
about a different statement or relationship, e.g. two questions both
listing "A. Started near river valleys / B. Stared developed cities /
C. Used metals such as copper..." as options but asking different
things about them. Genuine long duplicates that just happen to be
long - the same MCQ, stem and options both identical bar a stray blank
line - were left at their raw 1.000 untouched, confirming the guard
does not cost real duplicates anything.

**Word overlap alone missed a real pair** - reported live, assessment
#1127, questions 58317/58328, flagged as 99% similar and not demoted.
Both share the same 967-character A/B/C/D passage; the residuals are
"Which pair shows the second statement being a result of the first?"
and "Which pair shows no relationship between the first and second
statements?" - genuinely different questions, but they share enough of
the surrounding sentence ("which pair shows ... the first and second
statement(s)") that word overlap alone scored 0.44, just above the
0.34 floor tuned on a hand-written example, not a real one. Rather than
nudge that floor to fit two data points sitting 0.06 apart - fragile in
either direction - the fix was a new `CONTRAST_GROUPS` entry,
`{"a result of", "no relationship between"}`, so Guard 2 catches this
directly on a literal phrase match instead of a fuzzy ratio. This is a
recurring EdSider MCQ format ("examine the relationship between the
first and second statement") across history/reasoning papers, not a
one-off, so the fix should generalise past this one pair - though only
these two exact phrases are confirmed against real data; other
relationship types this format likely also asks about ("contradicts",
"the same situation as") are not yet in the group because no real
example of them has been seen, per this project's "measured, not
assumed" rule.

**A reviewer seeing this pair still needs to know WHY it looked close**,
even after the contrast fix stops it from being wrongly flagged as a
duplicate - which is a separate problem from scoring it correctly.
`dedupe.find_shared_passage_groups(rows)` groups questions on one paper
that share a long passage with each other (via `guards.shared_passage()`,
transitively - A sharing with B and B with C groups all three even if A
and C alone were not compared), and the "Duplicates within a paper" page
shows it as an info note on any pair that has one. It changes nothing
about scoring or which pairs are flagged - purely a "here is why these
look alike" annotation, same spirit as "Also appears on assessment(s)".

The idea behind it was originally "if two CONSECUTIVE questions match
100%, that is probably a shared passage, so ignore them" - rejected as
the detection rule itself, deliberately: a real copy-pasted block of
consecutive questions would look exactly the same, and silently
excluding that is precisely the zero-tolerance mistake this project
exists to catch. `find_shared_passage_groups()` uses actual shared text
instead of position, which also turned out to be the more accurate
signal on the real pair that prompted it - questions 31 and 33 on
assessment #1127, not 31 and 32, are the ones that actually share the
passage. Run against the real paper: it correctly found all FOUR
questions in the family (31, 32, 33, 34 - ids 58317, 58320, 58328,
58335), not just the two originally reported, confirming the shared
block is what ties them together rather than any two of them happening
to sit next to each other.

**Passage grouping made the within-paper page take 3.5 minutes.** It
ran `difflib` on every pair of questions on all 650 papers - 211 seconds
of the page, repeated on every click, since nothing was cached - to
annotate 182 pairs. Three fixes, none changing a result:
`shared_passage()` first checks whether any of text_a's back-to-back
100-character chunks occurs in text_b (a shared block of 200+ must
contain one whole, wherever it starts), which rules out almost every
pair before `difflib` runs - on 8,052 real long pairs across 60 papers,
identical output to the old code; grouping runs only on the 105 papers
that have a repeat (0.85s); and `app.py` caches the page's results
(`paper_repeats`) and holds the 274 MB of paper rows with
`cache_resource`, because `cache_data` copied them on every rerun.
Measured on the real bank: first visit 15s, a click afterwards 1-3s.
Most of the remaining first-visit time is
`find_within_assessment_duplicates()` itself - parsing each paper's
vectors from JSON and running guards.

## Paper intake - upload a paper, extract its questions (Project 2)

`src/intake/` is the start of "Project 2" from the proposal: a teacher
uploads a question paper (PDF, Word, or an image of a handwritten one),
its questions are pulled out with their grade / subject / marks, a
person corrects whatever the extractor got wrong, and then that batch
is checked for repeats within itself before any of it is allowed near
the bank. It is a separate package from `src/dedupe/` on purpose - it
*uses* the matching engine (the last step) but is not part of it -
and it does not touch the `questions` table at all: extracted questions
live in `intake_documents` / `intake_questions` as drafts until someone
promotes them (that promotion step, and pushing back to EdSider, is not
built yet - the page stops at the duplicate check).

**Extraction is deliberately local-only** (the decision, made
knowingly): `pdfplumber` for a PDF text layer, `python-docx` for Word,
`pytesseract` for images and scans. No text leaves the machine, same
as the embedding model. The cost is that handwriting recognition is
weak - which is exactly why the review step is not optional. If a
format's library (or the Tesseract binary, which pip cannot install)
is missing, `extract()` does not raise: the document comes back with a
single low-confidence draft holding whatever text there was, for
manual entry, and the `extractor` field records what happened.

**The extractor's two layers:**

- `read_pdf` / `read_docx` / `ocr_image` pull raw text. Lazy imports,
  so importing the module never fails for a missing optional library.
- `segment()` and `detect_paper_meta()` are pure text rules and where
  the heuristics (and the tests) live. `segment()` splits on a
  left-margin question number in every form real extracted text
  produces - `5.`  `5)`  `5 .`  `(5)`  `05.`  `Q5`  `Q.5`
  `Question 5`  `Question 5:`  `5.What` (no space)  `5.` alone on its
  line - the first version only matched `5. ` with a trailing space and
  a real paper fell straight through to the whole-text fallback. It
  **strips the printed number** (it is `question_number`, not question
  text - leaving it in broke exact-match de-duplication and made the
  numbers guard fire on `3.` vs `7.` rather than on anything asked),
  rejects a number that repeats or goes backwards (an MCQ option `(1)`
  after question 5, a `1990.` in prose - a real number only counts up),
  rejects a number directly followed by `marks` (a `(20) marks` total
  is not question 20), and pulls marks from the last
  parenthesised/bracketed `(N marks)` in the body. No number pattern at
  all -> one low-confidence draft with the whole text, and the review
  page says so and offers "Add a question" to enter them by hand.
- `extraction_confidence` is `'low'` only when there is almost no text.
  It used to also fire on missing marks, until a real 50-question MCQ
  paper - which prints no per-question marks anywhere, as MCQ papers
  don't - came back with a low-confidence warning on all 50. Absence of
  marks says nothing about extraction quality.

**Measured on two real uploads, and this is the ceiling of the
local-only approach:**

| paper | split by number | split by "Question" |
|---|---|---|
| Grade 13 Biosystem Technology (50 MCQs) | **50** | 1 |
| Grade 13 Combined Maths (worked solutions) | 5 | **17** |

The first paper numbers questions `01.Interpretes...` with no space
after the dot; the original regex demanded a trailing space, so it
matched nothing and the whole 18,000-character paper came back as one
draft. That is fixed. The second paper is the harder lesson: it is a
worked-solutions/marking-scheme document whose SOLUTION STEPS are
numbered ("1. Initial Velocity Components", "2. Finding the Vertical
Velocity"), which is textually indistinguishable from a numbered
question - splitting it by number finds five solution steps and no
questions. **No rule can separate those two cases**, so `segment()`
takes a `style` (`SPLIT_STYLES`) and the reviewer picks it, rather than
the extractor guessing and being confidently wrong.

**`raw_text` on `intake_documents`** holds the full text the extractor
read. It exists because both papers above were stuck: they had been
uploaded under the older, broken extractor, and there was no way to
pick up the fix short of re-uploading the file. The page can now re-run
`segment()` on the stored text under a different style, show a live
count of what each style would find before committing, and display the
raw text so a reviewer can see what was actually read.
`store.replace_intake_questions()` does the swap and is destructive by
design - the previous boundaries are the ones being rejected.

**`review.check_batch(drafts, embedder)`** is the duplicate check at
the end. One uploaded paper's questions ARE "one assessment", so this
is the same problem as `find_within_assessment_duplicates()` and reuses
`dedupe.find_pairs()` with the same measured `threshold=0.95`. It takes
an injected embedder (a fake in tests, same pattern as
`test_check_new.py`), runs Pass 1 (identical normalised text) and Pass
2 + guards, and never lists a pair in `similar_pairs` that is already
an `exact_group`.

**The restructure that came with it:** `src/dedupe.py`,
`src/guards.py`, `src/embeddings.py` moved into `src/dedupe/`
(`pipeline.py`, `guards.py`, `embeddings.py`), so the two features sit
side by side rather than intermixed. `src/normalize.py` did **not**
move - it is shared text infrastructure (import, sync, store and intake
all use it), not part of the matching engine. `src/dedupe/__init__.py`
re-exports the pipeline's public names, so every existing
`from src import dedupe; dedupe.find_pairs(...)` call site is unchanged;
only direct `from src.embeddings import ...` / `from src import guards`
imports had to be repointed at `src.dedupe.*`.

**The review page (`app.py`, "Paper intake")** is per-question, not a
data grid: grade and subject are set once for the whole paper (they do
not vary question to question - only marks do), then each question is
its own bordered card with a full-height text box, a marks input, a
Reviewed tick and a Remove button, with an "Add a question" expander
for anything the extractor missed or a paper that would not split. The
duplicate check is gated on every question being ticked Reviewed -
running it on text nobody has corrected is meaningless.

**Pictures, scans and equations - where most of a real paper actually
is.** The first extractor read almost nothing from the real uploads, for
four separate reasons, each measured on real files before fixing:

- *OCR was decided per document, not per page.* It only ran if the whole
  PDF had under 20 characters. The Grade 13 model papers have a typed
  cover and then ~25 pages whose only text is the page number ("9") and
  whose questions are pictures (~50% of each page) - so OCR never ran and
  the questions were silently lost. `_pdf_page_route()` now decides per
  page: `text` (layer is the page), `page` (under 20 non-space chars, or
  an unmapped `(cid:N)` glyph - render at 300 dpi and OCR the lot), or
  `pictures` (real text plus pictures - keep the exact text layer, OCR
  each picture and slot its text in at the height it sits on the page,
  so a pasted question lands between its neighbours, not at the end).
  On the six real PDFs to hand, the typed Combined Maths papers route
  33 of 34 pages to `text` (the other has one picture); the picture/scan
  papers route every page to OCR.
- *A scanned PDF crashed.* The fallback handed the `.pdf` file itself to
  Pillow, which cannot open a PDF, and that error was not caught. Pages
  are now rendered with pdfplumber's `to_image()` (pypdfium2, already a
  pdfplumber dependency - no poppler needed).
- *Word dropped every equation, table and picture.* python-docx's
  `paragraph.text` only reads plain runs. The real Combined Maths .docx
  papers hold 106-214 OMML equations each: "80 x 1/sqrt2 = 40 sqrt2" came
  out as "80 x  = 40 sqrt2", "V = sqrt(v^2 - u^2)" as "V = ". `read_docx`
  now walks the body XML in order: `_omml()` renders equations in linear
  form (a/b, x^2, v_x, sqrt(...)), tables become one `A | B` line per
  row, and pictures are OCR'd in place (`[image]` if they hold no text,
  `[image - not read]` if OCR is unavailable). Each real paper gained
  1,300-2,000 characters of previously invisible maths.
- *The "ti" ligature read as `(cid:415)`* ("mathema(cid:415)cs"). Mapped;
  any other unmapped glyph sends that page to OCR rather than guessing.

OCR itself: the paper's language is picked at upload (`OCR_LANGS` -
eng/sin/tam, one at a time, never all three); images are EXIF-rotated,
transparent screenshots flattened onto white (converted naively, they
turn black-on-black), and small ones upscaled up to 4x. `extract()`
takes an injected `ocr` callable, so the tests prove routing and
placement with a fake, not with Tesseract. It never raises now - a
corrupt file comes back `failed (...)` - and `was_incomplete()` drives a
warning on the review page, so a half-read paper cannot pass for a
short one. Tesseract is found on PATH, via `TESSERACT_CMD`, or in the
standard Windows install folders (the UB-Mannheim installer does not
add itself to PATH, and a non-admin winget install lands in
`%LOCALAPPDATA%\Programs\Tesseract-OCR`). Language data for `sin` and
`tam` does not come with it - it was added by hand from
github.com/tesseract-ocr/tessdata into that folder's `tessdata`.

**OCR quality, measured with Tesseract 5.4 on the real uploads - what it
can and cannot do:**

- *Printed questions pasted in as pictures: works.* Grade 13 Model Pure
  paper, 27 pages, every question a picture: 24 seconds, 19 questions
  split by "Question", wording fully readable ("Using the principle of
  mathematical induction, prove that...", "A thin wire of length l is
  bent to form the perimeter of a sector..."). Before this, the same
  paper yielded page numbers and nothing else.
- *Pasted, resized screenshots: the "distortion" was not the problem -
  Word's picture seams were.* Benchmarked on the real Grade 13 Applied
  paper against 19 passages typed by hand from the paper (question stems,
  a light-grey line, an italic note, a condensed-font question), scored
  as character error rate. Whole page rendered at 300 dpi: **2.0%**
  (1.0% counting letters only); 400 dpi: no better; each picture's own
  embedded pixels upscaled 2-4x, with or without sharpening: **7.0%**,
  worse; re-scaling each picture's width to whichever version Tesseract
  was most confident in: no change (7.0%), the picked widths jumping
  around from picture to picture. The squeezed-looking Part 2 question
  read at 0.0% from a whole render. What broke text was that Word's PDF
  export stores a tall picture as two halves meeting exactly (222.1pt,
  same column), and a line on the seam, cropped per half, came out as
  noise - 60% error per half, 1.9% whole. Only the `pictures` route
  crops per picture, so `_merge_touching()` now joins touching halves
  first: that paper's full extraction went from 3.1% to **2.0%**, the
  seam line from 22.6% to 1.9%. What still fails is maths notation,
  below. The benchmark scripts were throwaway; the ground-truth
  passages are not in the repo.
- *Maths notation comes back damaged* - "∈ Z+" as "€ Z~", "x²" as "x?",
  "θ" as "@", fractions flattened onto one line. The words survive, the
  symbols do not; that is the reviewer's correction to make, and why the
  review step stays mandatory.
- *Handwriting: does not work, and no setting fixes it.* Grade 12
  Applied term paper - phone photos of handwritten answers on ruled
  paper, 720x1009 px. The default pass returned **0 characters**; 16
  combinations (autocontrast, sharpening, binarising, ruled-line
  removal x page-segmentation modes 3/4/6/11) produced at best
  "Diselacneiand fs s + --H" for "Displacement (s) = H". Tesseract is
  trained on print. An OCR'd page that comes back under 20 characters is
  now reported as "not read", so it triggers the review-page warning
  instead of passing for an empty page. Reading handwriting needs a
  different engine - a local handwriting model, a cloud OCR service,
  or a vision-capable LLM - and all but the first break the local-only
  decision above, so that is a decision to make, not a setting to tune.

**A paper in parts - Part I MCQ, Part II structured, then a marking
scheme.** The real Grade 12 GIT first-term paper (intake upload #9) is a
header, "General Information Technology I" (40 MCQs "1." with options
"(1)"-"(4)"), "General Information Technology II" (five questions
"(01)"-"(05)" with parts "(1)", "(2)", "(a)"), then a MARKING SCHEME for
both. Read as one flat list it came back as **54 "questions" for 45 real
ones**: options (2)-(4) of question 1 became questions 2-4, Part II was
split on its parts, and the answer key ("Q1: (2) Q2: (3)...") became
eight more. `extract.structure()` now returns the parts and the questions
in them (`segment()` is its questions alone, for old callers):

- *Headings, line by line* (`_heading`): "Part I", "Part 2", "Section A",
  "Paper II", sub-parts "Part II – B" (the Biosystem paper), a subject
  name plus a roman numeral ("General Information Technology II" - only
  when the words really name a subject, so "...World War II" is not one),
  "Essay questions". Rejected, each from a real line: "Part 1 of the
  experiment..." (continues in lower case), anything with "|" (the
  Combined Maths .docx header "Paper – 1 | Part 2 Questions with
  Answers"), and a bare "Answer" (that paper labels every worked
  solution "Answer").
- *One numbering format per part.* Bracket style and zero-padding are
  each run on their own; the winner counts up the most with the fewest
  restarts - options and parts restart at 1 under every question - and a
  format that numbers only one thing never wins (the Combined Maths
  .docx's "1. 2. 3." solution steps otherwise handed it to a stray
  number, 2 questions found).
- *The marking scheme is answers, never questions.* Everything from
  "MARKING SCHEME" / "Answer key" / "Answers" on: "Q1: (2)" fills the MCQ
  part's `answer`, a "Question (01)" block fills the structured part's -
  only when exactly one part of that kind exists, so it cannot guess.
- *The rest:* questions before any heading, when a later "Part 2"
  exists, are "Part 1"; a heading with only instructions under it folds
  into the next part; a part title repeated on every page does not split
  it; "Each question carries 15 marks" is each question's mark, not the
  last "[2]" inside it; kind comes from the heading or instructions
  ("pick one of the alternatives"), else from the questions - options
  and no marks, or marks - where "a) Explain..." is a part, not an option
  (Biosystem's Part II-B was labelled multiple choice until that rule).
- *Grade, subject, title.* The review page's grade and subject lists come
  from the bank, which stores grades as `G06`-`G13` - the "G-something"
  a reviewer saw - and a detected "13" or "Combined Mathematics" matched
  nothing there, so both showed blank. `detect_paper_meta()` now returns
  the bank's own codes and subject names (aliases: "GIT", "C.maths",
  "Bio System Technolgy" spelled as the bank spells it; "English Medium"
  is not a subject), plus a title ("First Term Examination"), falling
  back to the filename ("GIT_Gr12_FirstTT" -> G12, General Information
  Technology, First Term Test). The app passes the upload's real name -
  it extracts a temp copy whose name says nothing. `grade_label()` shows
  `G12` as "Grade 12".

Re-split measured on every real upload:

| paper | split by number | split by "Question" |
|---|---|---|
| GIT Grade 12 | **Part I 40 MCQ + Part II 5 structured (15 each), 45 of 45 with answers** | 2 |
| Biosystem Grade 13 | Part I 50, II-A 4, II-B 6 | 3 |
| Combined Maths .docx | 12 | **Part 1 10 + Part 2 7** |
| Combined Maths PDF (OCR) | 6 | Part 1 8 + Part 2 7 |

Stored as `intake_documents.paper_title`, `intake_documents.sections`
(JSON, read with `store.intake_sections()`) and
`intake_questions.section`. The review page shows the paper as one large
title (subject - Grade 12 - title, all editable), a large header per part
with its kind, question count, marks each and instructions, an answer box
on every question, and names questions "Part II Q01" in the duplicate
check, since both parts have a question 1. Papers uploaded earlier keep
their old flat drafts until re-split from "Split it differently", which
now also refreshes their parts, grade, subject and title. Known limits:
answers are not attached when a paper has two structured parts (the
Biosystem II-A/II-B case - which part a "Question (01)" block belongs to
is not written down), and a repeated page-top part title stays inside
the question text it interrupts.

**A question and its parts.** A structured question is a stem plus the
parts a teacher actually marks - the real GIT question (01) is "(1) A
school library uses a computerized system ... (a) Define the term
'system' [2] (b) Draw a fully labeled block diagram [5] ... (2) Consider
the following scenario ... (a) ... (b) ...". Read as one draft it was one
box with one answer, which is neither how it is marked nor how EdSider
stores it (parent row with the shared stimulus, one child row per part).
`extract._split_parts()` now splits each question recursively, to
`MAX_SUB_DEPTH` (2 levels below the question), and each part is a draft
of its own with its own marks, answer box and Reviewed tick. Drafts stay
flat, each naming its `parent_seq` (the parent's seq within the document,
not a row id, so a whole paper inserts in one pass and a re-split rebuilds
the same links) and its `depth`; the review page indents them under the
question, and the parent's own box holds the shared stem, labelled as
such. `store.delete_intake_question()` takes a question's parts with it.
The rules, each from a real paper:

- *The outer list is the one that starts first, not the longest.* In
  question (01) the inner letters outnumber the outer numbers 6 to 2, so
  "longest run wins" split at the letters and hung "(2)"'s own parts
  under "(d)". Length only breaks a tie.
- *A bare "1." at the margin is never a part.* It is the paper's own
  question numbering, and on a worked-solutions document it is the
  numbered SOLUTION STEPS - the ambiguity `SPLIT_STYLES` exists for.
  Digits count only bracketed, "(1) (2)", as the GIT paper prints them.
- *An MCQ is never split.* Its options are a left-margin list shaped
  exactly like a part list, and an option is not separately answered.
- *Two markers minimum, and a part must look like a question* - at least
  35 characters or marks of its own (the shortest real part is 48
  characters; MCQ options on the same paper run 3 to 30). A lone "(a)"
  in a sentence splits nothing.
- *"i" is both a letter and roman one*, and only the sequence settles it,
  so each reading is tried and the one that counts up wins.

Measured on the real uploads: GIT Grade 12, 45 questions and 39 parts;
GIT A/L model paper, 45 and 36; Biosystem Grade 13, 60 and 53. The
duplicate check compares the parts and skips a stem, which on its own
("Consider the following scenario:") matches everything and means
nothing.

**The marking scheme is split the same way.** It labels each part's
answer exactly as the paper labels the part - "Question (01) [15 marks]"
then "(1)(a) Definition of a System [2 marks] ..." - and a draft's own
number is the question's number plus that same suffix, so
`_split_answer_by_part()` matches them directly. Anything before the
first label stays on the question; a scheme with no part labels at all
still lands whole on the question, as before; a part the scheme says
nothing about keeps an empty box. On the real GIT paper, 29 of Part II's
44 drafts come back with their own answer. The label pattern deliberately
ignores "(2 marks - 1 for ...)", which opens a line in the same shape.

**"Add a question" can add a PART**, picking the question (or part) it
belongs to, and names it the way the paper would - the question's number
plus the next letter after its existing parts ("01(c)"). The review page
orders drafts by their position in the tree rather than by seq, so a part
added later still renders under its own question instead of at the end of
the paper.

**Fill-in-the-blank questions - English papers have no MCQ options at
all.** A real Grade 12 General English paper (intake upload #66) has no
multiple-choice questions in the usual sense: a question is a whole
paragraph with several numbered gaps running through it -
"(1) ______ an important tournament. He walked (2) ______ the field..." -
each needing its own word, not a choice from a list. Read as one draft
it was a single box for the whole paragraph; a student needs one small
box per numbered gap instead, each prefilled from the paper's own
marking scheme. `split_fill_blanks(text)` finds each gap (`_BLANK_RUN`,
matched uncapped on purpose - a quantifier cap like `{3,50}` does not
exclude a longer run, it only splits it into several shorter matches
that would each still count; the length check happens separately in
Python) and the number labelling it (`_BLANK_NUMBER`, found either
right before the gap or starting its sentence), rejecting a candidate
whose nearest text on both sides is itself just more dots or ellipses
(`_WORD_NEARBY` - the paper's own long writing-space lines mix in short
runs of literal periods that are, on length alone, indistinguishable
from a real one-word gap). `fill_blank_answers(answer)` parses the
paper's own flat numbered answer key ("1. for\n2. across...") into
`{"1": "for", ...}`. The marking scheme for this paper has no label at
all ("Answers:", "Marking Scheme:") - just a repeated "Part I" heading -
so `_looks_like_marking_scheme()` finds it by shape instead: a heading
immediately followed by a flat numbered answer line, something a real
question's own heading never has. Answer blocks pair to questions by
POSITION, not by the printed number - "Question 1" restarts inside each
major part, so matching by number would pair the wrong blocks together.

**The data-entry page shows the paragraph and one answer box - never a
box per blank.** A reviewer corrects the paper's own answer key as one
piece of text (the same generic Answer panel every other question type
already has), not by juggling N separate inputs - splitting that key
apart and back together on every keystroke would be more work than the
feature was meant to save. Only the **student view**
(`PaperPreview.jsx`, the "Student view"/"Final view" toggle) renders one
small input box per blank, matching the count the paper's own text
implies; the final view's marking-scheme line shows each blank's number
and expected answer inline instead of the raw stored text.

**A fill-blank gap and an MCQ's inline options are the same marker
shape, and one real paper has both.** The Tamil physics paper's inline
MCQ fix (a whole option list printed on one line, "(1) X (2) Y") and
this English paper's fill-blank gaps ("(1) ______ ... (2) ______...")
are textually identical by marker shape alone - both are "(N) ...".
`split_mcq_options()` now rejects a candidate option list whenever any
option's own text contains a blank run anywhere (not just right after
the marker), or is itself separated by a blank line into several
unlabelled items stacked together - three distinct real false-positive
shapes measured directly on this paper: a blank run right after the
marker, real prose first and a blank run further into the same item (a
"respond to each situation" question, one answer per item, not a choice
among them), and no blank run at all but 4-5 unlabelled words stacked
with blank lines between them (a vocabulary-in-context question). A
genuine option is written as continuous text; the blank line always
falls BETWEEN options, never inside one. Confirmed on the real bank:
zero false MCQs remain on this paper, all 6 real fill-blank questions
still match their answers, and the original Tamil paper's 25 MCQ
questions are unaffected.

**"Underline the option..." - a part's own lettered list can BE the
MCQ, not further parts.** The same paper's question 6: "Underline the
option that best expresses the meaning of each of the sentences given
below" then five sentences, each followed by four lettered paraphrases
to pick from - a. AI has no impact on business, b. AI is slowly
changing business methods, and so on. Read as parts, `_split_parts()`
split each lettered paraphrase into its own one-line "question", each
wanting a separate answer box - wrong, since a student picks ONE of the
four, exactly like any other MCQ. The existing "four or more numbered
items with no marks are an option list, not parts" guard (built for a
different paper's digit-numbered options) does not fire here - these
are lettered, not numbered, and only two or three line up at this
level, not four. What does tell them apart is the question's own
wording: `_UNDERLINE_WORD` ("underline") is checked once, from the
instruction text no recursive call still has access to, and carried
down as `mcq_hint` through every nested `_split_parts()` call. A list
under an "underline"-worded question, with no marks of its own and
nothing further nested inside it, is kept merged rather than split -
the same shape the digit guard already looks for, generalized by this
measured keyword rather than by count and numbering style alone. A
genuinely two-part "underline..." question whose parts carry marks (or
their own further nested list) is still split normally - the keyword
alone is not enough; "choose"/"select" are the same instruction in
other words but not yet added, since neither has been seen on a real
paper.

**A part one level deep can itself be a leaf MCQ or fill-blank
question - depth alone no longer decides.** Keeping question 6's
lettered choices merged only fixed half of it: each of its five
sentences (6(1) through 6(5), depth 1, no children of its own) still
needed to be independently detected as MCQ and rendered with its own
four option cards. `detect_question_type()` and `service.py`'s
`_question_out()` both assumed only a depth-0 question could ever be
mcq or fill_blank; literally any part, at any depth, was unconditionally
"sub" - safe for every paper measured before this one, where a part was
always a further open-ended sub-question, never itself a complete MCQ.
Both now key on `has_parts` alone: a draft with children is a
container holding a shared stem, never answered directly, and stays
"sub"; a LEAF draft - a whole question or one part of a structured
question, whatever its depth - gets the same real detection either way.
Measured on the real paper: all ten nested "Underline" questions (five
under question 6, five more under a poem-interpretation question of the
same shape) now come back `mcq` with four options each, where before
every one of them was a "sub" container hiding four fake child drafts.
The data-entry page's "type the options in by hand" fallback (for a
picture-only MCQ, or one the extractor missed entirely) was restricted
to depth-0 questions for the same old reason; it is now available on any
leaf question - gated on not being a stem/container, via the page's
existing `isStem` flag - so a wrongly-typed sub-part can be corrected to
MCQ and given options exactly the way a top-level question already
could.

**A Sinhala paper letters its parts with its own vowels, not a-b-c.** A
real Grade 12 Sinhala-medium Business Statistics paper structures its
questions exactly like the GIT paper's English ones - a stem, lettered
parts, roman-numeral sub-parts - except the parts are labelled with
Sinhala's own independent vowels: "අ) ... ආ) ... ඉ) ...", the same role
"(a) (b) (c)" plays on an English paper (their roman-numeral sub-parts
stay in Latin script, unchanged, on this same real paper). Read before
this fix, the whole structured section came back as flat, unsplit
blocks - `_SUB_MARKER` and `_INLINE_SUB_MARKER` only ever recognised
`[a-zA-Z]`, a roman numeral, or a digit as a part marker, so a Sinhala
vowel was never even a candidate. Both now also accept the Sinhala
independent vowel block (`අ`-`ඖ`, contiguous in Unicode, their
standard dictionary order), and `_tag_kind()`/`_expected()` gained a
`"sinhala"` kind alongside alpha/roman/digit so `_sub_starts()` can
count a vowel sequence the same way it already counts a, b, c.

One real wrinkle: the same paper's question 2 letters its third part
"ඉ)", skipping two vowels ("ඇ)" and "ඈ)" never appear) - whether that is
this paper's own inconsistency or a real, narrower convention used more
widely is not something to assume from a single paper. Rather than guess
which, the "sinhala" kind only requires the vowel sequence to move
*forward* through the table, not land on the very next one - still
enough to reject a stray vowel character that does not advance the
sequence at all, while not silently dropping a real third part the way
strict consecutive matching (what alpha/roman/digit still require) would
have.

**The same paper exposed a second, unrelated gap: marks written as bare
"(N)" with no word "marks" at all.** `_find_marks()`'s patterns all
require the word "marks"/"mks" next to the number, or square brackets -
deliberately, since a bare number alone would catch "12 cm" or "in
1948" (see `_MARKS_PATTERNS`'s own comment). This paper's marks are
entirely numeric, parenthesised, and always sit at the very end of
their line: "...(4)\n(ii) ...(4)\n" - a shape none of the existing
patterns matched, which meant `_kind_from()` found no question with a
mark, miscounted every roman-numeral sub-part as an "option" instead
(`_OPTION_LINE` matches the same markers `_split_parts` would use as
parts), and classified the whole section as `"mcq"` - which skips
`_split_parts()` entirely by design (see "An MCQ is never split"
above), so none of this had anything to do with the vowel-letter fix
above; the section never reached `_split_parts()` at all. A new pattern,
anchored to end-of-line (`$`, not just a bare `\(\s*(\d{1,3})\s*\)`),
reads this shape as marks without reopening the "12 cm" / "in 1948"
problem the original rule avoided - an in-sentence parenthetical number
essentially never sits at the exact end of its line, by contrast with
this paper's trailing mark every single time.

Measured on the real paper's second question paper (the structured one):
all five questions now split into their real parts and sub-parts with
marks correctly rolling up part-by-part to the question total (e.g.
question 1's 20 marks from two parts worth 12 and 8, each of those from
their own three and two roman sub-parts) - previously all five were
single, unsplit blocks.

**The same Sinhala paper's MCQ section was also mis-detected, for two
more reasons that had nothing to do with its vowel-lettered parts.** Its
first paper (50 MCQs, no structured section at all) exposed both:

- **No heading, no English anywhere, so `_kind_hint()` found nothing.**
  The paper's own instructions ("...correct or best-matching answer,
  choose it") are entirely in Sinhala, and `_MCQ_HINT` was English-only
  text, so the section's `kind` fell through to `_kind_from()`'s
  per-question guess - which also failed, since `_OPTION_LINE` only
  recognises lettered/Roman/bracketed-digit options, never a bare "1."
  (deliberately, for the same worked-solution-steps reason below), and
  this paper's options are exactly that: bare digits, one per line, no
  brackets. With `kind` staying `None`, `_split_parts()` ran unguarded
  on a "combination" question - "A. statement one. B. statement two. C.
  statement three." then "1. A only  2. A and B only..." - and split the
  three lettered statements into fake one-line sub-questions, exactly
  the shape `_split_parts()`'s existing lettered-statement protections
  (built for a bracketed/Roman answer key) don't cover when the key is
  bare digits instead. `_MCQ_HINT` now also matches the paper's own
  Sinhala phrase for "choose the answer".
- **`split_mcq_options()` could not find the real options at all, even
  once the section's kind was fixed.** Like `_SUB_MARKER`, every marker
  pattern it tries deliberately excludes a bare (non-bracketed) digit -
  "1." "2." - for the same reason: a worked-solutions document's own
  numbered SOLUTION STEPS must never be mistaken for real content. A
  new, last-resort marker (`_BARE_DIGIT_OPTION_MARKER`) is tried only
  after every bracketed/lettered/Roman shape has failed, and only
  trusted with **four or more** items - not the usual two-marker
  minimum - because the one real numbered-steps document measured in
  this codebase (the Combined Maths worked solutions) has just two
  steps per question, while a genuine MCQ's option count is reliably
  4-5. Tried *before* the existing "no numbered key present, so trust a
  lettered list" fallback, not after - otherwise the same "combination"
  question's lettered statements would still be mistaken for the
  options themselves, just one fallback later.

Measured on the real paper: all five sample questions (two "combination"
questions with lettered statements plus bare-digit answer keys, three
plain bare-digit MCQs) now come back `mcq` with their real five options
each, where before every one was `descriptive` with no options found -
the two combination ones having been wrongly exploded into fake
sub-questions besides. Confirmed unaffected: the worked-solutions
2-step document still finds no options and stays `descriptive`, and the
English paper's 10 MCQ / 6 fill-blank and the Tamil paper's 24 of 25
Part I MCQs (both previously measured) are unchanged.

**Still to do here:** promote reviewed drafts into the bank (and back
to EdSider); decide how handwritten papers are read, and whether maths
notation needs a maths-aware reader (see above); split picture-question
papers properly - on the Grade 13 Applied paper "Question" finds 15 of
17 (two pictures have no "Question" header) and numbers find 9 (the
numbered solution steps inside the pictures break the count-up rule),
while the paper's own typed "1." "2." in the text layer are exact and
are what the split should key on; keep
the uploaded file itself, not only `raw_text`, so an extractor fix (like
OCR arriving) can be picked up without asking for the file again;
move `intake_questions` toward the same shape as `questions` so
promotion is a straight copy.

## Running on Azure App Service - what this repo does for it

Deploying is someone else's job - an Azure DevOps pipeline zip-deploys
to a Linux Web App (`Eds-dev-ai-api`). These are the parts that had to
live in this repo, because a pipeline cannot guess them:

- **`startup.sh` is the startup command** - `bash startup.sh` for the
  API, `bash startup.sh ui` for the review app. Left to itself App
  Service picks `app.py` and expects a Flask or Django `app` in it; ours
  is Streamlit, and the API is `service.py`. It no longer copies a
  database anywhere: `EDSIDER_DB` is a PostgreSQL connection string and
  both processes read the same live database. The SQLite version copied
  `/home/data/edsider_real.db` (persistent storage, a network share, where
  WAL is not reliable) to `/tmp` and served from the copy, which meant
  every reviewer decision was lost on restart. That is gone with SQLite.
- **`requirements.txt` pins `torch==2.4.1+cpu`** from PyTorch's CPU
  index. Unpinned, `sentence-transformers` pulls the default Linux build,
  several GB of CUDA libraries on a server with no GPU - and App Service
  installs exactly what that file says.
- **`EDSIDER_API_KEY`.** When set, `/check` and `/reload` require a
  matching `X-API-Key` header (`service.require_api_key`, compared with
  `secrets.compare_digest`). Unset, the service is open as before, so
  local runs and the tests are unchanged. `/health` is never gated - an
  uptime probe cannot send headers. `/docs` still loads, but its calls
  need the key; restrict it at the host if it should not be public.
- **`app.py` uses `EDSIDER_DB`** - one configured database, named in the
  sidebar, with a clear error when it is unset or unreachable. It used to
  list `data/*.db` and report "No database found" next to a perfectly
  good `EDSIDER_DB` pointing elsewhere; a PostgreSQL database cannot be
  found by looking in a folder at all.
- **The App Service needs network access to the database server.** On
  Azure PostgreSQL that means a firewall rule for the web app's outbound
  addresses, or the two on the same virtual network.
- **`.gitattributes` keeps `*.sh` at LF.** This repo is edited on
  Windows, and a CRLF `startup.sh` fails under bash before its first line.

**Deployed and confirmed working**, as of 2026-09-18: the app is live on
Azure App Service, with `EDSIDER_DB` connecting to the real PostgreSQL
database end to end. That resolves what used to be an open question
here - the pipeline building on Python 3.11 against code only run on
3.8, `SCM_DO_BUILD_DURING_DEPLOYMENT=true` installing torch from the CPU
index, the model's first download (about 1 GB) against the startup time
limit, and the memory plan - all of it ran successfully rather than
being reasoned about in advance. Tesseract still cannot be installed on
a zip-deployed App Service, so there the review app's OCR reports
scanned pages and pictures as "not read" - typed PDFs and Word files are
unaffected; full OCR still needs a custom container.

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
3. Basic auth and structured logging. The real deployment itself is
   **done** - see "Running on Azure App Service" above - so what is left
   here is narrower than it was: no auth in front of `/check` and
   `/reload` beyond the optional `EDSIDER_API_KEY` header, and no
   structured logging yet.
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
7. PostgreSQL - **done**, see "One database technology" above. What is
   left of it: pgvector allow-listed on the Azure server (the column is
   `bytea` until then), a role owning only `edsider_ai` instead of the
   server administrator, and the ANN index that pgvector makes possible,
   which is the real answer to the O(n^2) sweep.
