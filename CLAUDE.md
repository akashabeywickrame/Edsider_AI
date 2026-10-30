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
  files.py              uploaded papers and pictures (Azure Blob Storage,
                        or a folder) - the only module touching file storage
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
local_reader/           OCR tool for data-entry computers (Surya), copied
                        to each machine on its own - not part of the
                        deployed app; see "Pasting text instead of uploading"
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
- **When it is applied, it gives up after 10 s rather than queue**
  (`SET LOCAL lock_timeout`). A waiting `ALTER TABLE` makes every new
  query on that table wait behind it: one reader stuck for 51 minutes
  on `intake_documents` (sending results to a client that had stopped
  reading) left a schema apply queued, and with it the live app's paper
  pages. The local run and the deployed app share this database, so a
  schema change from either can do that to both.
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

**EdSider's own reference lists** (step 1 of sending papers to EdSider -
see the design study "Sending Papers to EdSider"). `sync_lookups()` copies
21 lists in one `UNION ALL` query - grade, subject, language, term, year,
exam_category, difficulty, source, author, part_notation, part,
assessment_category, grading_method, privilege, attempt, time_unit,
passage_type, question_type, answer_type, assessment_status, chapter; 325
entries, every table and column read off the live schema - into one
table per list in the `common` schema (`common.grade`, `common.subject`...,
keyed by EdSider's own id; see "Intake schema" below - it was one
`edsider_lookups` table first). It runs
in `run_sync()` after the teacher list. Names are trimmed (EdSider stores
"Chemistry " with a space); an entry EdSider drops goes `active = false`,
never deleted, since a paper already sent may name it; a kind missing from
a pull is left alone; an empty pull is refused. `GET /edsider/lists`
(data entry) and `POST /edsider/lists/sync` (admin) read and refresh it.

**A paper's EdSider labels** (step 2). EdSider needs a medium, term, year,
exam category, difficulty, source and author on every paper; we captured
none of them. They are columns on `intake.document` (`medium_id`, `term_id`
... - JSON on the old `intake_documents` first), EdSider ids, chosen on the data-entry page ("For EdSider", under Paper
details) from the copied lists; `PATCH /intake/documents/{id}` refuses an
id not on EdSider's list. Medium, term and year are read from the paper
(`src/edsider/labels.py`) and offered as `suggested_edsider_labels`, saved
only when data entry saves - the same rule as the assessment settings.
Measured on the 21 stored papers before writing the rules: medium is the
script that makes up 30%+ of the letters (Sinhala papers 98-100%, Tamil
86-92%, English 100% Latin), else EdSider's medium code in the file name
("Chemistry Part I SM.pdf", a scan with no text); term from "1st term",
"FirstTT", "Term02", "THIRD", "1 වන වාරය" (no word boundary before
"term"/"1st": file names write "TechnologyTerm02", "_1st term"); year only
from the file name, title and first 300 characters, since further in it
is question content (an MCQ option "E. 2012", a date in a question).
Decimal marks were left for later at first (see "Half marks" below).

**Half marks - 2.5, 1.5.** Asked for: marks entered as decimals. A
paper's questions and their earlier versions (`intake.question.marks`,
`intake.question_revision.marks`) became `DOUBLE PRECISION` through
`store.RETYPED` - an `ALTER ... TYPE`, run only when the type differs,
since it rewrites the table under an exclusive lock even when nothing
changes. Both are small (3.8 MB); the question bank (`questions`, 692 MB,
read by every deployed app) is not, so its `marks` stays INTEGER and
holds only a whole mark - a paper's exact marks are on its link,
`question_assessments.marks` (a new column, instant to add), which is
where an archived paper's marks are read back from. The API takes any
mark 0-1000, kept to two places (`service.Marks`); stored marks come
back as 2, not 2.0 (`store._num`). The extractor reads "(2.5 marks)",
"[1.5]", "2.5 marks" - read as whole numbers only, that last one was 5,
the "5" after the point being a number followed by "marks" - and sums
part marks with `extract.mark_total()` (2.5 + 1.5 is 4, 0.1 + 0.2 is
0.3), as do the converter and the pages (`roundMarks`). Marks boxes take
`step="any"`. EdSider's `mark_allocation` was already a float. A
deployed older version reads a whole mark from the new column fine, but
fails on a half mark - deploy this before anyone enters one.

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

**"Add a question" is dropdowns only - nobody types a number.** Data entry
picks a whole question or a part; a whole question can only be a number
the paper is missing (a gap between two questions, or before the first)
or the next one at the end of a part, zero-padding kept ("07" after
"06"); a part can only be a label missing among its siblings or the next
one, in the style those siblings use - (a), (i), (1), or Sinhala අ/ආ -
defaulting to letters under a question and roman under a part
(`frontend/.../addQuestionChoices.js`). Each choice carries `after_seq`,
and `POST /intake/documents/{id}/questions` places the question there
(`store.make_room_after()` moves every later seq, and every `parent_seq`
pointing at one, down a place; `after_seq=0` is before the first), so a
missed question 7 sits between 6 and 8 in review, preview and teacher
review, not at the end of the paper. The page re-reads the list after an
add, since the questions after it have moved.

**Finding a paper among hundreds.** The paper picker was a dropdown of
every upload, and the list behind it was `SELECT *` - every stored PDF
(`file_blob`) and its whole `raw_text` came along just to show names.
`PaperFinder.jsx` filters by status (Extracted, Ready to assign, Awaiting
teacher review, Sent back for changes, Review done), subject and grade -
each choice with its count - and searches file name and title, 25 a page,
newest first, through `GET /intake/documents/search`
(`store.search_intake_documents()` / `intake_document_facets()`, list
columns only; `GET /intake/documents` uses the same light query now).
Nothing opens by itself: a new session starts with no paper open until
one is picked (the old picker opened the newest upload).

**The finder filters as EdSider's assessment library does.** Asked for:
every filter EdSider's library has - start and end date, grade, language
(here "Medium"), subject, term, author (alphabetical), chapter,
difficulty, status, exam category, source, created by, year - in its
order. Each is a paper's own label, by EdSider id
(`store.PAPER_FILTERS`; grade and subject too, now by id rather than
name), plus `uploaded_by` and the upload day as Sri Lanka counts it
(`Asia/Colombo` - 20:00 UTC is already the next day there). The menus list
EdSider's whole list with each entry's count (one facet query, counted in
Python, not one per menu), "Created by" shows names from `staff`.
EdSider's papers carry a chapter ("Chapter/ Module/ Unit", optional) and
ours had none, so a chapter filter would always have been empty:
`intake.document.chapter_id` (a migration) is now a label under "For
EdSider" and goes to EdSider's `assessment.chapter_id` on sending. Its list
is "1" to "17" - the test copy of EdSider's lists gained it.

**Marks set in the Marks section reach every question card.** "Apply to
all questions" and the per-question boxes there always saved each
question's marks, but a card's own Marks box read `q.marks` once when it
appeared, so it went on showing the old figure; it now follows the saved
value.

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

