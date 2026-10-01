"""Teacher review - a separate, deliberately small app.

Run with:   streamlit run teacher_app.py

This is NOT a page inside app.py, on purpose: a teacher opening this has
nothing in common with a data-entry person's tool - no rich-text editor,
no marks-configuration controls, no sidebar of other pages. Just their
own assigned papers, each question laid out the way EdSider's own real
paper-review screen already shows it (the question, its marks, its
answer options or expected answer), a tick, and a place to say what is
wrong. Running it as its own script means a data-entry person opening
app.py never sees this and a teacher opening this never sees app.py's
busy review tools - not just a hidden sidebar entry, a page that
genuinely is not there.

A paper only shows up here once the assigning administrator (see
masd_app.py) has actually handed it to this signed-in identity -
store.documents_assigned_to() is the whole access rule, no separate
permission system needed.
"""

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store
from src.intake import extract as intake_extract

st.set_page_config(page_title="Teacher review", layout="wide")


@st.cache_resource(ttl=60, show_spinner="Connecting to the database...")
def database_problem(db_name):
    """None when the database answers, else why not. A bare
    store.connect() with no `with` would leak the connection it checks
    out - see app.py's own copy of this same check."""
    try:
        with store.connect():
            return None
    except Exception as unreachable:  # noqa: BLE001 - shown to the reviewer
        return str(unreachable)


problem = database_problem(store.display_name())
if problem:
    database_problem.clear()
    st.title("Teacher review")
    st.error(f"Cannot reach the database named by `EDSIDER_DB`:\n\n```\n{problem}\n```")
    st.stop()


def current_teacher():
    """Who is signed in. Azure App Service's Easy Auth injects this
    header once configured; falls back to a fixed name locally, the
    same reasoning app.py's own current_reviewer() uses - this is a
    small enough function that duplicating it beats sharing a module
    between two otherwise-independent apps for one three-line helper."""
    name = st.context.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    return name or "teacher"


def _set_teacher_reviewed(q_id):
    """A checkbox's on_change - saves the moment it is ticked, rather
    than needing a separate 'Save' step on a page meant to be simple."""
    with store.connect() as conn:
        store.update_intake_question(
            conn, q_id,
            teacher_reviewed=1 if st.session_state[f"tq_{q_id}_reviewed"] else 0,
        )


@st.cache_data(show_spinner=False)
def _question_image_blob(image_id, db_name):
    """One attached image's own bytes - cached per (image, database),
    same reasoning as app.py's own copy of this: a rerun should not
    re-fetch every attached image on the page just because one
    checkbox changed."""
    with store.connect() as conn:
        return store.question_image_blob(conn, image_id)


_STATUS_LABELS = {
    "teacher_review": "Awaiting your review",
    "refer_back": "Sent back for changes",
    "review_done": "Review done",
    "published": "Published",
}

st.title("Teacher review")
st.caption(
    "Every question on one paper, laid out the way you already check it "
    "in EdSider - the question, its marks, and its answer options (or "
    "the expected answer) - with a place to flag anything wrong."
)

identity = current_teacher()
with store.connect() as conn:
    # First visit is what puts this identity in the assigning
    # administrator's dropdown at all - see masd_app.py.
    store.upsert_teacher(conn, identity)
    docs = [dict(d) for d in store.documents_assigned_to(conn, identity)]

if not docs:
    st.info("No papers have been assigned to you yet.")
    st.stop()

labels = {d["id"]: f"#{d['id']}  -  {d['filename']}  "
                   f"({_STATUS_LABELS.get(d['status'], d['status'])})"
          for d in docs}
doc_id = st.selectbox(
    "Your papers", [d["id"] for d in docs],
    format_func=lambda i: labels[i],
)
doc = next(d for d in docs if d["id"] == doc_id)

with store.connect() as conn:
    drafts = [dict(q) for q in store.intake_questions(conn, doc_id)]
    comments_by_q = store.question_comments_for_document(conn, doc_id)
    images_by_q = store.question_images_for_document(conn, doc_id)

if not drafts:
    st.info("This paper has no questions.")
    st.stop()

locked = doc["status"] != "teacher_review"
if locked:
    st.info(
        f"This paper's review is finished "
        f"({_STATUS_LABELS.get(doc['status'], doc['status'])}) - shown "
        "read-only below."
    )

