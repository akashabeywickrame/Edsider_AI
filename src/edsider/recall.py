"""Take a sent paper back out of EdSider - the other half of send.py, for
reopening a paper here (service.reopen_document).

Only what send.write_package() wrote is removed, and only while EdSider has
not started on the paper: it is still Initial, Ready or Referred back, no
one is assigned to it beyond the send's own rows, its questions are its
own, and no student, order or link has touched it. Otherwise nothing is
removed and RecallRefused says why - the paper is EdSider's to change from
then on. A paper EdSider has deleted itself only has the send's leftovers
removed (questions no other paper uses, the work rows EdSider keeps
without a key to the paper).

One transaction, through the caller's connection; the caller commits."""

from .send import _review_table

# EdSider states (common.assessment_status) a paper can still be taken
# back from: before its MASAD has put anyone on it.
RECALLABLE = {1: "Initial", 2: "Ready", 7: "ReferBack"}

# Rows that mean EdSider is using the paper.
_IN_USE = [
    ("candidate.candidate", "students are registered for it"),
    ("candidate.candidate_answer", "students have answered it"),
    ("candidate.candidate_grade_report", "it has grade reports"),
    ("assessment_subscription.order_item", "it has been ordered"),
    ("assessment_subscription.exam_transaction", "it has been bought"),
    ("assessment_subscription.cart_item", "it is in a shopping cart"),
    ("assessment_subscription.saved_later_item", "someone saved it for later"),
    ("assessment.assessment_links", "it is linked to other papers"),
    ("assessment.assessment_autoshuffle", "its shuffle setting was set in EdSider"),
]


class RecallRefused(ValueError):
    """EdSider has started on the paper - it cannot be taken back."""


def _count(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _exists(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return cur.fetchone()[0]


def recall(pg, assessment_id, question_ids):
    """Remove EdSider paper `assessment_id` - sent from here with
    `question_ids` (EdSider's ids of every question the send wrote, as
    intake.edsider_send keeps them) - through connection `pg` (not
    committed). Returns {"found": whether EdSider still had the paper,
    "questions": how many question rows were removed}. Raises
    RecallRefused, having removed nothing, when EdSider has started on it."""
    cur = pg.cursor()
    ours = sorted({int(q) for q in question_ids})
    review_table = _review_table(cur)
    cur.execute("SELECT status_id, assessment_state, created_at FROM assessment.assessment "
                "WHERE assessment_id = %s FOR UPDATE", (assessment_id,))
    paper = cur.fetchone()

    if paper is None:
        # Deleted in EdSider. Its questions stay there unless something
        # removed them too: the ones nothing uses go, as do the work rows
        # EdSider keeps by number only.
        cur.execute(
            """SELECT q.question_id FROM question.question q
                WHERE q.question_id = ANY(%s)
                  AND NOT EXISTS (SELECT 1 FROM assessment.assessment_question l
                                   WHERE l.question_id = q.question_id)
                  AND NOT EXISTS (SELECT 1 FROM question.question_share s
                                   WHERE s.question_id = q.question_id)""", (ours,))
        loose = [r[0] for r in cur.fetchall()]
        removed = _remove_questions(cur, assessment_id, loose)
        cur.execute("DELETE FROM assessment.assessment_operation WHERE asmnt_id = %s",
                    (assessment_id,))
        cur.execute(f"DELETE FROM {review_table} WHERE asmnt_id = %s", (assessment_id,))
        cur.close()
        return {"found": False, "questions": removed}

    status_id, state, created_at = paper
    reasons = []
    if status_id not in RECALLABLE:
        reasons.append(f"it is {state or f'status {status_id}'} there")
    # The send wrote one assessor (its sender, as chief assessor) and its
    # operation and review rows, all stamped with the paper's own created_at.
    if _count(cur, "SELECT count(*) FROM assessment.assessment_assessor WHERE assessment_id = %s "
                   "AND NOT (role_code = 'CASS' AND from_date = %s)", (assessment_id, created_at)):
        reasons.append("assessors have been assigned to it")
    if _count(cur, "SELECT count(*) FROM assessment.assessment_operation WHERE asmnt_id = %s "
                   "AND created_at IS DISTINCT FROM %s", (assessment_id, created_at)):
        reasons.append("staff have been given work on it")
    if _count(cur, f"SELECT count(*) FROM {review_table} WHERE asmnt_id = %s "
                   "AND created_at IS DISTINCT FROM %s", (assessment_id, created_at)):
        reasons.append("reviewers have been assigned to it")
    for table, why in _IN_USE:
        if _exists(cur, table) and _count(
                cur, f"SELECT count(*) FROM {table} WHERE assessment_id = %s", (assessment_id,)):
            reasons.append(why)
    cur.execute("SELECT question_id FROM assessment.assessment_question WHERE assessment_id = %s",
                (assessment_id,))
    if {r[0] for r in cur.fetchall()} - set(ours):
        reasons.append("questions have been added to it in EdSider")
    if ours and _count(cur, "SELECT count(*) FROM assessment.assessment_question "
                            "WHERE question_id = ANY(%s) AND assessment_id <> %s",
                       (ours, assessment_id)):
        reasons.append("its questions are used on another EdSider paper")
    if ours and _count(cur, "SELECT count(*) FROM question.question_share "
                            "WHERE question_id = ANY(%s)", (ours,)):
        reasons.append("its questions have been shared")
    if reasons:
        cur.close()
        raise RecallRefused(f"EdSider has started on paper #{assessment_id}: "
                            + "; ".join(reasons)
                            + ". It can only be changed in EdSider now.")

    cur.execute("DELETE FROM assessment.assessment_question WHERE assessment_id = %s",
                (assessment_id,))
    removed = _remove_questions(cur, assessment_id, ours)
    cur.execute("""DELETE FROM assessment.assessment_part_section WHERE assessment_part_id IN
                     (SELECT assessment_part_id FROM assessment.assessment_part
                       WHERE assessment_id = %s)""", (assessment_id,))
    for table, column in (("assessment.assessment_part", "assessment_id"),
                          ("assessment.assessment_schedule", "assessment_id"),
                          ("assessment.assessment_assessor", "assessment_id"),
                          ("assessment.assessment_operation", "asmnt_id"),
                          (review_table, "asmnt_id"),
                          ("assessment.assessment", "assessment_id")):
        cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (assessment_id,))
    cur.close()
    return {"found": True, "questions": removed}


def _remove_questions(cur, assessment_id, question_ids):
    """Delete these question rows and the passages the send wrote for them
    (titled "ASMNT<paper> ...") that no question uses any more."""
    if not question_ids:
        return 0
    cur.execute("SELECT DISTINCT passage_id FROM question.question "
                "WHERE question_id = ANY(%s) AND passage_id IS NOT NULL", (question_ids,))
    passages = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM question.question WHERE question_id = ANY(%s)", (question_ids,))
    removed = cur.rowcount
    if passages:
        cur.execute("""DELETE FROM question.passage p WHERE p.passage_id = ANY(%s)
                          AND p.title LIKE %s
                          AND NOT EXISTS (SELECT 1 FROM question.question q
                                           WHERE q.passage_id = p.passage_id)""",
                    (passages, f"ASMNT{assessment_id} %"))
    return removed
