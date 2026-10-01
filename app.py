"""EdSider AI - duplicate detection and semantic search.

Run with:   streamlit run app.py
"""

import html
import json
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_pdf_viewer import pdf_viewer
from streamlit_quill import st_quill

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.dedupe import guards
from src.dedupe.embeddings import DEFAULT_MODEL
from src.intake import extract as intake_extract
from src.intake import review as intake_review
from src.normalize import content_hash

st.set_page_config(page_title="EdSider AI", layout="wide")


# ---------------------------------------------------------------- data

# Everything below is keyed on the database name, so a different bank is
# a different cache entry rather than the previous one's questions.
#
# How long the app trusts what it has loaded. The bank changes when
# sync.py or embed.py runs, not while someone is reviewing; the sidebar's
# "Reload from database" is there for the moment it has.
BANK_TTL = 30 * 60


@st.cache_resource(ttl=BANK_TTL, show_spinner="Loading the question bank...")
def load_rows(db_name):
    """The comparable, fingerprinted bank, loaded once and shared.

    This was cache_data with a 5-second lifetime - harmless on SQLite, but
    against the Azure server it meant re-downloading 16,084 questions and
    49 MB of fingerprints (95 s) every few clicks, and copying all of it
    into every rerun on top. cache_resource hands every rerun the same
    list; nothing on any page modifies it.
    """
    with store.connect() as conn:
        rows = store.comparable_questions(conn, DEFAULT_MODEL)
        total = store.count(conn)
    return rows, total


@st.cache_resource(ttl=BANK_TTL)
def load_index(db_name):
    """The bank's fingerprints as one matrix, built once.

    check_one, search and find_pairs each build this themselves when
    handed plain rows - stacking 16,084 fingerprints on every search and
    every duplicate check. Handed the index, they reuse it.
    """
    rows, _ = load_rows(db_name)
    return dedupe.VectorIndex(rows)


@st.cache_resource
def get_embedder():
    from src.dedupe.embeddings import Embedder
    return Embedder()


@st.cache_data(ttl=300)
def sweep(db_name, threshold, use_guards):
    """A full sweep is expensive on a real bank - cache it per setting."""
    return dedupe.find_pairs(load_index(db_name), use_guards=use_guards,
                             threshold=threshold)


@st.cache_resource(ttl=BANK_TTL, show_spinner="Loading the papers...")
def load_assessment_groups(db_name):
    """Comparable, fingerprinted questions grouped by the paper they're
    on. Only groups of more than one question can possibly contain a
    within-paper repeat, so smaller ones are dropped here rather than
    compared for nothing.

    cache_resource, not cache_data: cache_data hands every rerun its own
    copy, and on the real bank these groups are 274 MB of rows - a
    second of copying on every click. Nothing on the page modifies the
    rows, so one shared copy is safe."""
    # One query for every paper. It was one query per paper - 1,960 round
    # trips, a quarter of an hour against the Azure server.
    with store.connect() as conn:
        groups = store.questions_in_assessments(conn, DEFAULT_MODEL)
    return {aid: rs for aid, rs in groups.items() if len(rs) > 1}


@st.cache_data(ttl=300, max_entries=20)
def paper_repeats(group_key, _groups):
    """Repeats on each paper, and the shared-passage notes for them.

    Cached because every click on the page - a verdict, a checkbox, a
    filter - reruns the script, and this is seconds of work on the real
    bank. `group_key` names exactly which questions on which papers are
    being compared (database and filters included), so a different
    filter is a different entry; `_groups` itself is not hashed, which is
    what the leading underscore tells Streamlit. Reviewed pairs are
    removed after this, by split_reviewed(), so a verdict still takes
    effect on the next rerun without waiting for the cache.
    """
    results = dedupe.find_within_assessment_duplicates(_groups)
    # Informational only - does not change results or scores. See
    # dedupe.find_shared_passage_groups() for why this is content-based
    # (a real shared block) rather than "consecutive question numbers".
    # Only papers with a repeat get a note, so only they are grouped.
    papers_hit = {p["assessment_id"] for p in results}
    passage_groups = {aid: dedupe.find_shared_passage_groups(_groups[aid])
                      for aid in papers_hit}
    return results, passage_groups


@st.cache_resource(ttl=BANK_TTL)
def all_assessment_links(db_name):
    """Which papers each question appears on - informational context,
    not a flag. The same question on two different papers is normal; it
    is only a problem when it repeats on the SAME paper (see the
    'Duplicates within a paper' page).

    Loaded for the whole bank at once: asking per question was 100 round
    trips for one page of 50 pairs."""
    with store.connect() as conn:
        return store.assessment_links(conn)


def assessment_caption(qid):
    links = all_assessment_links(store.display_name()).get(qid)
    if not links:
        return
    where = ", ".join(f"#{l['assessment_id']}" for l in links)
    st.caption(f"Also appears on assessment(s): {where}")


def title_caption(q):
    """The per-question code EdSider stores against this question - a
    label for a reviewer to cross-reference against the paper, never
    fingerprinted and never part of the comparison itself."""
    title = q.get("title")
    if title:
        st.caption(f"Title: {title}")


# Bold/italic/subscript/superscript for correcting OCR damage that is
# genuinely a formatting loss (a squeezed fraction, a flattened
# exponent) rather than a wrong character - a wrong character (x?
# instead of x², € instead of ∈) is already fixable by typing the
# right Unicode symbol into plain text and needs none of this.
RICH_TOOLBAR = [["bold", "italic"], [{"script": "sub"}, {"script": "super"}]]


class _PlainTextExtractor(HTMLParser):
    """Quill's editor returns HTML so formatting round-trips while
    editing; what gets saved to the database is plain text, same as
    every other question - normalize()/content_hash() and the
    embedding both assume plain text, and teaching them to strip HTML
    is a bigger, separate change (see the rich-text storage decision).
    Formatting is therefore an editing convenience only: it is visible
    and usable while correcting a question, then discarded on save,
    the same as it would be if it had never been typed."""
    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "div", "br"):
            self.parts.append("\n")

    def reset(self):
        super().reset()
        self.parts = []


def quill_to_text(html_value):
    """Quill's HTML output -> the same plain text a plain text box
    would have held. Empty input gives "", matching str.strip() on an
    empty text_area rather than Quill's own empty-editor HTML."""
    if not html_value:
        return ""
    parser = _PlainTextExtractor()
    parser.feed(html_value)
    return "".join(parser.parts).strip()


def text_to_quill_html(text):
    """Plain text -> safe HTML for Quill's initial value - the inverse
    of quill_to_text(). Needed because what is stored has always been
    plain text (only how it is edited changed here, not how it is
    saved): unescaped, a "<" or "&" - not unusual in a maths paper's
    inequalities - would be misread as markup, and a bare newline is
    not a line break in HTML."""
    if not text:
        return ""
    return html.escape(text).replace("\n", "<br>")


@st.cache_data(show_spinner=False)
def _intake_blob(doc_id, db_name):
    """The uploaded file's own bytes - cached per (document, database),
    not fetched fresh on every rerun. Ticking one Reviewed checkbox
    reruns the whole page; without this, that meant a database round
    trip for a multi-MB file on every single click, for a file that
    never changes once uploaded."""
    with store.connect() as conn:
        return store.intake_file_blob(conn, doc_id)


@st.cache_data(show_spinner=False)
def _question_image_blob(image_id, db_name):
    """One attached image's own bytes - cached per (image, database),
    the same reasoning as _intake_blob(): a rerun should not re-fetch
    every attached image on the page just because one checkbox changed."""
    with store.connect() as conn:
        return store.question_image_blob(conn, image_id)