for d in drafts:
    depth = d.get("depth") or 0
    label = (f"Part {d['question_number']}" if depth
             else f"Q{d['question_number'] or d['seq']}")
    card = st.container(border=True)
    with card:
        head_l, head_r = st.columns([4, 1])
        head_l.markdown(f"**{label}**")
        head_r.markdown(f"{d['marks'] or 0} Marks")

        # Every image data-entry attached to this question - the
        # question itself, its answer, or one specific MCQ option - see
        # store.intake_question_images. A teacher's view is read-only
        # here: no attach/remove controls, just the pictures themselves,
        # since judging whether a picture-based question or option is
        # right is the whole reason this needs to be visible at all.
        images_by_slot = {}
        for img in images_by_q.get(d["id"], []):
            images_by_slot.setdefault(img["slot"], []).append(img)

        def _show_images(slot):
            for img in images_by_slot.get(slot, []):
                blob = _question_image_blob(img["id"], store.display_name())
                if blob:
                    st.image(blob)

        # MCQ options, if this question's text has them, separated from
        # the stem so they are not shown twice - never true for a part
        # (depth > 0), since an option is never itself split into parts.
        # See split_mcq_options().
        stem, options = ((d["text"] or "", []) if depth
                         else intake_extract.split_mcq_options(d["text"] or ""))

        st.markdown("**Question**")
        st.write(stem or "*(no text)*")
        _show_images("question")

        # A picture-only MCQ - each option is a photo next to a bare
        # roman numeral with no period or bracket to split on - leaves
        # split_mcq_options() with nothing to find (see app.py's own
        # fallback for attaching an image to it anyway). The option
        # still needs to show here if data-entry attached a picture to
        # it, even with no text split_mcq_options() could recover.
        option_labels = [o["label"] for o in options]
        option_text = {o["label"]: o["text"] for o in options}
        for slot in images_by_slot:
            if slot.startswith("option:"):
                extra = slot.split(":", 1)[1]
                if extra not in option_labels:
                    option_labels.append(extra)

        answer_images = images_by_slot.get("answer", [])
        if option_labels:
            st.markdown("**Answer Options**")
            correct = intake_extract.mcq_correct_label(d.get("answer"))
            rows = [option_labels[i:i + 2]
                   for i in range(0, len(option_labels), 2)]
            for row in rows:
                cols = st.columns(2)
                for col, opt_label in zip(cols, row):
                    is_correct = opt_label.lower() == correct
                    with col.container(border=True):
                        text = option_text.get(opt_label)
                        st.write(f"**{opt_label}.** {text}" if text
                                else f"**{opt_label}.**")
                        _show_images(f"option:{opt_label}")
                        if is_correct:
                            st.caption(":green[Correct]")
            if answer_images:
                st.markdown("**Marking Guidance**")
                _show_images("answer")
        elif d.get("answer") or answer_images:
            st.markdown("**Marking Guidance**")
            if d.get("answer"):
                st.write(d["answer"])
            _show_images("answer")
        else:
            st.caption("No answer recorded for this question yet.")

        existing = comments_by_q.get(d["id"], [])
        if existing:
            st.markdown("**Comments**")
            for c in existing:
                st.caption(f"{c['author'] or 'reviewer'} - {c['created_at']}")
                st.write(c["comment"])

        st.checkbox(
            "Marked as Reviewed", value=bool(d["teacher_reviewed"]),
            key=f"tq_{d['id']}_reviewed", disabled=locked,
            on_change=_set_teacher_reviewed, args=(d["id"],),
        )

        with st.expander("Add comment"):
            st.text_area("Comment", key=f"tq_{d['id']}_comment",
                         label_visibility="collapsed",
                         disabled=locked,
                         placeholder="Is this question or its answer "
                                     "wrong? Say what needs fixing.")
            if st.button("Add comment", key=f"tq_{d['id']}_comment_btn",
                        disabled=locked):
                text = st.session_state[f"tq_{d['id']}_comment"].strip()
                if text:
                    with store.connect() as conn:
                        store.add_question_comment(
                            conn, d["id"], identity, text)
                    st.rerun()

st.divider()
reviewed_count = sum(1 for d in drafts if d["teacher_reviewed"])
st.caption(f"{reviewed_count} of {len(drafts)} question(s) marked reviewed.")

if locked:
    st.stop()

all_reviewed = reviewed_count == len(drafts)
if not all_reviewed:
    st.info("Mark every question reviewed before finishing.")

finishing_key = f"finishing_review_{doc_id}"
if st.button("Finish Review", type="primary", disabled=not all_reviewed):
    st.session_state[finishing_key] = True

if st.session_state.get(finishing_key):
    st.markdown("**Select Status**")
    choice = st.selectbox(
        "Status", ["", "Refer Back", "Review Done"],
        key=f"status_choice_{doc_id}",
        help="Refer Back - there are issues or errors that need "
             "correction. Review Done - the paper has no issues and is "
             "ready to be published for students.",
    )
    finish_col, cancel_col = st.columns(2)
    if finish_col.button("Finish", disabled=not choice, type="primary"):
        with store.connect() as conn:
            if choice == "Refer Back":
                store.update_intake_document(conn, doc_id, status="refer_back")
                message = "Sent back to data-entry with your comments."
            else:
                # Review Done and promotion happen together: three
                # people (data-entry, the assigning administrator, and
                # the teacher) have now all signed off, so nothing is
                # left for a fourth manual step to do.
                store.update_intake_document(conn, doc_id, status="review_done")
                store.promote_document(conn, doc_id)
                message = "Review finished - promoted into the real bank."
        st.session_state[finishing_key] = False
        st.success(message)
        st.rerun()
    if cancel_col.button("Cancel"):
        st.session_state[finishing_key] = False
        st.rerun()
