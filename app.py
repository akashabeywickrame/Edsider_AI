"""EdSider AI - duplicate detection and semantic search.

Run with:   streamlit run app.py
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.embeddings import DEFAULT_MODEL
from src.normalize import content_hash

st.set_page_config(page_title="EdSider AI", layout="wide")


# ---------------------------------------------------------------- data

# Keyed on the database name so switching banks invalidates the cache
# rather than showing the previous one's questions.
@st.cache_data(ttl=5)
def load_rows(db_name):
    with store.connect() as conn:
        rows = store.comparable_questions(conn, DEFAULT_MODEL)
        total = store.count(conn)
    return [dict(r) for r in rows], total


@st.cache_resource
def get_embedder():
    from src.embeddings import Embedder
    return Embedder()


@st.cache_data(ttl=300)
def sweep(db_name, threshold, use_guards):
    """A full sweep is expensive on a real bank - cache it per setting."""
    rows, _ = load_rows(db_name)
    return dedupe.find_pairs(rows, use_guards=use_guards, threshold=threshold)


# ------------------------------------------------------------- sidebar

st.sidebar.title("EdSider AI")

# Which bank is open has to be visible and switchable. Relying on
# EDSIDER_DB alone means launching from a shell that never set it opens
# the 30-question sample and silently shows its planted duplicates.
banks = sorted(p.name for p in (ROOT / "data").glob("*.db"))
if banks:
    current = store.DB_PATH.name
    chosen = st.sidebar.selectbox(
        "Question bank", banks,
        index=banks.index(current) if current in banks else 0,
    )
    if chosen != store.DB_PATH.name:
        store.use_database(ROOT / "data" / chosen)
        st.rerun()

page = st.sidebar.radio(
    "View",
    ["Check a new question", "Find duplicates",
     "Search by meaning", "Question bank"],
)

rows, total_questions = load_rows(store.DB_PATH.name)

st.sidebar.divider()
st.sidebar.caption(f"Database: {store.DB_PATH.name}")
st.sidebar.caption(f"{total_questions:,} questions loaded")
st.sidebar.caption(f"{len(rows):,} comparable, with fingerprints")
st.sidebar.caption(f"Model: {DEFAULT_MODEL.split('/')[-1]}")

if not rows:
    st.title("EdSider AI")
    st.error(
        f"**{store.DB_PATH.name}** holds {total_questions:,} questions but "
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
                st.write(r["text"])
                st.caption(
                    f"Grade {r['grade']} - {r['subject']} - "
                    f"{r['topic'] or 'untagged'}"
                )
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
                    candidate, rows, use_guards=check_guards
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
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
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
        with_guards = sweep(store.DB_PATH.name, threshold, True)
        without_guards = sweep(store.DB_PATH.name, threshold, False)

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

    if not results:
        st.success("No possible duplicates found at this threshold.")

    # Rendering thousands of pairs makes the page unusable. Highest
    # scores first, so the top of the list is where a reviewer starts.
    SHOW = 50
    if len(results) > SHOW:
        st.caption(
            f"Showing the {SHOW} highest-scoring of {len(results):,} pairs. "
            "Raise the threshold to narrow this down."
        )

    for pair in results[:SHOW]:
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
        for col, q in ((left, qa), (right, qb)):
            with col:
                st.markdown(f"**{q['id']}**")
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
                st.caption(f"by {q['contributor'] or 'unknown'}")

        if pair["reasons"]:
            st.warning(
                "**Safety checks fired** (score lowered from "
                f"{pair['raw_score']:.0%} to {pair['score']:.0%}):\n\n"
                + "\n".join(f"- {r}" for r in pair["reasons"])
            )

        d1, d2, d3, _ = st.columns([1, 1, 1, 3])
        key = f"{pair['a']}_{pair['b']}"
        if d1.button("Confirm duplicate", key=f"dup_{key}"):
            with store.connect() as conn:
                store.record_decision(
                    conn, pair["a"], pair["b"], "duplicate", "reviewer",
                    pair["score"], "; ".join(pair["reasons"]),
                )
            st.success("Recorded as duplicate.")
        if d2.button("Not a duplicate", key=f"not_{key}"):
            with store.connect() as conn:
                store.record_decision(
                    conn, pair["a"], pair["b"], "distinct", "reviewer",
                    pair["score"], "; ".join(pair["reasons"]),
                )
            st.success("Recorded as distinct.")
        d3.button("Skip", key=f"skip_{key}")

        st.divider()


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
            hits = dedupe.search(qvec, rows, top_k=5)

        st.divider()
        for hit in hits:
            q = hit["question"]
            st.markdown(f"**{hit['score']:.0%} match** - {q['id']}")
            st.write(q["text"])
            st.caption(
                f"Grade {q['grade']} - {q['subject']} - "
                f"{q['topic'] or 'untagged'}"
            )
            st.divider()


# --------------------------------------------------------- question bank

else:
    st.title("Question bank")

    df = pd.DataFrame([
        {
            "ID": r["id"],
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