# The Paper intake page re-fetches these three on every single rerun -
# and in Streamlit, EVERY widget interaction is a full rerun of the
# whole script, not just of whatever changed. Ticking one Reviewed
# checkbox among forty questions was paying for a fresh database round
# trip for every question and every attached image on the page, each
# one costing real time over the network to Azure - not a rendering
# cost, an actual re-query cost, repeated on every click. Cached here,
# and explicitly cleared at every place that writes to intake_documents,
# intake_questions or intake_question_images, so a save still shows
# its own fresh result immediately - only the OTHER, unrelated reruns
# (a different question's checkbox, an unrelated widget) get to skip
# the round trip.
@st.cache_data(show_spinner=False)
def _load_intake_docs(db_name):
    """intake_documents() is SELECT * - which pulls file_blob (the
    paper's own uploaded bytes) along with everything else. Postgres
    hands that back as a memoryview, which st.cache_data cannot pickle
    into its cache at all (UnserializableReturnValueError), and caching
    the whole blob for every document just to show a selector and a
    status badge would be wasteful even if it could. Dropped here - the
    preview pane already fetches one document's own blob separately,
    on demand, via _intake_blob()."""
    with store.connect() as conn:
        docs = [dict(d) for d in store.intake_documents(conn)]
    for d in docs:
        d.pop("file_blob", None)
    return docs


@st.cache_data(show_spinner=False)
def _load_intake_drafts(doc_id, db_name):
    with store.connect() as conn:
        return [dict(q) for q in store.intake_questions(conn, doc_id)]


@st.cache_data(show_spinner=False)
def _load_intake_images(doc_id, db_name):
    with store.connect() as conn:
        return store.question_images_for_document(conn, doc_id)


def render_paper_preview(doc, doc_id):
    """The uploaded paper itself, next to the editable question cards -
    so a reviewer can check a suspect OCR read against the original
    page instead of trusting the extracted text blind.

    A PDF is rendered with streamlit-pdf-viewer (PDF.js under the
    hood), not hand-rolled HTML. Two things were tried and measurably
    failed before this: pdfplumber page images one page at a time
    (worked, but not practical to actually use - no continuous scroll);
    then a hand-built <embed>/<iframe> with a base64 data: URI, and
    then a Blob/JavaScript object URL - both rendered as nothing at
    all, silently, no error, which real production PDF viewers exist
    precisely to avoid by not depending on the browser's own plugin
    behavior. Not worth a third hand-rolled guess.

    A Word document has no equivalent here - a first version fell back
    to showing the extracted text, but a wall of plain text is not a
    preview and looked like a broken one. Rendering a real page image
    would mean converting it to PDF first, which needs LibreOffice (or
    similar) installed on the server - the same wall Tesseract already
    hits: not installable on a zip-deployed Azure App Service, only a
    custom container. Not worth half-building; a reviewer opens the
    original file itself alongside this page instead, until that
    infrastructure exists.
    """
    db_name = store.display_name()
    blob = _intake_blob(doc_id, db_name)
    if blob is None:
        st.info(
            "No preview stored for this paper - it was uploaded before "
            "previews were kept. Re-upload it to get one."
        )
        return

    file_type = doc.get("file_type")
    if file_type == "pdf":
        pdf_viewer(blob, width="100%", height=800, key=f"pdf_{doc_id}",
                  render_text=True)
    elif file_type == "image":
        st.image(blob)
    else:
        st.info(
            "No preview for Word documents yet - open the original file "
            "alongside this page to check it against the extracted text."
        )


# ------------------------------------------------------------ decisions

def split_reviewed(pairs, show_reviewed):
    """Drop pairs that already have a verdict, so a reviewer is not
    shown the same pair again on every visit.

    Returns (pairs to show, how many were already reviewed, how many
    were skipped this session). A reviewed pair is kept only when asked
    for, carrying its verdict so the page can say so."""
    with store.connect() as conn:
        decided = store.current_decisions(conn)
    skipped = st.session_state.get("skipped_pairs", set())
    visible, n_reviewed, n_skipped = [], 0, 0
    for p in pairs:
        key = store.pair_key(p["a"], p["b"])
        if key in decided:
            n_reviewed += 1
            if show_reviewed:
                visible.append({**p, "decision": decided[key]})
        elif key in skipped:
            n_skipped += 1
        else:
            visible.append(p)
    return visible, n_reviewed, n_skipped


def verdict_word(verdict):
    return {"duplicate": "Duplicate", "distinct": "Not a duplicate"}.get(
        verdict, verdict)


def show_last_decision():
    """A verdict reruns the page so its pair leaves the list at once;
    without this the reviewer would see the pair vanish with no sign it
    was saved, or what words were captured."""
    msg = st.session_state.pop("last_decision", None)
    if not msg:
        return
    st.toast(msg["text"])
    st.success(msg["text"] + " It is listed on the **Reviewed decisions** page.")
    only_a, only_b = msg.get("terms") or ([], [])
    if only_a or only_b:
        st.caption(
            "Contrasting words captured - only in the first question: "
            f"{', '.join(only_a[:10]) or '(none)'}; only in the second: "
            f"{', '.join(only_b[:10]) or '(none)'}"
        )


def current_reviewer():
    """Who is actually clicking Confirm/Not a duplicate.

    Azure App Service's built-in login (Easy Auth) sits in front of the
    app and, once configured, injects the signed-in user's identity as
    request headers - the app itself never has to talk to Azure AD B2C
    or verify a token, Easy Auth already did that before this request
    ever reached Streamlit. Falls back to the literal "reviewer" when
    there is no such header - a local run, or before Easy Auth is
    turned on - so this is safe to ship ahead of that Azure-side change
    and starts attributing real names the moment it is switched on,
    with no further code change needed.
    """
    name = st.context.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    return name or "reviewer"


def decision_note(pair):
    d = pair.get("decision")
    if d:
        st.info(
            f"Already reviewed: **{verdict_word(d['verdict'])}**, by "
            f"{d['decided_by']} at {d['decided_at']}. Choosing again "
            "replaces that verdict."
        )


def decision_buttons(pair, qa, qb, key, guard_flags):
    """Confirm / Not a duplicate / Skip for one flagged pair.

    A verdict is saved and the page reruns, so the pair disappears from
    the list straight away. Skip saves nothing - a skip is not a verdict
    - and only hides the pair until the app is reopened."""
    label = f"{pair['a']} vs {pair['b']}"
    d1, d2, d3, _ = st.columns([1, 1, 1, 3])
    if d1.button("Confirm duplicate", key=f"dup_{key}"):
        with store.connect() as conn:
            store.record_decision(
                conn, pair["a"], pair["b"], "duplicate", current_reviewer(),
                pair["score"], guard_flags,
            )
        st.session_state["last_decision"] = {
            "text": f"{label} saved as a duplicate."}
        st.rerun()
    if d2.button("Not a duplicate", key=f"not_{key}"):
        # The system flagged these as similar - whatever vocabulary
        # differs between them is a candidate reason why they are not
        # the same question, worth capturing for later guard tuning even
        # though nothing acts on it automatically.
        only_a, only_b = guards.candidate_contrast_terms(qa["text"], qb["text"])
        with store.connect() as conn:
            store.record_decision(
                conn, pair["a"], pair["b"], "distinct", current_reviewer(),
                pair["score"], guard_flags,
                candidate_contrast_terms=(only_a, only_b),
            )
        st.session_state["last_decision"] = {
            "text": f"{label} saved as not a duplicate.",
            "terms": (only_a, only_b)}
        st.rerun()
    if d3.button("Skip", key=f"skip_{key}"):
        st.session_state.setdefault("skipped_pairs", set()).add(
            store.pair_key(pair["a"], pair["b"]))
        st.rerun()


# ------------------------------------------------------------- sidebar

st.sidebar.title("EdSider AI")

# One bank, named by EDSIDER_DB - a PostgreSQL connection string. The
# selector over data/*.db went with SQLite: a PostgreSQL database is not
# something to discover by looking in a folder, and a server is
# configured, not browsed. A different bank (a staging copy) is a
# different EDSIDER_DB, or a different EDSIDER_SCHEMA on the same server.
if store.DSN is None:
    st.title("EdSider AI")
    st.error(
        "No database configured. Set `EDSIDER_DB` to a PostgreSQL "
        "connection string and restart:\n\n"
        "```\n$env:EDSIDER_DB = "
        "\"postgresql://edsider_ai:PASSWORD@localhost:5432/edsider_ai\"\n"
        "python scripts/sync.py --full\n"
        "python scripts/embed.py --language English\n```\n\n"
        "`.env.example` shows the settings; `.env` itself is gitignored."
    )
    st.stop()