**Tables are left out of the text - data entry adds each one to its
question as a picture.** Its cells as text only cluttered the question
and fed the duplicate check rows of numbers. What counts as a table was
measured on every stored PDF and Word paper (`_data_tables()`), because
two real shapes look like tables and are not: a one-column frame around
question text (GIT p16 "Defragmenter reorganizes...") and a paper's
header laid out as a grid (Combined Maths, top of page 1: "Subject -
Combined mathematics | Grade - 13 | Term - 1st | Time") - dropping that
would lose the grade/subject/title `detect_paper_meta()` reads. So a
table is 2+ rows by 2+ columns, not within the top 30% of page 1 (Word:
not before question 1, since Word has no fixed pages), and not covering
80%+ of the page (a border; none seen - the largest real one was 41%).
A third shape surfaced only by re-extracting the real papers with and
without the rule: **questions laid out in a grid.** Political Science
puts questions 26-30 in table rows ("26. <statement> | <statement>");
the first version dropped that table and the five questions with it.
Rows whose first cell starts with a numbered item are kept as questions
(`_rows_are_questions`) - but only when the numbers do not start at 1,
because Grade 13 Biology's real data table numbers its own rows "1.
First product of CO2 fixation | C3 | C4" from 1, while a question grid
carries on the paper's numbering. A bare "1" alone in a cell never
counts (Political Science's page-6 answer-choice legend, "1 | true |
true", is a real table and is dropped). Not handled: a paper whose very
first questions sit in a grid numbered from 1 - none of the stored
papers does. Measured after the fix, every paper with tables keeps
every question: Political Science 50 questions / 89 drafts (1 table
out), Biology 53/69 (2 out), GIT Gr12 45/84 (18 out), Combined Maths
header kept with grade/subject/title unchanged; GIT A/L Model keeps its
47 questions and its Part II regains its parts (82 -> 91 drafts): a
"Paper 2" heading inside one of its tables had split Part II in two.
Real ones found: Political Science's statement grids, a "C3 vs C4
plants" table, GIT truth/number tables, grids on the Sinhala Business
Statistics paper. How each source drops them: a PDF's text layer has the
table's text redacted in memory (PyMuPDF; the stored file is never
written) after routing, since a table's text is as real as the rest when
deciding whether to OCR; a page that is OCR'd has the table painted white
in the image first (`_blank_tables`) - which also covers a scan whose
table has drawn lines; pictures inside a table are not OCR'd; Word skips
`w:tbl` (`_is_docx_data_table`); pasted HTML drops `<table>` content
(`html_to_lines`); the local reader drops Surya's `Table`-labelled blocks
except a header on page 1. The paper's note says how many were left out.
No `[table]` marker is left: an OCR'd page has nowhere to put one, and
markers on some pages but not others would mislead more than none - data
entry has the original paper beside the questions. Not caught: a table
scanned with no drawn lines at all on a page Tesseract reads (the local
reader's Surya does find those).

**Then every table became a picture of itself, on its question.** Asked
for: tables "in the exact way they are", shown with their questions and
answers when the questions load and in the preview. EdSider's editor does
have a table block (`TABLE` atomic entity, `{tableData: [[cell]]}`, drawn
by the exam page's `ViewEditor`), but a plain grid of text cells - and of
the 51 tables left out of the stored papers, 38 (both GIT papers) have
merged cells, 7 are scans, 6 are clean grids. So each table is a picture,
exact for all of them; an editable EdSider table for the clean ones is a
possible next step. How (`extract.py`, "tables as pictures"):
- *Readers take `tables=[]`* (an upload passes it; without it, as before,
  tables are left out). Each data table is collected as `{"n", "page",
  "png"}` and a line `[Table N]` goes into the text where it stood:
  - PDF, text page: cut from the page itself at 200 dpi (pdfplumber's
    renderer - pictures inside the table come too); the marker goes just
    before the first line printed below the table, the text otherwise in
    the order it was read. Picture route: at the table's height, like a
    picture's OCR. Scanned page: OCR'd in bands - above the table, the
    table's own band with the table painted white (text beside it kept),
    below - with the marker between.
  - Word: drawn, since nothing on the server prints a Word page - the
    grid from `w:tblGrid` widths, `gridSpan`/`vMerge` merged cells, cell
    shading, bold, alignment, pictures in cells, equations as their
    linear form, each cell laid out by PyMuPDF's HTML engine
    (`insert_htmlbox`). Pillow could not: built without raqm it draws
    Sinhala conjuncts as loose letters; MuPDF shapes them, and carries
    its own Noto Serif Sinhala/Tamil and Noto Sans Math, so the Linux
    App Service needs no fonts installed. Its HTML ignores `colspan`,
    hence the grid drawn by hand.
- *`structure()` places them* (`_place_tables`): a marker in a question's
  text is `{"n", "slot": "question"}` on that draft, in its answer (a
  worked solution, a marking scheme) `"answer"`; in a part's or group's
  instructions or the paper's own, the first question they introduce.
  The line leaves the text; it stays in the stored `raw_text`, so "Split
  it differently" places the same tables again.
- *Stored*: `intake.document_table` (a new table - document, n, page,
  file path or bytes) holds each paper's pictures
  (`store.save_document_tables`, files at `papers/<doc>/tables/<n>.png`);
  `add_extracted_questions` and `replace_intake_questions` attach each to
  its question as a `question_image` in that slot (`_attach_tables`). So
  the data-entry cards, the student/final preview and the teacher's review
  - which already show a question's and an answer's pictures - show them
  with no page change, and they travel into the question bank and the
  EdSider converter like any picture. Data entry can remove one like any
  picture.
- *"Read the file again"* (`POST /intake/documents/{id}/reread`, a button
  in "Split it differently"): a paper uploaded before this has no markers
  in its text, so a re-split cannot place tables; this reads its stored
  file with today's extractor (OCR language from the paper's medium) and
  replaces its questions, destructive like a re-split. Not for pasted text.
- *Two older losses found on the way.* An MCQ answer key printed as a grid
  ("Q1: (2)", 40 cells - the GIT Grade 12 scheme) is the paper's answers,
  not a table (`_is_answer_key`): left out, a fresh upload had answers for
  3 of 40 MCQs. And a scheme label matching no part ("(a)" where the paper
  prints "(1)(a)", the GIT A/L model paper's question (02)) dropped that
  worked answer and its 7 tables; it now stays on the question, under its
  label.

Measured on every stored PDF and Word file with tables: GIT A/L model 19
tables (6 on questions, 13 in answers), GIT Grade 12 17 (1 and 16),
Grade 13 2nd term 2, Political Science 1, Sinhala Business Statistics
scan 7, 2025 ICT 3 and 1 - every one placed, each paper's questions
otherwise the same as without markers, 1-7 s a paper (the scan 54 s with
its bands; 20-350 KB of pictures a paper). Not done: tables in pasted
text (the local reader still drops Surya's `Table` blocks - there is no
picture to keep), and the table's exact spot inside a long question - a
picture shows under the question's text, not between its sentences.

**Pictures the same way.** Every figure on a page with text (PDF picture
route) and every picture in a Word file is kept as itself, `[Picture N]`
in the text, numbered with the tables (`intake.document_table` holds both,
with `content_type` - a photo stays a JPEG). A pasted question's OCR'd
words stay too, to be corrected and checked for repeats; the picture shows
the maths the OCR damaged. A picture inside an MCQ option lands in that
option's own slot (`option:<label>`); one before the first question is the
paper's crest and is dropped; one under 24 px, or covering most of a page,
is not a figure; a page read whole by OCR is itself a picture, so its
"pictures" are not kept. Measured: GIT Grade 12/A/L, the Applied maths
PDFs, paper 6 (14 pictures, one in option I), the Physics paper, the 2025
ICT papers (51 pictures - 6 crests dropped) - every figure placed except
#51's essay diagram, which sits in a part the splitter already loses (its
Part C follows the marking scheme of Part B).

**Pictures that are the options.** Reported on paper 6, question 7: five
bags of fish labelled I to V, the labels printed beside the pictures, no
option text - the pictures went to the question, and no options were
found. Each picture is now matched to its label and becomes that option
("[Picture 4 option IV]"):
- *PDF* (`_pdf_option_pictures`): the label word nearest each picture's
  top-left corner, where a label sits - "I" left of the first bag, "II"
  above the second, and "V", which falls inside picture IV's box (a bag
  drawn with white space around it), far nearer V's own corner (65 points
  against 198). Within 120 points: question 42's "A" printed 146 under its
  plough is a caption, not a label. Only when every picture on the page
  gets its own label and the labels are an option sequence from the start.
- *Word* (`_docx_option_pictures`): a floating picture is kept where it is
  anchored, not where it shows - the first of the row in the file is the
  middle one on the page - so pictures are ordered by their anchoring
  paragraph, then their offset across (`wp:positionH`), and take the bare
  label lines beside them in reading order, when the counts match.
- *Placing* (`_options_from_pictures`): with no option lines of its own,
  the question's label lines (and the blank lines among them) go and
  "I." ... "V." are written in - empty options, each with its picture in
  its `option:<label>` slot, so the cards, the preview and the converter
  show an MCQ of pictures. With text options under those labels, each
  picture is its option's; any other shape (question 42: pictures A-C,
  options I-V about them) leaves the pictures on the question.