@st.cache_resource(ttl=60, show_spinner="Connecting to the database...")
def database_problem(db_name):
    """None when the database answers, else why not. Checked once a
    minute rather than on every click: each check is a round trip, and a
    click already makes the ones it needs."""
    try:
        with store.connect():
            return None
    except Exception as unreachable:  # noqa: BLE001 - shown to the reviewer
        return str(unreachable)


problem = database_problem(store.display_name())
if problem:
    database_problem.clear()  # try again on the next click, not in a minute
    st.title("EdSider AI")
    st.error(
        f"Cannot reach the database named by `EDSIDER_DB`:\n\n"
        f"```\n{problem}\n```"
    )
    st.stop()

page = st.sidebar.radio(
    "View",
    ["Check a new question", "Find duplicates", "Duplicates within a paper",
     "Reviewed decisions", "Search by meaning", "Question bank",
     "Paper intake"],
)

rows, total_questions = load_rows(store.display_name())

st.sidebar.divider()
st.sidebar.caption(f"Database: {store.display_name()}")
st.sidebar.caption(f"{total_questions:,} questions loaded")
st.sidebar.caption(f"{len(rows):,} comparable, with fingerprints")
st.sidebar.caption(f"Model: {DEFAULT_MODEL.split('/')[-1]}")
# The bank is held for BANK_TTL. This button used to only re-read OUR
# OWN database - which does nothing useful unless sync.py already ran
# separately - so it now pulls from EdSider first (an incremental sync,
# never --full: that re-reads the whole bank and is a by-hand, one-off
# operation, not something a button click should risk). If the live
# pull can't run at all - no PG_* credentials here, or EdSider's server
# unreachable - it falls back to the old behaviour (reload our own copy
# only) rather than leaving the sidebar looking like nothing happened.
if st.sidebar.button("Sync from Database", use_container_width=True):
    message, changed = None, True
    try:
        with st.spinner("Pulling new questions from EdSider..."):
            from scripts.sync import run_sync
            result = run_sync(full=False)
        changed = bool(result["imported"] or result["links_saved"])
        message = (
            f"Synced: {result['imported']:,} new/updated question(s), "
            f"{result['skipped']:,} skipped, "
            f"{result['links_saved']:,} assessment link(s). "
            f"Bank now has {result['total']:,} questions."
            if changed else
            "Already up to date - nothing new on EdSider's side."
        )
    except SystemExit as e:
        message = (f"Live sync isn't configured on this server ({e}) - "
                   "reloaded our own database instead.")
    except Exception as e:
        message = (f"Could not reach EdSider's database ({e}) - "
                   "reloaded our own database instead.")

    # Reloading re-reads and re-stacks the whole bank from our own
    # database - measured at several seconds even once cached - so only
    # pay for it when something might actually be different. On an error
    # `changed` stays True: a local import could have added rows even
    # though the live pull failed, and that is the one case the old
    # "Reload from database" button existed for in the first place.
    if changed:
        for cached in (load_rows, load_index, load_assessment_groups,
                       all_assessment_links, sweep, paper_repeats):
            cached.clear()
    st.session_state["last_sync_message"] = message
    st.rerun()

if st.session_state.get("last_sync_message"):
    st.sidebar.caption(st.session_state.pop("last_sync_message"))

if not rows:
    st.title("EdSider AI")
    st.error(
        f"**{store.display_name()}** holds {total_questions:,} questions but "
        "none of them have fingerprints yet.\n\n"
        "Either pick another bank in the sidebar, or build them:\n\n"
        "```\npython scripts/embed.py\n```"
    )
    st.stop()


# ------------------------------------------------ check a new question

if page == "Check a new question":
    st.title("Check a new question")
    st.caption(
        "Before adding a question, check whether the bank already has "
        "it. This compares one question against the bank, which is what "
        "happens at save time - not a full sweep."
    )

    draft = st.text_area(
        "The question you are about to add",
        height=110,
        placeholder="Type or paste a question...",
    )

    c1, c2, c3 = st.columns(3)
    grade = c1.selectbox(
        "Grade", [""] + sorted({r["grade"] for r in rows if r["grade"]})
    )
    subject = c2.selectbox(
        "Subject", [""] + sorted({r["subject"] for r in rows if r["subject"]})
    )
    check_guards = c3.toggle("Safety checks ON", value=True, key="check_guards")

    if draft.strip():
        new_hash = content_hash(draft)

        # Pass 1 first. An exact match costs one indexed lookup and
        # makes the model unnecessary, so never load it before trying.
        with store.connect() as conn:
            exact = [dict(r) for r in store.find_by_hash(conn, new_hash)]

        st.divider()

        if exact:
            st.error(
                f"**Exact duplicate** of "
                f"{', '.join(r['id'] for r in exact)}. This question is "
                "already in the bank, word for word."
            )
            for r in exact:
                st.markdown(f"**{r['id']}**")
                title_caption(r)
                st.write(r["text"])
                st.caption(
                    f"Grade {r['grade']} - {r['subject']} - "
                    f"{r['topic'] or 'untagged'}"
                )
                assessment_caption(r["id"])
            st.info(
                "Matched by hash alone. No embedding was computed - the "
                "cheapest pass settled it."
            )
        else:
            with st.spinner("Comparing against the bank..."):
                embedder = get_embedder()
                # passage, not query: the bank was embedded as passages,
                # and a duplicate check compares like with like.
                qvec = embedder.encode(draft, is_query=False)[0]
                candidate = {
                    "text": draft,
                    "vector": qvec,
                    "grade": grade or None,
                    "subject": subject or None,
                }
                matches = dedupe.check_one(
                    candidate, load_index(store.display_name()),
                    use_guards=check_guards,
                )

            flagged = [m for m in matches
                       if m["score"] >= dedupe.REVIEW_THRESHOLD]

            if not flagged:
                st.success(
                    "**No duplicate found.** Nothing in the bank is close "
                    "enough to worry about."
                )
                if matches:
                    st.info(
                        f"{len(matches)} question(s) scored high before the "
                        "safety checks and were ruled out. They are shown "
                        "below with the reason."
                    )
            else:
                top = flagged[0]
                st.error(
                    f"**{top['verdict']}** of {top['match']['id']} at "
                    f"{top['score']:.0%}. Review this before adding."
                )

            if not matches:
                st.caption("No question in the bank scored above the threshold.")

            for m in matches:
                q = m["match"]
                colour = {
                    "LIKELY DUPLICATE": "red",
                    "NEEDS REVIEW": "orange",
                    "DISTINCT": "green",
                }[m["verdict"]]

                st.markdown(
                    f"### :{colour}[{m['verdict']}] &nbsp; "
                    f"{m['score']:.0%} similar &nbsp; - &nbsp; {q['id']}"
                )
                title_caption(q)
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
                assessment_caption(q["id"])
                if m["reasons"]:
                    st.warning(
                        "**Safety checks fired** (score lowered from "
                        f"{m['raw_score']:.0%} to {m['score']:.0%}):\n\n"
                        + "\n".join(f"- {r}" for r in m["reasons"])
                    )
                st.divider()


# ----------------------------------------------------- find duplicates

elif page == "Find duplicates":
    st.title("Find duplicate questions")

    # 0.85 was tuned on 30 sample questions. On the real bank it yields
    # about 1.5 million candidate pairs, which is neither reviewable nor
    # renderable, so a large bank starts high.
    big = len(rows) > 1000
    default_threshold = 0.95 if big else 0.85

    col1, col2 = st.columns([3, 2])
    with col1:
        use_guards = st.toggle(
            "Safety checks ON",
            value=True,
            help="Turn this off to see what happens without the guard layer.",
        )
    with col2:
        threshold = st.slider(
            "Similarity threshold", 0.70, 0.99, default_threshold, 0.01
        )

    if big and threshold < 0.92:
        st.warning(
            f"At {threshold:.2f} a bank of {len(rows):,} questions produces "
            "tens of thousands of pairs, and below 0.90 well over a "
            "million. Raise the threshold, or expect this to take a while."
        )

    with st.spinner(f"Comparing {len(rows):,} questions..."):
        with_guards = sweep(store.display_name(), threshold, True)
        without_guards = sweep(store.display_name(), threshold, False)

    # -- the comparison that matters --
    a, b, c = st.columns(3)
    a.metric("Pairs flagged, safety checks ON", f"{len(with_guards):,}")
    b.metric("Pairs flagged, safety checks OFF", f"{len(without_guards):,}")
    removed = len(without_guards) - len(with_guards)
    c.metric("Removed by safety checks", f"{removed:,}")

    if removed > 0:
        st.info(
            f"The safety checks removed **{removed:,}** pairs that look "
            "almost identical but test different things. Without them "
            "those would be shown to a teacher as duplicates."
        )

    st.divider()

    results = with_guards if use_guards else without_guards
    by_id = {r["id"]: r for r in rows}

    show_reviewed = st.checkbox(
        "Also show pairs already reviewed", value=False,
        help="Reviewed pairs are hidden so the same pair is not reviewed "
             "twice. Every verdict is on the Reviewed decisions page.")
    pending, n_reviewed, n_skipped = split_reviewed(results, show_reviewed)
    show_last_decision()
    if n_reviewed or n_skipped:
        st.caption(
            f"{n_reviewed:,} already reviewed "
            f"({'shown' if show_reviewed else 'hidden'}), "
            f"{n_skipped:,} skipped this session."
        )

    if not results:
        st.success("No possible duplicates found at this threshold.")
    elif not pending:
        st.success("Every flagged pair at this threshold has been reviewed.")

    # Rendering thousands of pairs makes the page unusable. Highest
    # scores first, so the top of the list is where a reviewer starts.
    SHOW = 50
    if len(pending) > SHOW:
        st.caption(
            f"Showing the {SHOW} highest-scoring of {len(pending):,} pairs. "
            "Raise the threshold to narrow this down."
        )

    for pair in pending[:SHOW]:
        qa, qb = by_id[pair["a"]], by_id[pair["b"]]

        colour = {
            "LIKELY DUPLICATE": "red",
            "NEEDS REVIEW": "orange",
            "DISTINCT": "green",
        }[pair["verdict"]]

        st.markdown(
            f"### :{colour}[{pair['verdict']}] &nbsp; "
            f"{pair['score']:.0%} similar"
        )

        decision_note(pair)

        left, right = st.columns(2)
        for col, q in ((left, qa), (right, qb)):
            with col:
                st.markdown(f"**{q['id']}**")
                title_caption(q)
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
                st.caption(f"by {q['contributor'] or 'unknown'}")
                assessment_caption(q["id"])

        if pair["reasons"]:
            st.warning(
                "**Safety checks fired** (score lowered from "
                f"{pair['raw_score']:.0%} to {pair['score']:.0%}):\n\n"
                + "\n".join(f"- {r}" for r in pair["reasons"])
            )

        decision_buttons(pair, qa, qb, f"{pair['a']}_{pair['b']}",
                         "; ".join(pair["reasons"]))

        st.divider()


# ------------------------------------------------ duplicates within a paper

elif page == "Duplicates within a paper":
    st.title("Duplicates within a paper")
    st.caption(
        "The same question should never appear twice on one assessment - "
        "MCQ, structured or essay. This checks each paper only against "
        "itself, not the rest of the bank, and does not use the safety "
        "checks: a paper is small enough that a false alarm costs nothing "
        "to dismiss, and a repeat here is a real problem even with "
        "different numbers plugged in - which is exactly what a safety "
        "check would otherwise excuse. The same question turning up on a "
        "DIFFERENT paper is normal and is not what this checks; see the "
        "'Also appears on assessment(s)' note on the other pages for that."
    )

    groups_all = load_assessment_groups(store.display_name())

    if not groups_all:
        st.info(
            "No assessment links with more than one comparable question "
            "were found. Either scripts/sync.py has not pulled "
            "assessment.assessment_question yet, or every synced paper "
            "has zero or one comparable question on it."
        )
    else:
        # Filters narrow WHICH questions are compared, not just which
        # results are shown - a row that fails a filter is dropped from
        # its paper's group before find_pairs ever sees it, so a group
        # left with only one question after filtering is correctly
        # skipped rather than reported as having "0 repeats".
        all_rows = [r for rs in groups_all.values() for r in rs]

        f1, f2, f3, f4 = st.columns(4)
        grade_f = f1.selectbox(
            "Grade",
            ["All"] + sorted({r["grade"] for r in all_rows if r["grade"]}),
        )
        subject_f = f2.selectbox(
            "Subject",
            ["All"] + sorted({r["subject"] for r in all_rows if r["subject"]}),
        )
        title_f = f3.text_input(
            "Title code contains", placeholder="e.g. Q3-2024"
        )
        assessment_options = sorted(
            groups_all.keys(),
            key=lambda x: (0, int(x)) if str(x).isdigit() else (1, str(x)),
        )
        assessment_f = f4.selectbox("Assessment #", ["All"] + assessment_options)

        def _matches(r):
            if grade_f != "All" and r["grade"] != grade_f:
                return False
            if subject_f != "All" and r["subject"] != subject_f:
                return False
            if title_f and title_f.lower() not in (r.get("title") or "").lower():
                return False
            return True

        groups = {}
        for aid, rs in groups_all.items():
            if assessment_f != "All" and aid != assessment_f:
                continue
            kept = [r for r in rs if _matches(r)]
            if len(kept) > 1:
                groups[aid] = kept

        st.divider()

        if not groups:
            st.warning(
                "No paper has more than one comparable question matching "
                "these filters."
            )
        else:
            group_key = (store.display_name(), tuple(
                (aid, tuple(r["id"] for r in rs))
                for aid, rs in groups.items()))
            with st.spinner(f"Checking {len(groups):,} papers..."):
                results, passage_groups = paper_repeats(group_key, groups)

            def shared_passage_group(aid, qid):
                for g in passage_groups.get(aid, []):
                    if qid in g["question_ids"]:
                        return g
                return None

            show_reviewed = st.checkbox(
                "Also show pairs already reviewed", value=False,
                help="Reviewed pairs are hidden so the same pair is not "
                     "reviewed twice. Every verdict is on the Reviewed "
                     "decisions page.")
            pending, n_reviewed, n_skipped = split_reviewed(
                results, show_reviewed)

            a, b, c = st.columns(3)
            a.metric("Papers checked", f"{len(groups):,}")
            b.metric("Repeats found", f"{len(results):,}")
            c.metric("Still to review", f"{len(results) - n_reviewed:,}")

            st.divider()
            show_last_decision()

            if not results:
                st.success("No paper repeats a question.")
            elif not pending:
                st.success("Every repeat found has been reviewed.")

            by_assessment = {aid: {r["id"]: r for r in rs}
                             for aid, rs in groups.items()}

            for pair in pending:
                aid = pair["assessment_id"]
                by_id = by_assessment[aid]
                qa, qb = by_id[pair["a"]], by_id[pair["b"]]

                st.markdown(
                    f"### :red[REPEAT ON ASSESSMENT #{aid}] &nbsp; "
                    f"{pair['score']:.0%} similar"
                )

                decision_note(pair)

                cluster = shared_passage_group(aid, pair["a"])
                if cluster:
                    nums = ", ".join(str(n) for n in cluster["question_numbers"])
                    st.info(
                        "Shares a long passage with question(s) "
                        f"{nums or 'unnumbered'} on this paper - looks like "
                        "a shared-passage question family (the same "
                        "passage or statement set, a different question "
                        "asked each time), not necessarily a real "
                        "duplicate. Check the actual questions below "
                        "before confirming."
                    )

                left, right = st.columns(2)
                for col, q in ((left, qa), (right, qb)):
                    with col:
                        # EdSider stores a sentinel (int32 max) on some
                        # rows rather than leaving question_number null -
                        # not a real position, so it is not worth showing.
                        num = q.get("question_number")
                        if num is not None and num > 100_000:
                            num = None
                        st.markdown(
                            f"**{q['id']}**"
                            + (f" (question {num} on this paper)" if num else "")
                        )
                        title_caption(q)
                        st.write(q["text"])
                        st.caption(
                            f"Grade {q['grade']} - {q['subject']} - "
                            f"{q['topic'] or 'untagged'}"
                        )

                decision_buttons(pair, qa, qb,
                                 f"paper_{aid}_{pair['a']}_{pair['b']}",
                                 "within-assessment repeat")

                st.divider()