Measured on both paper 6 files: all five right (PDF: III, I, II, V, IV in
the page's own picture order; Word: 3, 1, 2 for I, II, III); every other
paper's tables and pictures unchanged.

**Pictures named by a label under them.** Paper 6's question 42 prints
three ploughs with "A", "B", "C" under them, and options I-V about them
("A-Sweep hoe is used for..."): the pictures are the question's, but each
needs its letter. `_pdf_picture_captions` reads a lone label 3-20 points
under a picture and near its middle (within 0.4 of half its width) - for
two or more pictures whose labels run in sequence - as "[Picture 6 label
A]", a marker of its own: a label *beside* a picture ("option") is only
ever used to make it an option, because that reading also finds a
question's own number next to a picture that starts it (the Grade 13
Applied PDF's "1" and "2"; first measured as false labels when the two
were one marker). At placement a captioned picture whose labels are not
the question's options stays on the question with `label`, in label
order, and the bare "A B C" line leaves the text; with no option text
they become the options, as before. Stored as `intake.question_image.
label` (and `question_files.label` in the bank copy), shown under each
picture side by side in the preview and the teacher's review
(`components/Pictures.jsx`), sent to EdSider as a line under the picture.
Data entry sets, corrects or clears one on any picture, and moves a
picture the extractor put in the wrong place - question, answer or an
option - with `PATCH /intake/images/{id}` (`store.update_question_image`;
an earlier version's picture is history and cannot move). Measured on
every stored PDF and Word file: labels on paper 6's question 42 only, no
other question changed.

**Page footers and labels on a line of their own broke MCQs.** Reported on
paper 6 again (uploads #89/#90, read with every option label alone on its
line - "I. \nIgnoring climate data" - where #49/#62 read "I. Ignoring..."):
question 14 was "sub-questions" with parts 14(I)-14(V), and so were 13
more of its 50 MCQs. Two causes, both fixed in `structure()` before
anything is split, so a re-split of the stored text picks them up:
- *Running page lines* (`strip_running_lines`): "4 Biosystem Technology
  Grade 13", read with its page, sat inside an option list that runs on
  to the next page and after the last option of every page; an option
  holding a blank line and a footer is no option. A line goes when its
  words (8+ letters) recur on 3+ pages with a different page number each
  time, going up - and "-2-", "Page 2 of 21" lines. A question's own
  repeated "(2 marks)" carries no page count and stays.
- *A label alone on its line* (`join_lone_markers`) is joined to the words
  after it - "(a)", "I.", "A)"; never a bare "1.", which is as often a
  question number. Read apart, no line looked like an option, so the part
  was not taken for multiple choice and long options were split as parts.
Measured on every stored paper: #89/#90 109 drafts and 49/50 MCQs, the
same as the other copies of paper 6 (it was 178 drafts and 36-37); the
footer lines go from paper 6's other copies and the "-2-" page numbers
from the 2025 ICT papers; no other paper changes.

**Instructions at three levels: the whole paper, each part, and a group
of questions.** A paper prints instructions above everything ("This paper
consists of 50 questions. Answer all."), above each part, and above a run
of questions ("Choose the correct answer by reading the below 20 to 25
questions"). Before: the paper's opening text was dropped outright on a
paper with no part headings (`structure()` treated it as the header), the
general-instructions box (`intake_documents.notes`) sat inside the paper
details panel with "the extractor does not capture this", a part's
instructions could only be entered when the extractor had recognised its
heading, and a group instruction had nowhere to go - splitting by number
left it glued to the end of the question before (measured: the pasted
Sinhala paper's question 35 ended with its five choices, then "36 සිට 39
දක්වා ප්‍රශ්න වලට ... භාවිතා කරන්න" and the chart's data).
- `structure()` returns `paper_instructions` (the text before the first
  heading or question, capped at 2,000 characters), saved into `notes` on
  upload/paste and on a re-split only when `notes` is still empty.
- `_split_group_instruction()` takes a line naming a question range off
  the question before it, with everything after it on that question (the
  chart, passage or legend it introduces), into `instruction_groups` (a
  JSON column: `section`, `from`, `to`, optional `title`, `text`). Only
  when the range starts at the very next question's number AND the line
  says "question" (English / Sinhala ප්‍රශ්න, ZWJ optional / Tamil
  வினா, கேள்வி) - measured against every stored paper, which also hold
  "Use about 75-100 words", "7-8 months", "30-50%", class intervals
  "10-19" and "(1) - (5)" (a question's own parts), none of which pass.
  Found: Biology 41-50, Sinhala Business Statistics 36-39, Political
  Science 21-25; no false ones on the other 16 papers; every paper's
  questions and parts identical with and without it. It runs before the
  older single-question mover (`_split_trailing_instruction`).
- The data-entry page shows the paper's instructions at the top of the
  question list, each group as a banner above its first question (edit,
  remove), and "Add instructions for a group of questions" (part, from,
  to, optional title, text) - the title is how a part whose heading was
  not recognised gets its own instructions. The list is saved whole
  through `PATCH /intake/documents/{id}` (`by_alias`, so it is stored as
  `from`, the extractor's own key). The student preview and the
  teacher's review show the paper's instructions and every group.

**Pasting text instead of uploading - for scanned papers read on a
data-entry computer.** Tesseract cannot read the real Sinhala Business
Statistics scan: it turned the "A." / "B." / "C." markers into `&,` /
`පි.` / `ට,` and dropped the "1."-"5." choice numbers outright, so no
splitter rule could recover the structure - every psm mode gave the same
damage. Surya (open-source vision OCR, Apache-2.0 code; its model
weights' licence still needs reading before EdSider relies on it) read
the same 300 dpi page essentially perfectly, every marker intact - but
took 5 m 37 s for one page on this laptop's CPU, and longer on its Intel
Iris Xe GPU (4 m 39 s just to load, 8 m 4 s to read), and App Service
offers no GPU at any tier. So the reading moves to the data-entry
person's own computer and only the text comes here.

`local_reader/` is that tool, copied to each machine as one folder:
`setup.bat` (once: a private Python 3.11, CPU torch, `surya-ocr`, and
the llama.cpp CPU runner, all pinned to what was tested), `start.bat`
(a page on 127.0.0.1:8765 - drop a PDF or picture, per-page progress
with a time-left estimate, then Copy / Save as .txt), and
`time_test.bat` (drag a paper onto it: prints seconds per page - the
"is this laptop fast enough" test). Decisions in it, each for a
measured reason: pages are read one at a time so progress is real, which
also lets `SURYA_INFERENCE_PARALLEL=2` replace the default 8 (24,576
tokens of context reserved instead of 98,304 - memory a laptop cannot
spare); one paper at a time (each page takes the whole CPU); a trailing
bare page number is dropped from each page; setup refuses a folder path
over 40 characters, because torch's install failed on Windows' 260-
character limit from inside a Temp folder. Surya's "Failed to stop
llamacpp ... WinError 87" log line is harmless on Windows: its liveness
check (`os.kill(pid, 0)`) raises that error once the process is already
gone - no orphaned `llama-server` processes were left. The tool's
`html_to_lines()` is a copy of `extract.html_to_lines()` (the folder
cannot import from the repo); `tests/test_local_reader.py` keeps them
identical.

Then in EdsiderAI: "Paste text" on Paper intake, `POST
/intake/documents/from-text`, `extract.extract_text()`. Everything after
reading is shared with an upload - `structure()`, `detect_paper_meta()`,
parts, options, answers, re-split - except the OCR-only number repair
and bolded-answer detection (pasted text carries no bold).
`html_to_lines()` turns Surya's raw HTML into one line per paragraph or
list item, so pasting it uncleaned still works, and leaves plain text
untouched. Measured on Surya's real page-1 output: questions 1-4 come
back MCQ with all five options each - including question 1's three
lettered statements kept in the stem and its five choices printed on ONE
line - where Tesseract's reading of the same page gave none. (Question 5
runs on to page 2, so page 1 alone shows its A/B/C as options; with the
whole paper pasted it has its real choices.) A two-question cut-down of
that page splits wrongly, and correctly so: the paper's "1)" numbering
only beats the choices' "1.-5." when there are enough questions for it
to count up further - tests use the real page, not a shortened one.

**Word's automatic list numbering.** A list made with Word's numbering
button has no number in its text - Word draws "I." / "(a)" / "1." from
`numbering.xml` - and 6 of the 7 real Word papers use it ("paper 6.docx"
numbers 245 of 438 paragraphs that way, every MCQ option among them).
`_DocxNumbering` replays Word's counters in document order (per list
definition, so instances sharing one continue each other; `startOverride`
restarts, which is how "restart at 1" is saved; numbering inherited from a
paragraph style counts too) and `_docx_walk` writes each label in front of
its paragraph. Measured: paper 6.docx 0 -> 48 of 50 MCQs with options (the
other two are picture-only) and 0 -> 40 of 109 answers; Grade 12 Term 3
Pure & Applied 1 -> 16 questions; General English loses 8 junk "questions"
that were its answer key; GIT Gr12 (no auto-numbering) and every typed PDF
unchanged. One knock-on, fixed: Accounting's question 04 holds an
auto-numbered "1."-"10." list, and "10." (the same padded or not) jumped a
zero-padded run 01, 03, 04 past the real "06." - a padded run no longer
jumps to a number when a padded one in between is still to come.

**A scanned Sinhala paper is read as Sinhala+English, and repaired.**
Reported on the Grade 12 Business Statistics paper uploaded as #94 - a
15-page scan with no text layer, two papers in one file (50 MCQs; then
Part I and Part II of structured questions). Read as Sinhala alone it gave
17 "questions" and no Paper 2: its Latin-script markers came back as
Sinhala look-alikes ("A." "B." "C." as "&," "පි." "ට,"; ")" as a digit, so
"13)" was "138"), and the default layout read the indented "1."-"5."
option lists as a separate column and dropped their numbers. Now
(`extract.ocr_sinhala_page`, used for `lang == "sin"` only - Tamil prints
the same markers but has not been measured):
- *Read twice*, Sinhala+English in the default and the one-block layout
  (psm 6); the reading with more lines starting a question, option or part
  (`_marker_lines`) is kept - on #94, 207 against 147. Twice the OCR time
  for a Sinhala scan (about 120 s for #94's 15 pages).
- *Repaired* (`repair_sinhala_ocr`), each fix only where the lines around
  it say what it must be, measured line by line against the printed paper:
  "C." read "ට," on the line after "B."; "1," for "1."; one look-alike
  between "2." and "4." ("රි." -> "3."); roman runs "(i) (1) (iii)" and
  "(1) (1) (ili) (iv)" -> i ii iii iv, never a "(1) (2) (3)" option list,
  and a "(5)" marks line does not break a run; a lone "2)" after "අ)" "ආ)"
  inside question 02 is "ඉ)"; combination answer lines ("1. A පමණි 2. A
  හා B පමණි ...") get their letters back ("&" / "ගු" / "ඨී" ... -> A, "පි"
  -> B, "ට" -> C, "3. හා B" -> "A හා B" since nothing else can stand before
  "and B"); options on one line have their numbers walked in order ("4,"
  -> "4.", "ර3." -> "3.") - only the number expected next, so an option
  "7, 21" keeps its comma.
- *Headings in Sinhala*: "N පත්‍රය ... කාලය" (paper N, with its time) and
  "I/II කොටස" (part; OCR "| කොටස", "1! කොටස", "11 කොටස") - `_sinhala_heading`.
  The MCQ hint accepts the OCR's "තොරන්න" for "තෝරන්න". A part's marks
  "(3)" alone on a line are no longer glued onto the heading after them
  (`join_lone_markers`) - that hid Part II on the pasted copies too.
- *Grade and subject*: "12 ශ්‍රේණිය" (file names write "12 ශ්_රේණිය") is
  G12; "ව්‍යාපාර සංඛ්‍යානය" is Business Statistics.
- *An ellipsis is not a blank*: dots glued to text on both sides ("6-A,
  6-B.........,6-F", option 4 of question 11; Political Science's
  "(..........)" answer box) no longer stop an option list (`_has_blank`);
  every real gap measured has a space or a quote beside it.
Measured: #94 read again end to end - Paper 1 50 MCQs, 49 with all five
options (question 29's answer line, read in bands around the page's table,
stays garbled), Paper 2 Part I 01-04 and Part II 05-08 with every lettered
and roman part as printed, grade G12, subject Business Statistics. Every
stored paper re-split with and without the change: only the Business
Statistics copies (scans and pastes, now with Paper 2's parts), Political
Science (#21/#57-#59 gain question 3's options; the OCR'd #60/#61 gain their
real Part II from question 31) and the badly-read #83 change.
`tests/data/business_statistics_g12_ocr.txt` is the paper as read, for
`tests/test_sinhala_ocr.py`. Still OCR, so still checked by a person: the
local Tesseract is what reads it - the deployed App Service has none, and
there a scan still needs the local reader and "Paste text".

**Chemistry - formulas, equations and drawn structures.** The real
scanned 2025 A/L Chemistry paper (Sinhala medium) came back from the
deployed site as one empty draft: every page is a picture and the server
has no OCR (intake upload #78 - 0 characters; #77, with Tesseract locally,
20 of 50 questions). Through the local reader, Surya's *reading* was right
- `N<sub>2</sub>(g)`, `dm<sup>3</sup>`, `\Delta H^\circ = -90 \text{ kJ
mol}^{-1}`, `N_2(g) + 3H_2(g) \rightleftharpoons 2NH_3(g)` - and the damage
was all ours: tags stripped ("dm3", "6.022 x 1023", "mol-1") and LaTeX left
raw. `src/intake/ocr_html.py` (with an identical copy in `local_reader/`,
checked by `tests/test_local_reader.py`) now turns `<sub>`/`<sup>` into the
Unicode characters the question editor itself saves (N₂, dm³, Fe³⁺, Kₛₚ;
Unicode has no subscript capitals, so K_C stays "KC" and a longer one is
bracketed, "ΔH°(CCl₄(g))"), and `<math>` into one readable line ("N₂(g) +
3H₂(g) ⇌ 2NH₃(g)", "ΔH° = -90 kJ mol⁻¹", fractions as "a/(b c)"). A
**drawn structure is left out**: Surya lays a benzene-ring drawing out as
rows joined by bond lines ("CH₃ | C₆H₅ | CHCH₂CH₂OH | OH"), which as text
is a different molecule (the ring's substitution pattern and a C=O drawn
under its carbon are lost) - data entry adds it as a picture, like a
table. Also from this paper: Sinhala marks, "(ලකුණු 60 යි)" (and the OCR's
"(ලකුණ 60 යි)") and "ලකුණු 150 බැගින්" for every question - worded marks
may now exceed 100, bare numbers still may not; a marks line on its own
after a part's last sub-part is that part's total; the MCQ hint accepts
"පිළිතුර තෝරා" as well as "තෝරන්න"; the local reader drops Surya's
PageHeader/PageFooter blocks ("- 4 -", "[see the next page]"), which had
landed inside questions. The review page now says plainly when pages could
not be read and sends data entry to the local reader and "Paste text".

**Worked-solutions papers - "Solution" and what follows it is the
answer.** The Grade 13 Combined Maths "Full Solutions Guide" (intake
uploads #84 Word, #85/#86 PDF) prints each question, then "Solution" and
the working, numbered "1. Base Case", "2. Assumption", "3. Inductive
Step". Left in the text, those steps became questions 1-3 (the real
questions 1 and 2 were lost) and options: 2-12 "MCQs" per copy on a
paper with none. `extract.structure()` now takes every solution out
before splitting (`_take_out_solutions`, a private-use marker left on
the line before it) and puts it back as the answer of the question or
part it follows (`_put_back_solutions`). The rules, each from this paper
or the older Combined Maths Applied .docx/PDFs (#5-#8, #12, #26, #27,
#33, #35, #67):
- *What a solution line is:* "Solution", "Solution:", "Solution
  Sketch", "Solution: <text>"; and "Answer" alone on its line (the
  Applied .docx). Not "Solutions": the PDF's header grid breaks "Full
  Solutions Guide" over three lines. Nothing before the first question
  is a solution. A second "Solution" inside one stays in it (question
  06(b) repeats its problem and "Solution Sketch:" in its own working).
- *Where it ends:* the next question ("Question 3", "2. Question",
  "Question: Vectors"), a part heading, or the next part in sequence at
  its level - "(b)" after the question printed "(a)", "ii)" after "i)".
  Never a numbered line (the working's own steps), and not a part out of
  sequence: the PDF breaks "(1 + k)(-1/a) = -a" so a line starts "a) =
  -a", and an answer labels its own working "(i)", "(ii)" again under
  parts (i) and (ii). Letters and roman numerals are separate levels.
- *Headings that say two things:* "Part B - Question 01" is a part
  heading and a question; the first "Question 02 (a)" starts question 2
  and its part (a), a later "Question 02 (b)" or "Question (b)" is only
  part (b). Numbers start again after each part heading. "Problem:" is a
  label and is dropped.
- *Padding:* the paper prints "Question 01", "Question 02", then
  "Question 3" to "10". Zero-padded and plain numbers are run as two
  numberings (an Accounting paper's "1."-"10." list needs that), which
  lost 01 and 02 - not for a line saying "Question", which is a question
  however it is padded.
- *No MCQs from working:* a leaf whose only "options" are bare numbered
  lines (question 05(c) prints four steps with no "Solution" line) is
  stored as `question_type` 'descriptive'; a bracketed "(1)"-"(4)" list
  is still an MCQ. Extraction now stores `question_type` (the column was
  never written), and `service._question_out` reads options out only for
  an MCQ (or no type) and blanks only for a fill-in-the-blank - a type
  someone chose is the type (one stored question changed by this: paper
  12's question 2, marked descriptive, no longer shows blank boxes).
- *The PDF's header grid is read first.* Word writes it after the page's
  text, so it was read at the foot of page 1 - inside question 3. A kept
  page-1 header table is now moved to the front of the page's text
  (`_data_tables(..., header=True)`), where the grade/subject/title rules
  look. Of the stored PDFs only #33, #85 and #86 have one on a text page.

Measured on every stored paper, both split styles: only the Combined
Maths papers change. Pure maths (#84-#86): 32 questions and parts, 23-24
with their answers, the one leftover "MCQ" (05(c)) stored as
descriptive; before, 12-25 drafts and no answers. Applied, by "Question":
17-18 drafts, 14-17 with answers, no MCQs (before 19, none answered, up
to 2 MCQs). Not handled: a scan whose OCR lost a question's heading
(#7's question 5) - that question runs into the solution before it and
lands in that answer. A paper's stored text keeps whatever order it was
read in, so #84-#86 were read again from their files ("Read the file
again", below): 32 questions and parts each, 24 with their answers.

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

## Intake schema - normalised, and papers assigned to Data Entry Operators

Everything about an uploaded paper now lives in its own schema, `intake`,
and EdSider's reference lists in `common` - asked for in one go: every
intake table in the intake schema, normalised, grades and subjects from
common tables with nothing repeated, and papers given to Data Entry
Operators (EdSider's `DEOP` role) by an administrator, so only that person
can work on one. EdSider has the same idea in
`assessment.assessment_operation` (paper, employee, operation, completed,
assigned by, plus a log), which is what `intake.review` mirrors.

- **`common.<list>`**, one table per EdSider list (grade, subject,
  language, term, year, exam_category, ... - `store.COMMON_LISTS`), keyed by
  EdSider's own id, with `name`, `short_code`, the list's own extra columns
  (an author's `type_id`, ...), `active`, `synced_at`. The sync fills them
  in one statement. Nothing else stores a grade or subject name.
- **`intake.document`** - the paper: title, `grade_id`/`subject_id` and
  the EdSider labels (`medium_id`, `term_id`, `year_id`, `exam_category_id`,
  `difficulty_id`, `source_id`, `author_id`), each a foreign key into
  `common`; `status` a key into **`intake.status`** (extracted, in_review,
  checked, teacher_review, refer_back, review_done, published).
  **`intake.document_file`** holds the file, the read text and the
  extractor, apart, so listing papers never touches them;
  **`intake.document_settings`** the assessment form, one column per
  setting, plus the marks plan's paper-wide figures.
- **`intake.part`** (a paper's parts, with the marks plan per part),
  **`intake.instruction_group`**, **`intake.question`** (its part by
  `part_id`; no grade, subject or depth - those are the paper's, and depth
  follows from `parent_seq`), **`intake.question_comment`**,
  **`intake.question_image`** - all JSON on `intake_documents` before, or
  repeated on every question.
- **`intake.review`** - who has a paper: `stage` 'data_entry' (a Data Entry
  Operator) or 'teacher', `assigned_to`, `assigned_by`, `finished_at`,
  `active` (one active row per stage; reassigning closes the old one, so
  who had it before is kept). **`intake.review_log`** - every upload,
  assignment, hand-on, send-back and status change, with who did it.

`store.py` assembles rows in the shape callers always read
(`paper_grade` as EdSider's code "G12", `paper_subject` as its name,
`sections`, `instruction_groups`, `assessment_settings`, `depth`), so
`service.py`, `app.py` and the pages changed little. Queries name the
schemas `intake.` and `common.`; on a test's own schema `store._qualify()`
swaps in `t_<random>_intake` / `t_<random>_common`
(`store.schema_names()`), and `conftest.py` seeds them with a copy of
EdSider's real lists (`tests/edsider_lists.json`) and drops all three.

**The workflow now.** An administrator uploads (upload and paste are
`ADMIN` routes) and gives each paper to a Data Entry Operator on Admin ->
Data entry (`AssignOperators.jsx`, `POST /intake/documents/{id}/operator`,
`GET /operators` - everyone whose role is `employee`, labelled "Data
Entry Operator" in Manage users). `service.paper_access`, on every route
with a paper, question or picture in its path (a test fails if one is
missing), lets an operator open only their papers and not change one
while a teacher has it (409), and a teacher open only theirs and change
it only while their review is open; admins and a local run reach
everything. The paper finder lists an operator's own papers only. The
operator's duplicate check finishes their stage ('checked'); the admin
assigns a teacher; "Refer back" reopens the operator's stage; "Review
done" copies the paper into our own question bank (`promote_document`,
grade code, subject and medium names as the bank already holds them) and
marks it 'published'. `GET /intake/documents/{id}/history` shows the log.

**Moving the existing papers - not yet done, deliberately.** The live
database is shared by a local run and the deployed dev and pre-prod apps,
and the deployed version reads and writes `intake_documents` /
`intake_questions` until it is replaced. So the new code creates the new
schemas (empty) and never touches the old tables;
`scripts/migrate_intake_schema.py` (`store.migrate_old_intake_tables()`)
copies them across, keeping ids, at the moment the new version is
deployed - without `--apply` it rolls back and only reports. A paper
already copied is skipped, so it can run again after a late upload. On
the live data (83 papers) the only value not on EdSider's lists is one
paper's "Combined Mathematics" (EdSider: "Combined Maths"), left unset
and reported. A paper goes to its uploader for data entry when the
uploader is a Data Entry Operator - before, whoever uploaded a paper
worked on it. After that, drop the old tables by hand:
`DROP TABLE intake_question_images, intake_question_comments,
intake_questions, intake_documents, edsider_lookups;`.
`scripts/migrate_to_postgres.py` (the old SQLite move) still names the
old tables and is not meant to run again.

Not done yet: EdSider's own DEOP-role staff are not synced the way its
teachers are - Data Entry Operators are added on Manage users. The
question bank (`questions`) still stores grade and subject as text; it
mirrors EdSider's question rows and was left as it is.


## Sending a paper to EdSider - the converter and its preview

Steps 3 and 4 of the plan ("Sending Papers to EdSider"): what a finished
paper becomes in EdSider's own tables, and a page that shows it before
anything is sent. Nothing writes to EdSider yet - that waits on EdSider's
choice of route (an import endpoint, their API, or direct inserts).

**EdSider's `assessment` schema, read end to end** (37 tables, 13 of them
dated backups `*_bk2025...`; paper #2141, Grade 12 GIT, read row by row).
A paper is, in write order: `assessment.assessment` (1 row),
`assessment_schedule` (1), `assessment_assessor` (the chief assessor,
`CASS`), `assessment_part` (per part, with `part_instruction`),
`assessment_part_section` (per section - "answer N of M" lives here:
`allow_optional_questions`, `no_of_questions_to_answer`),
`question.passage` (a run of questions, titled "ASMNT<id> Q28 - Q31"),
`question.question` (a question's parts before the question),
`assessment_question` (one link per question row: `question_number` per
section, `mark_allocation`), `assessment_operation` (+ its log) and
`assessment_review` (`assessment_review_new` until EdSider renamed it -
see "Publishing in EdSider" below). Measured rules:
- People are named two ways. The paper's `created_by` /
  `assessment_admin`, the schedule's `scheduled_by`, the assessor row and
  every link's `added_by` use `staff_user.user_id` as text ("314",
  "CASS314"); `assessment_operation.employee_id` / `assign_by`,
  `assessment_review.teacher_id` / `assign_by` and
  `question.passage.created_by` use `registration.teacher.user_id`
  (= `staff_user.staff_id`) - EdSider's own models join them that way.
- `assessment_state` (an enum) and `status_id` are set together and
  match. An "operation" is a status id: all 950 are 9, ProofReading;
  EdSider assigns one only to a Ready or ReviewCompleted paper.
- Marks are on the link: a parent's is 0, each part carries its own, all
  sharing the question's number (#2141's question 5: 11 + 4, parent 0).
- `start_time` cannot be empty; a title is at most 128 characters and
  unique; EdSider keeps default terms per medium in
  `assessment_term_condition`; recent papers use all three grading methods.
- 309 old papers have no parts and 9,285 links no section; every paper
  made in the current screens has both, so a sent paper always does.

**`src/edsider/export.py`** - `build_package(doc, questions, lists,
people, ...)` returns every row in EdSider's own columns, rows naming each
other by local keys ("part1", "sec2_1", "q512") for the writer to replace
with EdSider's ids, plus problems (`error` blocks sending, `warning` is for
a person to check). Pure, so the preview and the send are the same rows.
Text becomes compact Draft.js JSON as EdSider's editor saves it (a block
per line, pictures as atomic IMAGE blocks with data: URLs); an MCQ's
correct labels become weights ("0,100,0,0", an uneven split's remainder
to the first); a part's own parts are sent as the question's parts in its
place, each with its own answer box and marks, the part's text and pictures
leading the first of them and each keeping its printed label (`(a)`) since
EdSider numbers parts by position (`_answerable_parts`) - EdSider has one
level of parts, and as lines inside one part five questions shared one
answer box, seen in EdSider's Student View (a part whose stored mark
disagrees with its parts' is reported);
"Part II - A" / "- B" become two sections of one part; the paper's general
instructions go on the first part. Marks per question and "answer N of M"
come from the saved marks plan, else what the paper says
(`extract.part_marks()` - "Each question carries 15 marks", "Answer FOUR
questions only", and one mark each for an MCQ part with none printed,
reported). The question code is EdSider's: G12-GIT-EM-T2-EX02-P2-Q05.
Measured on our stored GIT Grade 12 paper (#34): Part I 40 MCQs at 1 mark,
Part II 5 questions "answer any 4" at 15, 115 marks, question 1's parts at
11 and 4 under a 0-mark parent - the same shape as EdSider's #2141; the
only errors left were the labels data entry had not set.

**Grading method** is now a required EdSider label beside exam category
(`intake.document.grading_method_id`, a migration on the intake schema -
MIGRATIONS entries may now name `intake.<table>`).

**No preview page.** A "Preview for EdSider" panel on Paper intake showed
these rows, codes and links; it was taken off on request - how a paper is
transferred is internal, not something data entry or admins need to see.
The route stays (`GET /intake/documents/{id}/edsider-preview`, ADMIN +
paper rule) for checking one paper's rows by hand, and the converter for
the send itself; the paper is recorded as created by whoever sends it
(`as_email` names someone else on a local run only).

**EdSider's Data Entry Operators come from EdSider.** `sync_teachers()`
now reads everyone holding a current TEACH or DEOP role (162 people: 133
teachers, 30 operators, one both) with every position they hold
(`staff_position`, names in `common.staff_role`) and both EdSider ids
(`staff.edsider_user_id`, `staff.edsider_staff_id`) - the ids the rows
above need. An EdSider operator signs in as data entry
(`staff.edsider_operator`, after teacher in `STAFF_ROLE_SQL`) and fills
Admin -> Data entry's dropdown with their positions ("Assessor, Data Entry
Operator"); "Refresh from EdSider" there re-reads the list. Several of the
30 are EdSider's own test accounts, active there, so they show here too.

**EdSider's Main Assessment Administrators are this app's admins.** MASAD
("Main person who is handling all Assessments settings and operations",
20 people) is synced with TEACH and DEOP (`staff.edsider_admin`, admin in
`STAFF_ROLE_SQL` ahead of teacher and operator; a role a superadmin set
still wins). They assign papers and send them to EdSider; losing MASAD in
EdSider makes them whatever else they are.

**Two teachers per paper.** Asked for: each paper reviewed by two
teachers, ready to publish once both finish. `intake.review` holds an
active row per teacher (`idx_intake_review_active` - one active row a stage
- became `idx_intake_review_operator`, one operator a paper, and
`idx_intake_review_teacher`, each teacher once; `_schema_is_current` checks
the index, so the change applies once). `store.assign_teachers()`;
`POST /intake/documents/{id}/assign` takes `teacher_identities`, exactly
two, different; the papers list carries `teachers` and `teachers_done`.
Each teacher ticks questions for themselves (`intake.question_check`) - the
`teacher_reviewed` column was one tick for everyone, so the second teacher
would have found every question already ticked; a teacher's GET/PATCH of
questions reads and writes their own ticks. Review Done closes that
teacher's part only (`finish_stage(..., who=)`); the second one's syncs
the bank copy and makes the paper ready to publish. A teacher who has
finished cannot change it (`paper_access`). Refer Back from either sends
it to data entry; when data entry finishes again the paper goes straight
back to both teachers, both reopened - both sign off the final version.

**The two teachers have different jobs.** Asked for: the paper's own
teacher answers the repeats, the overall checking teacher checks it
against the syllabus and comments. The first teacher assigned is the
**owner**, the second the **syllabus checker** (`intake.review.duty`, a
migration; Admin -> Paper assignment labels the two dropdowns so). Only
the owner may replace or keep a flagged repeat (`_require_owner`, 403 for
the checker); only the owner's Review Done waits for open repeats - the
checker's does not. Both tick, comment, refer back and sign off. Lists
put the owner first (`store._TEACHER_ORDER`), and the paper carries
`teacher_owner`; a paper assigned before duties were kept counts its first
teacher as owner. Reassigning in the other order swaps them.

**Then one teacher became enough.** Asked for: a paper is normally
reviewed by its own author; only when the author does not want to review
it does it go to a different teacher - never two. `AssignIn` takes exactly
one teacher (`POST .../assign`, 422 for two), the paper is ready to
publish the moment that teacher's Review Done lands (the existing "every
active teacher finished" rule, with one teacher), and the MASAD's "Send
to EdSider" shows there as before. Assigning a different teacher takes the
paper off the first - the Paper assignment page has "Change teacher" on a
paper still in review, for an author who turns it down after being given
it. The page picks the paper's author for it (the "For EdSider" author,
matched to a teacher by name - EdSider keeps the two lists apart, and only
18 of its 189 author names match a teacher's exactly), says so, and says
when the author is not on the teacher list. The teacher list is
alphabetical by the name shown, on the page and in
`assignable_teachers()` (trimmed: EdSider's names carry leading spaces).
Owner and syllabus checker stay in the code for a paper already given two
(none was in review when this changed); a paper has one teacher, its
owner, from now on.

**A teacher writes any kind of question, and corrects small mistakes.**
Asked for: the question a teacher writes in place of a repeat is an MCQ,
a written-answer question or one with parts, with the inputs each needs;
and teachers fix a spelling, not only comment on it.
- *Replace* (`POST .../replace`, `ReplaceQuestionIn.kind`): "mcq" takes
  `options` and `correct` (by place) and is stored as the paper stores
  one - the stem, then "(1) ..." lines, the answer "(2)" or "1, 3"; "sub"
  takes `parts` ({text, answer, marks}), written straight under it as
  (a), (b)... (`store._add_parts()`, the later questions moved down with
  `make_room_after`), its marks theirs added up; a question that already
  has parts cannot take more. `question_type` is stored as written.
- *Correct* (`POST /intake/questions/{id}/correct`, `TEACHER` + the paper
  rule - only the paper's teacher, only while their review is open): the
  stem, an MCQ's options (markers kept) and the answer, edited in place on
  the teacher's card ("Edit"). `store.correct_question()` keeps the
  wording before as a revision ("Corrected by the teacher") and logs "Qn
  corrected by the teacher"; a save that changes nothing keeps nothing.

**An administrator follows a paper as its teacher sees it.** Asked for:
MASAD, admins and superadmins see a paper's progress and what its teacher
changed, the way EdSider does it. Read in EdSider's code: there is no
"log in as" and no monitoring screen - MASAD opens the teacher's own Paper
Review page from a View icon on its "All Papers" list, and the page's role
flags decide what each may do; EdSider writes change logs
(`question_log`, `assessment_review_logs`) but never shows them. Here:
**View** on each paper in Paper assignment's table opens
`/app/admin/papers/:id` - `TeacherReview` with `adminView`, the same page,
read only (no ticks, comments, edits or finish), with the teacher, review
done or not, how many questions they have ticked (`list_questions` gives a
non-teacher the paper's teacher's own ticks), every comment, each
question's "Earlier versions", and "Changes and history" - the paper's
whole log, newest first. Works on a paper already sent to EdSider too (it
reads back from the bank).

**Paper assignment's table: filters, pages, Unassign and Delete.** Asked
for, with a thousand papers in mind: "Already assigned" is now searched a
page at a time (25, First/Previous/Next/Last) with the paper finder's
filters plus Teacher - the filter bar and the search moved into
`components/PaperFilters.jsx` (`usePaperSearch`), which both pages use.
`GET /intake/documents/search` takes `statuses` (comma-separated; its menus
then count only those papers) and `teacher`; the facets count papers per
teacher, and `uploaded_by_names` names teachers too. "Ready to assign"
asks for `data_entry_done` papers only rather than every paper. Two
buttons, both confirmed first: **Unassign** (`POST .../unassign`,
`store.unassign_teachers()`, admin only) closes the teacher's review - kept
in the history - and puts the paper back at "Ready to assign", questions
and bank copy as they are, from teacher review, review done or ready to
publish; **Delete** is the same `DELETE /intake/documents/{id}` as data
entry's Remove, which for an administrator (`moved_on`) now takes a paper
at any stage, with its question-bank copy (`questions` "intake-<id>-*",
their `question_assessments` and `question_files`, and the stored files).
Never a paper sent to EdSider (`last_send`, or 'published') - it is in
EdSider, and this is our record of it - nor an archived one (the paper rule
refuses every change to that).

**"At least 2 from each section, 5 in all" - a part's sections and its
own count.** Asked for: a part whose questions are in sections, each with
its own number to answer, plus some to answer from either. EdSider already
models it (read in its `optionalQuestionScoring.js`): a section's
`no_of_questions_to_answer` counts its best N, then a part's
`total_questions_to_answer` (with `allow_questions_to_answer`) adds the
best of what is left in any of its sections. We sent the first and never
the second. Now:
- *The count in all* is `intake.part.plan_group_answer_count` (a
  migration), kept on a part's first section, `group_answer_count` in the
  marks plan; the converter puts it on EdSider's `assessment_part` - only
  when set, since a database copied before EdSider added those columns
  lacks them - and warns when it is below what the sections ask for or
  above what they hold. A section may ask for none ("None required", sent
  as optional with 0) when the part's count picks from it.
- *Dividing a part* (`POST .../sections/split`, `store.split_part()`): for
  a paper whose section headings were not read, data entry picks the
  question each later section starts at; the part becomes "Part II - A",
  "- B"..., the converter's own section naming, each question taking its
  parts with it. "Join the sections" (`.../sections/join`) undoes it. A
  paper in one piece divides as "Part I".
- *The totals* on the Marks section (`groupTotal()`) follow EdSider's rule
  exactly, so the paper's total is what EdSider will give it.
- *Found on the way, and fixed:* EdSider counts a question in a picking
  section as compulsory unless its link says `is_optional` - and we sent
  every link `is_optional = false`, so "answer 4 of 5" would have been
  marked out of all five there. Every question in a section the student
  picks from is now sent optional, as EdSider's own papers are (8,289
  links in the pre-prod copy).
- *A particular question compulsory* ("Question 1 must be answered, then
  any 3 of the rest"): `intake.question.compulsory` (a migration), ticked
  under the question's marks box on the Marks section wherever the student
  picks. Sent as EdSider does it: that question's link (and its parts')
  not optional, the section's `no_of_required_questions` the number of
  them, "questions to answer" still the whole count (4: Q1 and any 3). More
  compulsory questions than there are to answer is an error. The totals
  count them first (`groupTotal()`'s `required`). A re-split, which builds
  the questions again, clears the ticks; so does archiving, whose bank copy
  keeps no such column - neither matters once the paper is sent.

**EdSider showed a question broken where the paper's margin broke it.**
Reported with a Biosystem MCQ: the extractor keeps every printed line, the
preview joins a wrapped sentence back together (`joinSoftBreaks`,
`frontend/src/lib/text.js`), but the converter made a Draft.js block of
every line - so EdSider showed the paper's line ends, half-empty lines
across its page. `export.draft_doc()` now joins the same way first
(`join_soft_breaks()`): a blank line, a list item and a table row
("A | B") still break. Both rules gained "a." (a lettered statement,
"a. Platty, Swordtail are...") and Sinhala "අ)" as list starts, so each
statement stays its own line; a test keeps the two patterns identical.
Papers already in EdSider keep their broken lines - only papers sent from
now on are joined.

**A paper referred back waits at the top of data entry's page** ("N
papers sent back by a teacher", a button per paper) - nothing
pointed anyone at it before.

**Data entry sees each picture where it belongs.** The card showed every
picture inside a closed "Images (N)" panel, apart from answer pictures,
so a question's table was nowhere near the question. Now (`SlotPictures`
in `PaperIntakeQuestion.jsx`) a question's pictures sit straight under its
text, an option's inside that option, the answer's under the answer -
each with its label, where it belongs and Remove. The panel became "Add a
picture", plus any picture naming an option the card does not show.

**Numbers as the paper prints them.** Asked for: "5", then "a", then
"i" - not "05(a)(i)". `frontend/src/lib/numbers.js` `shownNumber()` shows
a whole question by its number alone ("05" and "(01)" lose the padding and
brackets) and a part by its own last label; each card is indented 30px a
level (`marginLeft` from depth - `.pi-part` was one fixed step). Used on
the data-entry cards, the student/final preview and the teacher's review.
The stored `question_number` stays whole - answers and marking schemes
match on it, and the duplicate check and marks list still show it, since
they mix questions and parts.

**Papers are added by data entry, and no longer given to one operator.**
Asked for, in turn: uploading moved to the admin side, then back - the
admin side is only assigning teachers (and Manage users for a superadmin).
So Admin -> Data entry (`AssignOperators.jsx`), `GET /operators` and `POST
/intake/documents/{id}/operator` are gone; the upload and paste form
(`pages/data-entry/UploadPaper.jsx`) is at the top of Paper intake again,
and its two routes are data entry's (`EMPLOYEE`; admins pass every rule).
Every Data Entry Operator sees and works on every paper still with data
entry (`paper_access`, `_own_papers`); whoever finishes data entry is
recorded as the paper's operator (`finish_data_entry` -> `assign_review`),
so its history and EdSider's operation row still name the person who did
it.

**Data entry deletes a paper while it is still theirs** - Extracted, In
review or Duplicate check done (`store.DATA_ENTRY_DELETABLE`; asked for:
duplicate-checked papers too) - a red Delete in the paper list. After
"Finish data entry" it is in the question bank and with a teacher, and
only an administrator deletes it, from Paper assignment. Before:

**A paper uploaded by mistake can be removed - while it is still
Extracted.** A "Remove" beside each Extracted paper in data entry's paper
list (`PaperFinder.jsx`), `DELETE /intake/documents/{id}` (data entry and
admins). `store.delete_intake_document()` refuses any other status, an
archived paper and one ever sent - by then a copy is in the question bank,
its teachers or EdSider may have it, and its history matters. One delete
takes every row (each intake table names the paper `ON DELETE CASCADE`);
the stored file, pictures and table pictures go only after that commits
(`delete_stored_files`), so a failure leaves an unused file, never a row
naming a missing one.

**A question repeated on its own paper - flagged, replaced, its history
kept.** The duplicate check's results were shown once and saved nowhere,
so a teacher never saw them. Now:
- *Data entry flags* the repeat from the results: on exact copies and
  similar pairs, "Flag Q7 as a repeat of Q3" (the later copy is the repeat)
  - `POST /intake/questions/{id}/flag` (`store.flag_repeat`), taken back
  with `DELETE` while no teacher has acted. The card says it is flagged.
- *The teacher sees it* on the question: "Repeated in this paper - the same
  as Q3", the other question's text beside it, and either **Write a new
  question** (text, answer, marks, and whether its pictures belong to the
  old question) - `POST .../replace`, `store.replace_question` - or **Keep
  it - not a repeat**, with a reason - `POST .../keep`. Review Done is
  refused while a flag is open; Refer Back is not. With two teachers,
  whoever resolves it resolves it for both, and the other sees what was done.
- *The history* (`intake.question_revision`): before a question is
  replaced, what it was - text, answer, marks, type - is saved with who
  replaced it, when and why; `GET /intake/questions/{id}/history` returns
  every earlier version and the current one, shown as "Earlier versions" on
  the teacher's and data entry's cards. Pictures that belonged to the old
  question are not deleted: they move to slot `earlier:<revision>:<slot>`,
  hidden from the question, shown with that version. Flags
  (`intake.question_flag`) and revisions are keyed to the paper, not by
  foreign key to the question, so archiving - which removes the paper's
  question rows - keeps them; the history then finds its pictures in the
  bank copy. Every flag, replacement and keep is in the paper's log.
- *Found on the way:* `question_images_for_document` listed the paper's
  bank copy beside its own pictures for every paper, not only archived
  ones, so from the moment data entry finished (and the bank copy was
  made) the teacher saw every picture twice. It reads the bank copy only
  once the paper is archived.

**Sending a paper to EdSider - steps 5 and 6.** Decided: written straight
into EdSider's database (the pre-prod copy, `examonliine_db_prod_copy`,
for now), arriving as **ReadyToPublish** (status 11 - EdSider's staff then
schedule and publish it there), sent by an admin (MASAD/admin/superadmin)
once both teachers have finished. `POST /intake/documents/{id}/send-to-edsider`
builds the converter's package with the pictures embedded as data: URLs
(EdSider keeps them inside the question), refuses it with every blocking
problem listed (409, `{message, problems}`, shown on the page per
question), then `src/edsider/send.py` writes it in one EdSider transaction
in EdSider's order - paper, schedule, chief assessor, parts, sections,
passages ("ASMNT<id> Q1 - Q2"), questions (parts before their question,
which names their new ids in `sub_questions`), links, the operator's
operation and one review row per teacher - with EdSider's default terms
for the medium. Then `intake.edsider_send` records it (where, who, the
assessment id and every question's new id; a failed attempt too, with the
error) and the paper is archived as 'published' ("Sent to EdSider"). Never
twice. The target is a setting, `EDSIDER_SEND_DB` (on PG_HOST, as
PG_USER) - unset, sending is off, so no deployed app writes to EdSider
until someone sets it there; pointing it at live EdSider is that one
setting. Rehearsed on the pre-prod copy inside a transaction that was
rolled back: every row accepted (types, foreign keys, the state), nothing
left. "Send to EdSider" is on Admin -> Paper assignment, beside a
ready-to-publish paper's status, for MASAD only (`/me` says `masad`, from
`staff.edsider_admin`; the route refuses anyone else with a 403 - an
admin a superadmin made here is not one). Disabled, with the reason, on a
server without `EDSIDER_SEND_DB` (`/health` shows it as `edsider_send`).
Sent, the paper is 'published'. Data entry used to show "Archive the
paper" there instead, and an archived paper can never be sent; it now
only says where the paper is. Every page's status pill is
`components/StatusPill.jsx` - the design bundle's printed most statuses
as their codes ("ready_to_publish"); ready to publish and published are
green.

**The duplicate check no longer takes a paper back from its teachers.**
It set 'checked' whatever the status, so an admin running it from data
entry on a paper in teacher review (paper #90) put it back to 'checked':
both teachers' ticks were refused ("Your review of this paper is
finished") and "Finish data entry" showed again. It now sets 'checked'
only while data entry has the paper (`_PASSED_ON`). And once both
teachers have finished ('ready_to_publish', 'published', archived), the
check, the early check and repeat flags are closed altogether
(`_refuse_once_reviewed`, 409; the page hides them and says why) - admins
could still run them, and a flag then asked for a review already over.

**Then sent for real into the pre-prod copy, and read back as EdSider
reads it** (2026-10-27; the production database and the real pre-prod are
not ours to touch). Paper #55 (GIT A/L model, 64 rows) went in with the
same converter and writer as the button, from a scratch script that does
not archive our paper, and was read back the way EdSider's own
paper-details API assembles a paper
(`paper_details_route.get_assessment_details_with_parts_section`: parts
through `assessment.part`, sections, links by section, `sub_questions`
ids, Draft.js content, MCQ weights), then compared column by column with
#2141, a paper made in EdSider's screens. Three things it found, all fixed:
- *Integer out of range* - the whole send refused at the schedule: the
  paper's access buffer was entered as 100,000,000 hours. The converter
  now reports a duration or buffer outside EdSider's integer column as an
  error naming the setting (EdSider's own papers mostly use 525,600
  minutes, a year).
- *Every part had `part_id` NULL*, and EdSider's paper-details screen skips
  a part it cannot find in `assessment.part` - the paper would have shown
  no questions there. EdSider sets it whenever a paper has a part notation
  (Roman I = 7, II = 8; numeric 1 = 4; alpha A = 1); its NULLs are on
  papers without one (#2141 is an exception, which is where the converter
  copied it from). Now set from the synced `part` list.
- *Titles are unique in EdSider's screens, not its database*, so a second
  send of the same paper got in. `write_package` now refuses a taken title
  (case-insensitive) before writing - a 409 asking for a new title.
After the fixes: every part, section and all 64 links resolve, 40 MCQs
with weights matching their options, 7 questions naming their 17 parts -
EdSider assessment #2156 in the copy. Left as test data there. Not yet
tested: the operation and review rows (#55's operator and reviewer are not
on EdSider's staff list, so none were written), and opening it in
EdSider's own screens. Still open: whether a question our duplicate check found in
EdSider's bank should be linked instead of copied, and a read-only check
after sending that the paper opens in EdSider's own screens.


**Publishing in EdSider - the reviewers' table was renamed.** ASMNT2159
(our demo paper) arrived fine, but EdSider's own Publish button returned
a 500 on the pre-prod copy: its `update_assessment_status` route sets the
paper's status, then the same status on `assessment.assessment_review` -
and the copy has no such table. EdSider renamed `assessment_review_new` to
`assessment_review` in code (its commit 70ed7a63, 2026-09-07) with no
migration in the repo, the table renamed by hand on its own databases; the
copy was made before that. Any paper failed to publish there, not only
ours. `send._review_table()` now writes the reviewers to whichever name the
database has (`to_regclass`), so a send works before and after the copy is
renamed, and on EdSider's real databases. Same as the two
`assessment_part` columns the copy lacked: EdSider's team should know
their copy is behind.


## A paper's way into the question bank - copied, reviewed, then archived

The flow, as decided:
1. **Data entry finishes** - every question reviewed, the duplicate check
   run on the corrected paper ('checked') - with "Finish data entry - copy
   to the question bank" (`POST /intake/documents/{id}/finish-data-entry`):
   the questions are copied into the bank (`store.sync_paper_to_bank()`) and
   the paper, still in intake, becomes 'data_entry_done' ("Ready for a
   teacher"). A Data Entry Operator cannot change it from here unless a
   teacher sends it back (`service._PASSED_ON`).
2. **An administrator assigns a teacher** (Assign to teachers, as before).
   The teacher reviews and edits it in intake. Refer Back returns it to the
   operator, whose next finish refreshes the bank copy. Review Done
   refreshes the bank copy with the teacher's corrections and makes the
   paper 'ready_to_publish'.
3. **An administrator archives it** (`POST /intake/documents/{id}/archive`,
   `store.archive_document()`): the bank copy is refreshed once more, the
   paper's own question rows are removed (their pictures' rows and comments
   with them; the files stay, the bank points at them) and the paper is
   'archived' (`intake.document.archived_at`). Its record, file, parts,
   settings and history stay. Sending it to EdSider belongs to this step
   once EdSider's route exists.

**The bank copy.** Each question and part is a row in `questions` - the
bank that also holds EdSider's synced questions - with id
"intake-<paper>-<question id>", stable however often the copy is refreshed
and wherever the question moves on the paper; parts are linked to their
question as EdSider does. Its place on the paper is a row in
`question_assessments` (the bank's copy of EdSider's question-to-paper
links) under the paper's own assessment id "intake-<paper id>": EdSider's
whole number per section, `printed_number` ("01(1)(a)"), `position`, the
part, and the marks (0 on a question with parts, as EdSider does). Its
pictures are rows in `question_files`, keeping the picture's id. A refresh
updates all of these and removes whatever left the paper since.

**An archived paper reads back from the bank** (`store.intake_questions()`
-> `bank_paper_questions()`, and its pictures through
`question_images_for_document()` / `get_question_image()`), with the same
ids, places, numbers, parts and marks. Every change to it is refused for
every role (`service.paper_access`, 409). The `questions` table is close
to EdSider's `question.question` - a row per question or part, parts linked
to their question - but holds plain text, not Draft.js, and our MCQ
answers are labels ("(2)"), not EdSider's weights; the converter
(`src/edsider/export.py`) does both when a paper is sent. New bank rows
need `embed.py` before the duplicate checks see them.

## Uploaded papers and pictures - in Azure Blob Storage, not the database

Uploaded papers and the pictures data entry attaches were kept in
PostgreSQL as `bytea`, so there was one thing to back up. Measured on the
live data: a paper averages 1.1 MB (largest 6.3 MB), so 100,000 papers
would be about 110 GB on EdSider's database server - its dearest storage,
copied by every backup, each download holding one of its 50 connections.

**`src/files.py`** is now the only module that touches file storage, as
`store.py` is for the database (store calls it; nothing else does):
- Azure Blob Storage when `EDSIDER_BLOB_CONNECTION_STRING` is set - one
  private container, `EDSIDER_BLOB_CONTAINER` (default `intake-files`,
  created on first use). Never public: files are served only through the
  API, behind sign-in and the paper rule. Otherwise a folder,
  `EDSIDER_FILES_DIR` (default `data/files`, gitignored); the tests use a
  throwaway folder each (`conftest.py`), never the real account.
- Names: `papers/<document id>/<sha256>.pdf`,
  `images/<question id>/<image id>.png`.
- `intake.document_file` keeps `file_path`, `file_size`, `file_sha256`;
  `intake.question_image` keeps `image_path`, `image_size` (its
  `image_blob` became optional - `store.MADE_OPTIONAL`, for the one
  change ADD COLUMN cannot express). A file still in the database is read
  from there until moved. Deleting a picture, a question, or re-splitting
  a paper deletes the stored pictures too.
- An upload stores the file before its row, so a failure rolls the whole
  upload back instead of leaving a paper with no file.
- An App Service without the storage settings keeps files in the database
  (`files.available()` is False there), never on its own disk - zip deploy
  replaces that disk, so a folder there would lose every upload since the
  last deploy. So the code can deploy before the settings exist.
- `/health` says where files go ("files": "Azure Blob Storage
  esaidevstr/intake-files") - never the key - so a deployed app's setting
  can be checked without signing in.

**One storage for every app.** Dev, pre-prod and a local run share one
database, so a path one of them records is read by all of them: every one
needs the same `EDSIDER_BLOB_CONNECTION_STRING`. The account is
`esaidevstr` (named for dev, used by all three for that reason).

**Moving what is already there** - `scripts/move_files_to_storage.py`
(`store.files_in_database()`, `store.move_file_to_storage()`) lists by
default and moves with `--apply`: one file at a time, read back before its
bytes are cleared, each committed on its own, safe to re-run. On the live
data: 49 paper files and 22 pictures, 55 MB. Run it only once every
deployed app has the storage settings. The old public-schema tables
(`intake_documents`, `intake_question_images`) hold their own copies until
they are dropped.

Not done: signing in to the account with the App Services' managed
identity instead of the account key (`azure-identity`, role "Storage Blob
Data Contributor") - the key in an app setting works, but a managed
identity has nothing to leak or rotate.

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

**Sign-in - the frontend is already built for Easy Auth, Easy Auth
itself is the one piece still missing.** The data-entry/teacher/admin
React app was deliberately built against Azure App Service's own Easy
Auth (App Service Authentication) rather than EdSider's own custom
Flask/MSAL/JWT login system - a platform feature, not application code:
once an identity provider is configured on the App Service resource,
Azure's own reverse proxy handles the whole OAuth exchange and injects
the signed-in user's identity as a request header, before the request
ever reaches this app. `service.py`'s `current_identity()` already reads
exactly that header (`X-MS-CLIENT-PRINCIPAL-NAME`), and
`frontend/src/lib/auth.js` already builds real Easy Auth URLs
(`/.auth/login/aad`, `/.auth/login/google`, `/.auth/logout`) that
`SignIn.jsx`'s "Sign in with Microsoft"/"Sign in with Google" buttons
and `TopNav.jsx`'s "Sign out" link already use. None of that has
anywhere to go yet, because Easy Auth has not been turned on for the
App Service resource - confirmed directly, not assumed, since there is
no Azure CLI available to even check from this environment, only the
person who manages the Azure resources can.

`BYPASS_SIGN_IN` in `auth.js` tracks `isDevAuth` (`import.meta.env.DEV`)
rather than standing on its own: a real production *build* never
bypasses real sign-in, only `npm run dev` does, since there is no Easy
Auth proxy on localhost regardless of whether Azure is configured. This
used to be a separate flag hardcoded `true` unconditionally - bypassing
sign-in even in a deployed production build, because turning it off
before Easy Auth existed on Azure would have 404'd every real sign-in
attempt. **Deploying this change without first completing the Azure-side
step below will do exactly that** - the two must go together.

**To turn Easy Auth on** (Azure Portal, on the `Eds-dev-ai-api` App
Service resource - someone with access to the Azure subscription, not a
code change):

1. **Authentication** blade -> **Add identity provider**.
2. Pick **Microsoft** (Entra ID) and/or **Google**, matching the two
   buttons already built. Microsoft needs an App Registration (Azure
   can create one automatically from this same screen, or an existing
   one can be reused); Google needs a Client ID/Secret from Google
   Cloud Console.
3. Under **Authentication settings**, **"App Service authentication"**
   has two modes, and which one to pick is a real open question, not
   something to guess from this repo alone: **"Require authentication"**
   makes Azure redirect every unauthenticated request to login before it
   reaches the app at all - the most direct match for "a user must log
   in" - but it would also gate `/health`, which an uptime probe calls
   with no login and which `EDSIDER_API_KEY`'s own separate gate already
   deliberately leaves open (see `require_api_key`'s docstring). Whether
   this tier of App Service can exclude specific paths (`/health`) from
   that redirect, or whether the health check needs to move to Azure's
   own platform-level health-check feature instead of an external
   anonymous probe, needs checking against the real resource, not
   assumed from here. The other mode, **"Allow unauthenticated access"**,
   leaves every route reachable without logging in and only makes the
   sign-in buttons functional - closer to what exists today than to "the
   user must log in," so on its own it does not satisfy that requirement.
4. Once saved, `/.auth/login/aad` and `/.auth/login/google` go live
   immediately - no redeploy needed for that part. The redeploy that
   *is* needed is this repo's own `BYPASS_SIGN_IN` fix, so production
   stops bypassing sign-in once there is somewhere real for it to go.

**Two providers at once needs a setting, not Azure AD B2C.** With
"Require authentication" selected and more than one identity provider
configured, App Service auto-redirects every unauthenticated request to
ONE designated default provider - Google never gets a chance, because
that redirect fires before either of `SignIn.jsx`'s own explicit
`/.auth/login/aad` / `/.auth/login/google` links are ever reached.
Microsoft's own documented fix for more than one sign-in option is
"Allow unauthenticated access" instead, letting the app's own explicit
per-provider links do the choosing - no B2C tenant needed (B2C is a
heavier, separate path: its own tenant, its own app registration, Google
and Microsoft re-registered as ITS identity providers rather than
App Service's, and it is also end-of-sale to new customers as of May
2025, pointing new projects at Microsoft Entra External ID instead - a
real dead-end risk worth checking before investing time there). The
trade-off that setting creates is the next section.

**Who is allowed to sign in - EdSider's own staff pattern, not a
self-service sign-up.** Checked directly against EdSider's own code
(`D:\Edsider\OAT-API-2.0`, `OAT-WEB-2.0`): there is no self-service
"register your email, get a verification link" flow for teacher/admin/
data-entry staff at all - that flow exists only for a different
audience (parent/student enrollment, built on Flask-Mail/SMTP, not
Azure Communication Services despite `VITE_ACS_*` vars sitting unused in
EdSider's own `.env`). For staff, an administrator adds each person by
hand (email + role) through a management screen, and at login EdSider
looks the signed-in email up in its own `staff_user` table, rejecting
anyone not found with "You are not a user! Please contact System
Administrator!" - email is the sole link to identity; no password, no
B2C object id is stored anywhere.

This repo now does the same thing, deliberately, rather than standing
up email infrastructure nothing here needs yet - and it closes the gap
"Allow unauthenticated access" above would otherwise open, since nothing
in this app enforced sign-in on its own before this (`current_identity()`
silently defaulted to `"reviewer"`, and no frontend route existed to
turn an unauthenticated visitor away):

- **Staff profiles - one `staff` table.** Everyone this app knows as
  staff, one row per email: EdSider's teachers (`in_edsider`, from the
  teacher sync), people a superadmin added (`role`, `added_by`), and
  anyone seen in the teacher app (`first_seen_at`/`last_seen_at`), plus
  `active` (a superadmin's switch-off) and `subjects` (JSON list, for
  narrowing the assignment dropdown later). It replaced three tables
  that each held part of this - `authorized_users`, `edsider_teachers`,
  `teachers` - so sign-in, Manage users and the dropdown read one place.
  Deliberately NOT stored: anything EdSider owns about a person (phone,
  address) - EdSider is where it gets edited, and a copy would drift.
  Who may sign in, and as what, is one SQL rule (`store.STAFF_ROLE_SQL`,
  surfaced as `effective_role`): nobody inactive; else the role a
  superadmin set; else `teacher` for anyone EdSider lists as one; else
  nobody. Removing what a superadmin granted puts an EdSider teacher
  back to teacher; switching them off is how to stop them entirely. A
  superadmin cannot switch themselves off. Managed on
  `/app/admin/users` (`ManageUsers.jsx`: search, per-row role/subjects,
  switch on/off), routes GET/POST/PATCH/DELETE `/admin/users`.
  **The old three tables are copied into `staff`, never dropped
  automatically** (`store._merge_old_people_tables()`, once, when
  `staff` is first created): a local run and the deployed app share one
  database, so dropping `teachers` the moment this version ran locally
  would break the still-deployed older version's teacher pages. Once
  this version is the one deployed, drop them by hand:
  `DROP TABLE teachers, authorized_users, edsider_teachers;`. Anything
  the older version writes to them between the copy and that deploy is
  not carried over.
- **Four roles: `superadmin`, `admin`, `teacher`, `employee`.** Only
  `superadmin` can manage the allowlist (`require_superadmin()` on all
  three `/admin/users` routes; the "Manage users" nav link and page are
  hidden/blocked for anyone else). `admin` keeps today's operational
  work - assigning papers to teachers - but cannot add, remove or
  re-role anyone. `employee` is the data-entry / question-review
  person; "staff" was asked for too and confirmed to mean the same job,
  so it is not a separate role.
- **Each role is kept to its own section - on the API, not only the
  page.** `service.allow(*roles)` is on every data route (applied from
  one table, so each route's section is a deliberate choice): data
  entry (`EMPLOYEE`: upload, paste, edit, marks, pictures, duplicate
  checks, search, decisions), teacher review (`TEACHER`: my papers,
  comments, finish), both (`EMPLOYEE_OR_TEACHER`: reading a paper, its
  questions, file and pictures, and editing a question - the teacher's
  own review tick goes through that PATCH), admin (`ADMIN`: assign, the
  teacher list, `/reload`). Admin and superadmin pass every rule - they
  oversee all three sections - and Manage users stays superadmin only.
  A local run (role None) is open, as everywhere else. A refusal is a
  403, "Your role cannot use this part of EdsiderAI."
  `tests/test_roles.py` fails if any route ever lacks a role rule, the
  same "everything except a short list" shape as the API-key test, so a
  new route cannot quietly be open to every signed-in person. On the
  page, each Layout passes its `roles` to `RequireUser`, which shows
  "This section is not for your role" with a link to the person's own
  section instead of a page whose every request would be refused.
  Still role-level only: a teacher can read any paper through the
  shared routes, not just the ones assigned to them.
- **`current_identity()` now rejects, rather than defaulting to
  "reviewer", whenever Easy Auth reports a real email that is not on
  this list** - a 403 with the same spirit as EdSider's own message.
  The no-header case (a local run, or the tests - Easy Auth was never
  in front of the request) is unchanged: still "reviewer", no role,
  nothing to check against an allowlist that only matters once Easy
  Auth is actually in front of real traffic. `current_user()` returns
  identity and role together, for `/me` and `require_admin()`.
- **`EDSIDER_BOOTSTRAP_ADMINS`** (comma-separated emails, an app
  setting, never in code) is the way into an otherwise-empty allowlist:
  each listed email is always treated as `superadmin`, whatever the
  table says. Without it, a freshly deployed app with Easy Auth on and
  zero rows in `authorized_users` would lock out even the person meant
  to add everyone else.
- **`GET /me` is the actual "at login, check the signed-in email
  against the list" gate** - not something re-checked route by route.
  The frontend's `RequireUser` wraps every top-level Layout
  (data-entry, teacher, admin), calls it once through `useMe()`, and
  shows "ask an administrator to add your email" instead of the app on
  a 403. `TopNav`'s `who` text (three hardcoded placeholder strings
  before this - "Signed in as reviewer" / "...teacher" / "...
  administrator", visible in the git history) now shows the real
  signed-in person's name from that same call.
- **Every question edit is stamped with who made it** -
  `intake_questions.last_edited_by` / `last_edited_at`, written by
  `store.stamp_question_editor()` alongside every
  `update_intake_question()` call (the plain-field PATCH, the per-paper
  marks bulk-set, the MCQ reassembly, and the fill-blank reassembly),
  never on a read or a no-op save. Shown as a small caption on the
  question card (`PaperIntakeQuestion.jsx`) once a question has been
  edited at least once; blank for anything still exactly as the
  extractor produced it.
- **EdSider's own teachers fill the assignment dropdown, and sign in
  without being added by hand.** `scripts/sync.py`'s `sync_teachers()`
  copies every `TEACH`-role staff member (`user_management.staff_user` +
  `staff_user_role`, name from `registration.teacher` via
  `staff_user.staff_id`) into `edsider_teachers` - all of it confirmed
  against the live schema: `TEACH` is "Teacher" in
  `user_management.role` (`DEOP` is "Data Entry Operator", `SYST`
  "System Administrator"), every one of the 135 TEACH rows has a teacher
  row with a matching email, 133 distinct emails (two people listed
  twice), all `user_status_id` 1 ("Active"), and names carry stray
  spaces. Email is the key because it is what Easy Auth reports at
  sign-in, so a paper assigned to it is exactly what that teacher sees.
  129 of the 135 are Gmail and the rest personal Microsoft accounts
  (live/hotmail) - no work/school accounts, which matters for how
  Microsoft sign-in is configured. Every sync re-reads the whole list (a
  watermark would never notice a removed teacher) into `staff`
  (`store.sync_edsider_teachers()`): listed people are marked
  `in_edsider`, anyone no longer listed is unmarked - never deleted, so
  their history and settings stay. EdSider's name is taken unless a
  superadmin entered the person by hand. A sync returning nobody is
  refused (more likely a broken query than true), and it is one
  statement: row by row it took 44 s from this laptop, batched 6-7 s,
  most of that connection setup. The assignment page shows the saved
  copy at once and calls `POST /teachers/sync` (admin/superadmin,
  `require_assigner()`) to refresh it, keeping the saved copy with a
  warning if EdSider cannot be reached; `run_sync()` refreshes it too. `PG_USER` in `.env` is currently `postgres`,
  not the read-only account `sync.py` was designed around - every read
  here forces a read-only session, but the account itself should be
  swapped (see `.env.example` for the grants it now needs).
- **This also changes `TeacherReview.jsx`'s "view as" dev-only identity
  picker**: typing an arbitrary name there now goes through the same
  allowlist check as a real sign-in, since the backend has no way to
  tell a locally-simulated Easy Auth header from a real one. Trying the
  teacher flow locally now means adding that test identity through
  `/app/admin/users` first, same as a real deployment would need - a
  deliberate tightening, not an oversight.

**One address for the page and the API - why sign-in did nothing.**
Deployed as two App Services, `eds-dev-ai-web-...` (the page) and
`eds-dev-ai-api-...` (the API), each with its own Easy Auth: the web
app has Microsoft and Google, the API only Microsoft, under a different
app registration. Checked live - both apps' `/.auth/login/*` redirect
correctly. But Easy Auth adds the signed-in person's identity only to
requests for the app they signed in to, and a browser never carries one
app's sign-in cookie to another: every API call arrived with no
identity, `_resolve_identity()` treated that as a local run, and
everyone was "Signed in as reviewer" with full access - the allowlist
never consulted, and anyone could call the API without signing in (the
API key is in the page's own code, so it stops no one).

Fixed by serving the built page from the API itself: if
`frontend/dist/index.html` exists (`EDSIDER_FRONTEND_DIR` to move it),
`service.py` serves its files and, for the app's own page addresses
(`/`, `/sign-in`, `/app/...`), `index.html` (`Cache-Control: no-cache`,
since it names the build's hashed files); any other address is a plain
404, and a path cannot climb out of `dist`. The route is registered last
so every API route matches first. `frontend/.env.production` builds the
page to call the API at its own address (`VITE_API_BASE_URL` empty;
`api.js` uses `??`, so empty means "same address", not "unset"); a
`VITE_API_BASE_URL` set in the build environment still wins, so the old
two-app deployment keeps working until the pipeline changes.

The gate is middleware (`require_signed_in_staff`), not per route: every
API request with an identity must resolve on the staff list (403
otherwise - including routes that never ask who is calling), and with
`EDSIDER_REQUIRE_SIGNIN=true` a request with no identity is a 401
(`_resolve_identity()` applies the same rule, for anything calling it
directly). `/health` and the page's own files stay open - the sign-in
page must load before anyone can sign in. Added before CORS, so CORS
wraps it and a refusal still carries the headers the browser needs to
read it. On the page: `RequireUser` sends a 401 to `/sign-in?next=...`,
and `SignIn.jsx` skips its buttons for someone already signed in -
forwarding to `next` (only ever an `/app/...` address, so a crafted link
cannot send people elsewhere) or to their role's section. The sign-in
and sign-out buttons are plain `<a>` links: as router `<Link>`s they
never reached Azure at all, and rendered the app's own "Not found" at
`/.auth/login/aad`.

**Until that single-app deploy happens, the page forwards the ID token -
and that alone closes the hole.** Reported live: anyone with any Google
account could open every page, admin included. The page reads its own
Easy Auth session (`/.auth/me` on its own address, which holds the
Google/Microsoft ID token issued at sign-in) and sends it as
`X-EdsiderAI-Id-Token` on every API call (`api.js`, cached until a
minute before expiry). `src/id_tokens.py` verifies it - RS256 signature
against Google's/Microsoft's published keys (PyJWKClient, cached), issuer,
expiry, and audience: only tokens issued to THIS app's Google client and
Microsoft app registration (`DEFAULT_AUDIENCES`, the client IDs the web
app's own login redirects name - public, not secrets;
`EDSIDER_TOKEN_AUDIENCES` for another environment), since a token Google
issued to any other site carries a real email too and could otherwise be
replayed here. Google tokens must have `email_verified`; Microsoft's email
is `email` or `preferred_username`. Only then does the staff list decide.
A header of our own, not `Authorization`, because the API's own Easy Auth
reads bearer tokens itself. Easy Auth's `X-MS-CLIENT-PRINCIPAL-NAME` wins
when present (App Service strips any copy a caller sends). Sign-in is now
**required by default on Azure**: App Service always sets
`WEBSITE_SITE_NAME`, so `_require_signin()` turns on there without anyone
setting `EDSIDER_REQUIRE_SIGNIN` (which still overrides either way) -
deploying the code is enough. ID tokens last about an hour; an expired
one is a 401, and the sign-in page then sends the person back through
their provider once (at most every two minutes, so it cannot loop),
which issues a fresh token, normally without a prompt. Needs Easy Auth's
**token store** on the web app (it is on by default) - `/.auth/me` must
show an `id_token` after signing in.

**To deploy it** (the pipeline and the Azure resource, not code):
1. Build the page into the API's deployment: `cd frontend && npm ci &&
   npm run build`, with `VITE_API_BASE_URL` NOT set in the pipeline (or
   set empty), and ship `frontend/dist` with the API.
2. On the API App Service: Authentication - add **Google** too (it has
   only Microsoft today), with the API's own callback
   `https://<api-host>/.auth/login/google/callback` added to the Google
   OAuth client's redirect URIs; keep "Allow unauthenticated access".
3. App settings: `EDSIDER_REQUIRE_SIGNIN=true`,
   `EDSIDER_BOOTSTRAP_ADMINS=<first superadmin's email>`.
4. Open the API's address, not the web app's - the separate web app is
   then no longer needed.

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