# --------------------------------------------------- reviewed decisions

elif page == "Reviewed decisions":
    st.title("Reviewed decisions")
    st.caption(
        "Every verdict given on the Find duplicates and Duplicates within "
        "a paper pages. A reviewed pair is hidden on those pages so it is "
        "not reviewed again; undo it here to send it back."
    )

    undone = st.session_state.pop("undo_message", None)
    if undone:
        st.success(undone)

    with store.connect() as conn:
        decisions = store.list_decisions(conn)
        schema = store.table_schema(conn, "decisions")

    n_dup = sum(d["verdict"] == "duplicate" for d in decisions)
    n_distinct = sum(d["verdict"] == "distinct" for d in decisions)
    m1, m2, m3 = st.columns(3)
    m1.metric("Pairs reviewed", f"{len(decisions):,}")
    m2.metric("Duplicate", f"{n_dup:,}")
    m3.metric("Not a duplicate", f"{n_distinct:,}")

    with st.expander("How this table is built"):
        st.markdown(
            "1. **Find duplicates** or **Duplicates within a paper** shows "
            "a flagged pair.\n"
            "2. **Confirm duplicate** or **Not a duplicate** adds one row "
            "to the `decisions` table: both question ids, the score shown, "
            "the safety-check notes, the verdict, who and when.\n"
            "3. **Not a duplicate** also stores the *contrasting words* - "
            "see the next section.\n"
            "4. Every click is a new row, so a changed mind leaves a "
            "history; the newest row for a pair is its verdict, and that "
            "pair is hidden on the review pages from then on.\n"
            "5. **Skip** saves nothing. It hides the pair until the app is "
            "reopened.\n"
            "6. **Undo** below deletes the pair's rows, and it shows up "
            "for review again.\n\n"
            "`store.py` creates the table the first time the app opens a "
            "database. This is its definition, read from the database "
            "itself:"
        )
        st.code(schema or "(no decisions table yet)", language="sql")
        st.caption(
            f"The database is `{store.display_name()}`. To browse it outside "
            "the app, connect to it with pgAdmin or `psql` and read the "
            "`decisions` table there."
        )

    with st.expander("How the contrasting words are picked - try it"):
        st.markdown(
            "When a pair is marked **Not a duplicate**, the system saves "
            "the words that could explain the difference:\n\n"
            "1. Take every word of 3 or more letters from each question, "
            "in lower case (numbers and symbols are left out - the "
            "numbers check covers those).\n"
            "2. Drop common words that never tell two questions apart "
            "(listed below).\n"
            "3. Keep the words found only in the first question, and the "
            "words found only in the second.\n\n"
            "Nothing uses these automatically. Words that keep turning "
            "up across many rejected pairs - *area* vs *perimeter* - are "
            "the ones a person can later add to the safety checks' "
            "opposite-concepts list."
        )
        st.caption("Common words ignored: "
                   + ", ".join(sorted(guards._STOPWORDS)))
        t1, t2 = st.columns(2)
        try_a = t1.text_area(
            "First question",
            "Calculate the area of a rectangle 12 cm by 5 cm")
        try_b = t2.text_area(
            "Second question",
            "Calculate the perimeter of a rectangle 12 cm by 5 cm")
        only_a, only_b = guards.candidate_contrast_terms(try_a, try_b)
        t1.markdown("**Only in the first:** " + (", ".join(only_a) or "(none)"))
        t2.markdown("**Only in the second:** " + (", ".join(only_b) or "(none)"))
        known = guards.find_contrast(try_a, try_b)
        if known:
            st.info(
                f"The safety checks already know **{known[0]}** vs "
                f"**{known[1]}** - this pair would be demoted "
                "automatically.")

    st.divider()

    if not decisions:
        st.info(
            "No decisions yet. Review pairs on Find duplicates or "
            "Duplicates within a paper and they appear here.")
        st.stop()

    shown = st.radio("Show", ["All", "Duplicate", "Not a duplicate"],
                     horizontal=True)
    wanted = {"All": None, "Duplicate": "duplicate",
              "Not a duplicate": "distinct"}[shown]
    listed = [d for d in decisions if wanted is None or d["verdict"] == wanted]

    table = pd.DataFrame([{
        "Decided at": d["decided_at"],
        "Verdict": verdict_word(d["verdict"]),
        "Score": d["similarity"],
        "Question A": d["question_a"],
        "Question A text": d["text_a"],
        "Question B": d["question_b"],
        "Question B text": d["text_b"],
        "Words only in A": ", ".join(d["only_in_a"]),
        "Words only in B": ", ".join(d["only_in_b"]),
        "Safety-check notes": d["guard_flags"],
        "Decided by": d["decided_by"],
    } for d in listed])
    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={"Score": st.column_config.NumberColumn(format="%.3f")},
    )
    st.download_button(
        "Download as CSV", table.to_csv(index=False).encode("utf-8"),
        file_name="decisions.csv", mime="text/csv")

    st.subheader("Check or undo a decision")
    SHOW = 100
    if len(listed) > SHOW:
        st.caption(f"The {SHOW} most recent of {len(listed):,}.")
    for d in listed[:SHOW]:
        score = f"{d['similarity']:.0%}" if d["similarity"] is not None else "-"
        with st.expander(
                f"{verdict_word(d['verdict'])} - {d['question_a']} vs "
                f"{d['question_b']} - {score} similar"):
            left, right = st.columns(2)
            for col, qid, text, words in (
                    (left, d["question_a"], d["text_a"], d["only_in_a"]),
                    (right, d["question_b"], d["text_b"], d["only_in_b"])):
                with col:
                    st.markdown(f"**{qid}**")
                    st.write(text or "(this question is not in this bank)")
                    if d["verdict"] == "distinct":
                        st.caption("Words only in this question: "
                                   + (", ".join(words) or "(none)"))
            if d["guard_flags"]:
                st.caption(f"Safety-check notes: {d['guard_flags']}")
            st.caption(f"Decided by {d['decided_by']} at {d['decided_at']}")
            if st.button("Undo - send back for review", key=f"undo_{d['id']}"):
                with store.connect() as conn:
                    store.undo_decision(conn, d["question_a"], d["question_b"])
                st.session_state["undo_message"] = (
                    f"{d['question_a']} vs {d['question_b']} sent back for "
                    "review.")
                st.rerun()

# ---------------------------------------------------- search by meaning

elif page == "Search by meaning":
    st.title("Search by meaning")
    st.caption(
        "Finds questions by what they ask about, not by matching words."
    )

    examples = [
        "how do plants make their own food",
        "area of a rectangle",
        "when did Sri Lanka become independent",
    ]
    cols = st.columns(len(examples))
    for col, ex in zip(cols, examples):
        if col.button(ex, use_container_width=True):
            st.session_state["query"] = ex

    query = st.text_input(
        "Search", value=st.session_state.get("query", ""),
        placeholder="Type a question or a topic...",
    )

    if query:
        with st.spinner("Searching..."):
            embedder = get_embedder()
            qvec = embedder.encode(query, is_query=True)[0]
            hits = dedupe.search(qvec, load_index(store.display_name()),
                                 top_k=5)

        st.divider()
        for hit in hits:
            q = hit["question"]
            st.markdown(f"**{hit['score']:.0%} match** - {q['id']}")
            title_caption(q)
            st.write(q["text"])
            st.caption(
                f"Grade {q['grade']} - {q['subject']} - "
                f"{q['topic'] or 'untagged'}"
            )
            st.divider()


# --------------------------------------------------------------- paper intake

elif page == "Paper intake":
    st.title("Paper intake")
    st.caption(
        "Upload a question paper - PDF, Word, or an image of a handwritten "
        "one. Its questions are pulled out, you correct them, then the batch "
        "is checked for repeats against itself before any of it joins the "
        "bank. Extraction is local and rough, handwriting especially, so "
        "every question needs a look - nothing here is trusted until you "
        "have confirmed it."
    )

    up = st.file_uploader(
        "Question paper",
        type=["pdf", "docx", "doc", "png", "jpg", "jpeg", "tif", "tiff", "bmp"],
    )
    up_lang = st.radio(
        "Language of the paper",
        list(intake_extract.OCR_LANGS),
        format_func=lambda k: intake_extract.OCR_LANGS[k],
        horizontal=True,
        help="Used to read scanned pages and questions pasted in as "
             "pictures. Typed text is read directly whatever this says.",
    )
    style_keys = list(intake_extract.SPLIT_STYLES)
    up_style = st.radio(
        "Where does a new question start in this paper?",
        style_keys,
        format_func=lambda k: intake_extract.SPLIT_STYLES[k],
        horizontal=True,
        help="Most papers number their questions. Pick 'Question' for a "
             "worked-solutions or marking-scheme document, where the "
             "numbered lists are answer steps rather than questions.",
    )
    if up is not None and st.button("Extract questions"):
        suffix = Path(up.name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(up.getbuffer())
            tmp_path = tmp.name
        try:
            with st.spinner("Reading the paper - scanned pages and pictures "
                            "are OCR'd, which can take a minute..."):
                # The real name, not the temp file's - the paper's grade,
                # subject and title fall back to it ("GIT_Gr12_FirstTT").
                result = intake_extract.extract(tmp_path, style=up_style,
                                                lang=up_lang,
                                                filename=up.name)
        finally:
            os.unlink(tmp_path)

        with store.connect() as conn:
            doc_id = store.create_intake_document(
                conn, up.name, file_type=result["file_type"],
                extractor=result["extractor"],
                paper_grade=result["paper"]["grade"],
                paper_subject=result["paper"]["subject"],
                paper_title=result["paper"]["title"],
                sections=result["sections"],
                raw_text=result["raw_text"], split_style=up_style,
                # The paper itself, kept so a reviewer can check a
                # suspect OCR read against the original page instead of
                # trusting the extracted text blind.
                file_blob=bytes(up.getbuffer()),
            )
            store.add_extracted_questions(
                conn, doc_id, result["questions"],
                fallback_grade=result["paper"]["grade"],
                fallback_subject=result["paper"]["subject"],
            )
        _load_intake_docs.clear()
        parts = ", ".join(f"{s['label']} ({s['questions']})"
                          for s in result["sections"] if s["label"])
        st.success(
            f"Extracted {len(result['questions'])} question(s) from "
            f"{up.name} via {result['extractor']}"
            + (f" - {parts}" if parts else "")
            + ". Select it below to review."
        )
        st.rerun()

    docs = _load_intake_docs(store.display_name())

    if not docs:
        st.info("No papers uploaded yet.")
        st.stop()

    labels = {d["id"]: f"#{d['id']}  -  {d['filename']}  ({d['status']})"
              for d in docs}
    doc_id = st.selectbox(
        "Uploaded papers", [d["id"] for d in docs],
        format_func=lambda i: labels[i],
    )
    doc = next(d for d in docs if d["id"] == doc_id)
    st.caption(f"Extractor: {doc['extractor']}  -  uploaded {doc['uploaded_at']}")
    if intake_extract.was_incomplete(doc["extractor"] or ""):
        st.warning(
            "Part of this paper could not be read, so questions are "
            "probably missing below - see the extractor note above. Fix "
            "the cause and upload it again, or add the missing questions "
            "by hand."
        )

    drafts = _load_intake_drafts(doc_id, store.display_name())
    images_by_q = _load_intake_images(doc_id, store.display_name())

    # -- re-split, without needing the original file again --
    if doc.get("raw_text"):
        with st.expander("Split it differently, or see the raw text read"):
            st.caption(
                "The paper's text is kept, so it can be re-split without "
                "uploading it again. Re-splitting REPLACES the questions "
                "below, including any edits already made to them."
            )
            cur_style = doc.get("split_style") or \
                intake_extract.DEFAULT_SPLIT_STYLE
            keys = list(intake_extract.SPLIT_STYLES)
            re_style = st.radio(
                "Where does a new question start?", keys,
                index=keys.index(cur_style) if cur_style in keys else 0,
                format_func=lambda k: intake_extract.SPLIT_STYLES[k],
                key="resplit_style",
            )
            parsed = intake_extract.structure(doc["raw_text"], style=re_style)
            preview = parsed["questions"]
            parts = ", ".join(f"{s['label']}: {s['questions']}"
                              for s in parsed["sections"] if s["label"])
            st.write(
                f"That style finds **{len(preview)}** question(s)"
                + (f" ({parts})" if parts else "")
                + f". Currently stored: **{len(drafts)}**."
            )
            if st.button("Re-split this paper", type="primary"):
                meta = intake_extract.detect_paper_meta(
                    doc["raw_text"], filename=doc["filename"])
                # Papers read before grades and subjects came back in the
                # bank's own names hold "13" or "Combined Mathematics",
                # which the lists below cannot show - take the fresh
                # reading for those, keep a reviewer's own choice otherwise.
                grade = (doc["paper_grade"]
                         if intake_extract.is_grade_code(doc["paper_grade"])
                         else meta["grade"] or doc["paper_grade"])
                subject = (doc["paper_subject"]
                           if doc["paper_subject"] in intake_extract.KNOWN_SUBJECTS
                           else meta["subject"] or doc["paper_subject"])
                with store.connect() as conn:
                    store.replace_intake_questions(
                        conn, doc_id, preview,
                        fallback_grade=grade, fallback_subject=subject,
                    )
                    store.update_intake_document(
                        conn, doc_id, split_style=re_style, status="extracted",
                        sections=parsed["sections"], paper_grade=grade,
                        paper_subject=subject,
                        paper_title=doc.get("paper_title") or meta["title"],
                    )
                _load_intake_docs.clear()
                _load_intake_drafts.clear()
                _load_intake_images.clear()
                st.rerun()

            st.text_area(
                "Raw text the extractor read", value=doc["raw_text"],
                height=220, disabled=True, key=f"rawtext_{doc_id}",
            )
    else:
        st.info(
            "This paper was uploaded before the raw text was kept, so it "
            "cannot be re-split here - upload it again to pick up the "
            "current extractor."
        )

    # -- paper-level labels: one set for the whole paper. Grade, subject
    # and title are the same for every question on a paper; only the
    # marks differ, and those are set per question below. --
    sections = store.intake_sections(doc)
    title_bits = [doc.get("paper_subject"),
                  intake_extract.grade_label(doc.get("paper_grade")),
                  doc.get("paper_title")]
    st.header(" - ".join(b for b in title_bits if b) or doc["filename"])

    st.subheader("Paper details")
    # The bank's own grades and subjects, plus whatever this paper holds -
    # a value missing from its list would otherwise show as blank.
    grade_opts = sorted({r["grade"] for r in rows if r["grade"]}
                        | ({doc["paper_grade"]} if doc["paper_grade"] else set()))
    subject_opts = sorted({r["subject"] for r in rows if r["subject"]}
                          | ({doc["paper_subject"]} if doc["paper_subject"]
                             else set()))

    pc1, pc2, pc3, pc4 = st.columns([2, 2, 3, 1])
    pg = pc1.selectbox(
        "Grade", [""] + grade_opts,
        index=([""] + grade_opts).index(doc["paper_grade"] or ""),
        format_func=lambda g: intake_extract.grade_label(g) or "-",
    )
    ps = pc2.selectbox(
        "Subject", [""] + subject_opts,
        index=([""] + subject_opts).index(doc["paper_subject"] or ""),
        format_func=lambda s: s or "-",
    )
    ptitle = pc3.text_input("Title", value=doc.get("paper_title") or "")
    pc4.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
    if pc4.button("Apply to all", use_container_width=True):
        with store.connect() as conn:
            store.update_intake_document(
                conn, doc_id, paper_grade=pg or None, paper_subject=ps or None,
                paper_title=ptitle.strip() or None,
            )
            for d in drafts:
                store.update_intake_question(
                    conn, d["id"], grade=pg or None, subject=ps or None,
                )
        _load_intake_docs.clear()
        _load_intake_drafts.clear()
        st.rerun()

    st.divider()
    st.subheader(f"Questions ({len(drafts)})")

    low = [d for d in drafts if d["extraction_confidence"] == "low"]
    if low:
        st.warning(
            f"{len(low)} of {len(drafts)} came out low-confidence - almost no "
            "text, or no marks found. They are marked below."
        )
    if len(drafts) == 1 and drafts[0]["question_number"] is None:
        st.error(
            "This paper did not split into separate questions - it came back "
            "as one block. Correct the text below and use 'Add a question' to "
            "enter each one, or re-upload a clearer copy."
        )

    st.caption(
        "Grade and subject come from Paper details above. Set the marks per "
        "question, fix the text, and tick Reviewed once a question is correct."
    )

    # Parts in paper order, questions in order within each part - so a
    # question added by hand to Part I shows under Part I, not at the end.
    part_order = {s["label"]: n for n, s in enumerate(sections)}
    by_seq = {d["seq"]: d for d in drafts}

    def tree_path(d):
        """A draft's position as the seq of each question above it, so a
        part added by hand sorts under its own question rather than at
        the end of the paper (the extractor's own parts already follow
        their question by seq, but a later one has a higher seq)."""
        keys, cur = [], d
        while cur is not None and len(keys) <= intake_extract.MAX_SUB_DEPTH + 1:
            keys.append(cur["seq"])
            cur = by_seq.get(cur.get("parent_seq"))
        return tuple(reversed(keys))

    drafts.sort(key=lambda d: (part_order.get(d.get("section"), len(part_order)),
                               tree_path(d)))
    by_label = {s["label"]: s for s in sections}
    # Which drafts are a question WITH parts - their own text is the
    # shared stem rather than a question to answer on its own.
    parents_of = {d["parent_seq"] for d in drafts if d.get("parent_seq")}
    shown = None
    # The paper itself sits alongside the editable cards, not above or
    # below them, so a reviewer can check a suspect OCR read against the
    # original page without losing their place in the list.
    cards_col, preview_col = st.columns([3, 2])
    with preview_col:
        st.subheader("Original paper")
        render_paper_preview(doc, doc_id)
    with cards_col:
        for i, d in enumerate(drafts):
            if d.get("section") and d["section"] != shown:
                shown = d["section"]
                part = by_label.get(shown, {})
                st.header(shown)
                here = [x for x in drafts if x.get("section") == shown]
                n_questions = sum(1 for x in here if not (x.get("depth") or 0))
                n_parts = len(here) - n_questions
                about = [
                    part.get("heading") if part.get("heading") != shown else None,
                    intake_extract.KIND_LABELS.get(part.get("kind")),
                    f"{n_questions} question(s)",
                    f"{n_parts} part(s)" if n_parts else None,
                    f"{part['marks_each']} marks each"
                    if part.get("marks_each") else None,
                ]
                st.caption("  |  ".join(a for a in about if a))
                if part.get("instructions"):
                    with st.expander("Instructions for this part"):
                        st.text(part["instructions"])

            # A question's parts sit indented under it, each with its own
            # text, marks and answer - that is how the paper marks them,
            # and how EdSider stores them (a parent row plus child rows).
            # Streamlit allows columns nested only one level deep, and
            # the preview pane (cards_col/preview_col) already spends
            # that one level - so the indent spacer below is the ONLY
            # st.columns() call allowed anywhere in this loop; label/
            # Remove and marks/Reviewed, which used to sit in their own
            # column pairs, are stacked instead rather than nested again.
            depth = d.get("depth") or 0
            if depth:
                _, body = st.columns([0.06 * depth, 1])
                card = body.container(border=True)
            else:
                card = st.container(border=True)

            with card:
                if depth:
                    label = f"Part {d['question_number']}"
                else:
                    label = (f"Question {d['question_number']}"
                             if d["question_number"]
                             else f"Question {i + 1} (no number found)")
                if d["extraction_confidence"] == "low":
                    label += "  -  low confidence"
                st.markdown(f"**{label}**")

                if d["seq"] in parents_of:
                    st.caption(
                        "Shared text for the parts below - the scenario or "
                        "diagram they all refer to. Each part is answered "
                        "separately."
                    )
                st_quill(
                    value=text_to_quill_html(d["text"]), html=True,
                    toolbar=RICH_TOOLBAR, key=f"iq_{d['id']}_text",
                    placeholder="No shared text - the parts below are the question"
                    if d["seq"] in parents_of else "",
                )
                st.number_input(
                    "Marks", min_value=0, max_value=100, step=1,
                    value=int(d["marks"]) if d["marks"] else 0,
                    key=f"iq_{d['id']}_marks",
                )
                st.checkbox(
                    "Reviewed", value=bool(d["reviewed"]),
                    key=f"iq_{d['id']}_reviewed",
                )
                with st.expander("Answer" if d.get("answer")
                                 else "Answer (none found in the paper)"):
                    st_quill(
                        value=text_to_quill_html(d.get("answer")), html=True,
                        toolbar=RICH_TOOLBAR, key=f"iq_{d['id']}_answer",
                    )

                # Some content is fundamentally a picture - a diagram, a
                # photograph, a table that reads badly as flattened text
                # - that extraction cannot turn into meaningful text on
                # its own. This lets a reviewer attach one to the
                # question itself, its answer, or (for an MCQ) one
                # specific option - options are never their own row, so
                # an option's own printed label is what an image for it
                # is keyed on. See store.intake_question_images.
                existing_images = images_by_q.get(d["id"], [])
                img_label = (f"Images ({len(existing_images)})"
                            if existing_images else "Images")
                with st.expander(img_label):
                    slot_names = {"question": "Question", "answer": "Answer"}
                    for img in existing_images:
                        name = slot_names.get(
                            img["slot"], img["slot"].replace("option:", "Option "))
                        st.caption(name)
                        blob = _question_image_blob(img["id"], store.display_name())
                        if blob:
                            st.image(blob)
                        if st.button("Remove image",
                                    key=f"iq_{d['id']}_img_{img['id']}_rm"):
                            with store.connect() as conn:
                                store.delete_question_image(conn, img["id"])
                            _load_intake_images.clear()
                            st.rerun()
                        st.divider()

                    _, mcq_options = ((d["text"] or "", []) if depth
                                      else intake_extract.split_mcq_options(
                                          d["text"] or ""))
                    if mcq_options:
                        option_slots = [f"option:{o['label']}"
                                        for o in mcq_options]
                    else:
                        # A picture-only MCQ - each option is a photo
                        # next to a bare "I"/"II" label with no period
                        # or bracket the extractor can split on - leaves
                        # split_mcq_options() with nothing to find. A
                        # reviewer who can see the real options in the
                        # paper preview still needs somewhere to attach
                        # an image per option, so this offers a plain,
                        # adjustable set of slots instead of leaving
                        # them with only "Question" and "Answer".
                        n = st.number_input(
                            "This question's options were not split out "
                            "automatically - how many does it have?",
                            min_value=0, max_value=8, value=0, step=1,
                            key=f"iq_{d['id']}_img_option_count",
                        )
                        option_slots = [f"option:{i + 1}" for i in range(int(n))]
                    slot_choices = ["question", "answer"] + option_slots
                    st.selectbox(
                        "Attach a new image to", slot_choices,
                        format_func=lambda s: slot_names.get(
                            s, s.replace("option:", "Option ")),
                        key=f"iq_{d['id']}_img_slot",
                    )
                    upload = st.file_uploader(
                        "Image file", type=["png", "jpg", "jpeg"],
                        key=f"iq_{d['id']}_img_upload",
                    )
                    if upload is not None and st.button(
                            "Attach image", key=f"iq_{d['id']}_img_attach"):
                        with store.connect() as conn:
                            store.add_question_image(
                                conn, d["id"],
                                st.session_state[f"iq_{d['id']}_img_slot"],
                                upload.getvalue(), content_type=upload.type,
                            )
                        _load_intake_images.clear()
                        st.rerun()

                if st.button("Remove", key=f"iq_{d['id']}_rm"):
                    with store.connect() as conn:
                        store.delete_intake_question(conn, d["id"])
                    _load_intake_drafts.clear()
                    _load_intake_images.clear()
                    st.rerun()

    if st.button("Save all questions", type="primary"):
        with store.connect() as conn:
            for d in drafts:
                m = st.session_state[f"iq_{d['id']}_marks"]
                store.update_intake_question(
                    conn, d["id"],
                    text=quill_to_text(st.session_state[f"iq_{d['id']}_text"]),
                    marks=int(m) if m else None,
                    answer=quill_to_text(
                        st.session_state[f"iq_{d['id']}_answer"]) or None,
                    reviewed=1 if st.session_state[f"iq_{d['id']}_reviewed"]
                    else 0,
                )
        _load_intake_drafts.clear()
        st.success("Saved.")
        st.rerun()

    with st.expander("Add a question the extractor missed"):
        part_labels = [s["label"] for s in sections if s["label"]]
        if part_labels:
            st.selectbox("Part", part_labels, key="iq_new_section")

        # A part of an existing question, not only a question of its own -
        # the same nesting the extractor produces, for a part it missed.
        def parent_label(seq):
            if seq is None:
                return "A question on its own"
            p = by_seq[seq]
            name = (f"part {p['question_number']}" if p.get("depth")
                    else f"question {p['question_number'] or p['seq']}")
            return f"A part of {name}"

        st.selectbox(
            "Add it as", [None] + [d["seq"] for d in drafts
                                   if (d.get("depth") or 0)
                                   < intake_extract.MAX_SUB_DEPTH],
            format_func=parent_label, key="iq_new_parent",
            help="A part gets its own text, marks and answer, indented "
                 "under the question it belongs to.")
        st.text_input("Number (optional)", key="iq_new_number")
        st.text_area("Question text", key="iq_new_text", height=120)
        st.number_input("Marks", min_value=0, max_value=100, step=1, value=0,
                        key="iq_new_marks")
        if st.button("Add question"):
            new_text = st.session_state["iq_new_text"].strip()
            if not new_text:
                st.warning("Enter the question text first.")
            else:
                parent = by_seq.get(st.session_state.get("iq_new_parent"))
                number = st.session_state["iq_new_number"].strip() or None
                if parent and not number:
                    # Name it the way the paper would: the question's own
                    # number and the next letter after its existing parts.
                    siblings = sum(1 for d in drafts
                                   if d.get("parent_seq") == parent["seq"])
                    number = (f"{parent['question_number'] or ''}"
                              f"({chr(ord('a') + siblings)})")
                with store.connect() as conn:
                    seq = max((d["seq"] for d in drafts), default=0) + 1
                    m = st.session_state["iq_new_marks"]
                    store.add_intake_question(
                        conn, doc_id, seq, new_text,
                        question_number=number,
                        grade=doc["paper_grade"], subject=doc["paper_subject"],
                        marks=int(m) if m else None,
                        section=(parent["section"] if parent else
                                 (st.session_state["iq_new_section"]
                                  if part_labels else None)),
                        parent_seq=parent["seq"] if parent else None,
                        depth=((parent.get("depth") or 0) + 1) if parent else 0,
                    )
                _load_intake_drafts.clear()
                st.rerun()

    st.divider()
    st.subheader("Duplicate check within this paper")
    st.caption(
        "Same rule as 'Duplicates within a paper' for the existing bank - the "
        "same question must not appear twice on one paper. Runs only once "
        "every question is marked reviewed, because it is meaningless on text "
        "that has not been corrected yet."
    )

    all_reviewed = bool(drafts) and all(d["reviewed"] for d in drafts)
    if not all_reviewed:
        pending = sum(1 for d in drafts if not d["reviewed"])
        st.info(f"{pending} question(s) still not marked reviewed.")

    if st.button("Check for duplicates", disabled=not all_reviewed):
        with store.connect() as conn:
            everything = [dict(q) for q in store.intake_questions(conn, doc_id)]
        # A question WITH parts holds only their shared stem ("Consider
        # the following scenario: ..."), and the parts are the questions
        # actually asked. Comparing a stem against the bank finds nothing
        # useful and buries the real comparisons.
        stems = {d["parent_seq"] for d in everything if d.get("parent_seq")}
        fresh = [d for d in everything
                 if d["seq"] not in stems and (d["text"] or "").strip()]
        with st.spinner("Embedding and comparing..."):
            out = intake_review.check_batch(fresh, get_embedder())
        with store.connect() as conn:
            store.update_intake_document(conn, doc_id, status="checked")
        _load_intake_docs.clear()

        by_id = {d["id"]: d for d in fresh}
        if not out["exact_groups"] and not out["similar_pairs"]:
            st.success("No repeats found in this paper.")

        def qname(q):
            # Part I and Part II both have a question 1.
            num = f"Q{q['question_number'] or q['id']}"
            return f"{q['section']} {num}" if q["section"] else num

        for grp in out["exact_groups"]:
            nums = ", ".join(qname(by_id[i]) for i in grp)
            st.error(f"Exact duplicate - questions {nums}")
            for i in grp:
                st.write(f"- {by_id[i]['text']}")
            st.divider()

        for pair in out["similar_pairs"]:
            qa, qb = by_id[pair["a"]], by_id[pair["b"]]
            colour = {
                "LIKELY DUPLICATE": "red",
                "NEEDS REVIEW": "orange",
                "DISTINCT": "green",
            }[pair["verdict"]]
            st.markdown(
                f"### :{colour}[{pair['verdict']}] &nbsp; "
                f"{pair['score']:.0%} similar"
            )
            left, right = st.columns(2)
            left.markdown(f"**{qname(qa)}**\n\n{qa['text']}")
            right.markdown(f"**{qname(qb)}**\n\n{qb['text']}")
            if pair["reasons"]:
                st.warning(
                    "**Safety checks fired** (score lowered from "
                    f"{pair['raw_score']:.0%} to {pair['score']:.0%}):\n\n"
                    + "\n".join(f"- {r}" for r in pair["reasons"])
                )
            st.divider()

    st.divider()
    st.subheader("Review status")
    st.caption(
        "Data-entry's own job stops at the duplicate check above. Assigning "
        "a checked paper to a teacher is a separate person's job, in the "
        "paper-assignment app - not a button on this page."
    )
    if doc["status"] == "checked":
        st.info("Ready - waiting for the assigning administrator to hand "
               "this to a teacher.")
    elif doc["status"] == "teacher_review":
        st.info(f"With {doc['assigned_to'] or 'a teacher'} for review.")
    elif doc["status"] == "refer_back":
        st.warning(
            "The teacher sent this back with comments - see them on each "
            "question above, fix what was flagged, then run the duplicate "
            "check again to return this to the assignment queue."
        )
    elif doc["status"] == "review_done":
        st.success(
            "The teacher has signed off - this paper is promoted into the "
            "real bank automatically the moment they finish, so seeing "
            "this for more than a moment is worth a second look."
        )
    elif doc["status"] == "published":
        st.success("Promoted into the real bank.")
    else:
        st.caption("Run the duplicate check above first.")

# --------------------------------------------------------- question bank

else:
    st.title("Question bank")

    df = pd.DataFrame([
        {
            "ID": r["id"],
            "Title": r["title"],
            "Question": r["text"],
            "Grade": r["grade"],
            "Subject": r["subject"],
            "Topic": r["topic"],
            "Marks": r["marks"],
            "Contributor": r["contributor"],
            "Status": r["status"],
        }
        for r in rows
    ])

    c1, c2 = st.columns(2)
    subjects = c1.multiselect(
        "Subject", sorted(df["Subject"].dropna().unique())
    )
    grades = c2.multiselect(
        "Grade", sorted(df["Grade"].dropna().unique())
    )

    view = df
    if subjects:
        view = view[view["Subject"].isin(subjects)]
    if grades:
        view = view[view["Grade"].isin(grades)]

    st.caption(f"Showing {len(view)} of {len(df)} questions")
    st.dataframe(view, use_container_width=True, hide_index=True)
