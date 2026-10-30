import { Fragment, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { AuthImg } from '../../lib/useFileUrl.jsx'
import { devIdentity, isDevAuth, setDevIdentity } from '../../lib/auth.js'
import { joinSoftBreaks } from '../../lib/text.js'
import { shownNumber } from '../../lib/numbers.js'
import { useToast } from '../../components/Toast.jsx'
import PaperSummary from '../../components/PaperSummary.jsx'
import OriginalPaper from './OriginalPaper.jsx'
import RepeatPanel from './RepeatPanel.jsx'
import QuestionHistory from '../../components/QuestionHistory.jsx'
import Pictures from '../../components/Pictures.jsx'
import { groupsStartingAt } from '../data-entry/GroupInstructions.jsx'
import './TeacherReview.css'

// The subject teacher's review of one paper. Mirrors
// design-system/screens/project/TeacherReview.dc.html: every question with its
// marks, options, marking guidance and comments, a per-question reviewed tick,
// and the finish decision. The teacher ticks, comments, corrects a small
// mistake in a question's wording (kept in its history), and finishes.
//
// `adminView`: an administrator (MASAD, admin, superadmin) opens a paper here
// from Paper assignment, as EdSider's MASAD opens its teacher's Paper Review -
// the same page, read only, with the teacher's progress and what they changed.

const STATUS_TEXT = {
  teacher_review: 'Awaiting your review',
  refer_back: 'Referred back to data entry',
  review_done: 'Review done',
  ready_to_publish: 'Ready to publish',
  published: 'Published',
  archived: 'Archived',
}

function gradeText(code) {
  return code ? code.replace(/^G0*/, 'Grade ') : ''
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleString()
}

export default function TeacherReview({ adminView = false }) {
  const { PageHeader, Button, Notice, StatusPill } = window.EdSider
  const toast = useToast()
  const params = useParams()
  const [papers, setPapers] = useState(adminView ? [] : null)
  const [docId, setDocId] = useState(adminView ? Number(params.docId) : null)
  // The paper's history - every assignment, correction, replacement and
  // status change - for the administrator's view.
  const [history, setHistory] = useState(null)
  // A question being corrected: its id and the wording being edited.
  const [editing, setEditing] = useState(null)
  // The paper that was open has been deleted.
  const [paperGone, setPaperGone] = useState(false)
  const [doc, setDoc] = useState(null)
  const [questions, setQuestions] = useState([])
  const [comments, setComments] = useState({})
  const [images, setImages] = useState({})
  const [loadError, setLoadError] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState({})
  const [commenting, setCommenting] = useState(null)
  const [draft, setDraft] = useState('')
  const [decision, setDecision] = useState(null)
  const [finishing, setFinishing] = useState(false)
  const [message, setMessage] = useState(null)
  const [who, setWho] = useState(null)
  // This teacher's own identity - a paper may have had two teachers, and whether
  // it is still open to this one depends on whether they have finished.
  const [myId, setMyId] = useState(null)
  const [viewAs, setViewAs] = useState(devIdentity() || '')
  const [reloadKey, setReloadKey] = useState(0)

  // Register this teacher, then list the papers handed to them.
  useEffect(() => {
    if (adminView) return
    let live = true
    api
      .touchTeacher()
      .then((t) => {
        if (live) {
          setWho(t.display_name || t.identity)
          setMyId(t.identity)
        }
        return api.listMyDocuments()
      })
      .then((list) => {
        if (!live) return
        setPapers(list)
        // Open the paper that is waiting for this teacher, if there is one.
        const first = list.find((d) => d.status === 'teacher_review') || list[0]
        if (first) setDocId(first.id)
      })
      .catch((err) => {
        if (live) setLoadError(err.message)
      })
    return () => {
      live = false
    }
  }, [reloadKey])

  // The chosen paper, its questions, comments and pictures.
  useEffect(() => {
    if (!docId) return
    let live = true
    setLoadError(null)
    if (adminView) {
      api
        .documentHistory(docId)
        .then((h) => live && setHistory(h))
        .catch(() => live && setHistory([]))
    }
    Promise.all([
      api.getIntakeDocument(docId),
      api.listIntakeQuestions(docId),
      api.listDocumentComments(docId),
      api.listDocumentImages(docId),
    ])
      .then(([d, qs, cs, imgs]) => {
        if (!live) return
        setDoc(d)
        setQuestions(qs)
        setComments(cs)
        setImages(imgs)
        setDecision(null)
      })
      .catch((err) => {
        if (!live) return
        if (err.paperDeleted) {
          // Deleted by an administrator: off the screen, and the list is
          // read again without it.
          setDoc(null)
          setDocId(null)
          setPaperGone(true)
          setReloadKey((k) => k + 1)
        } else setLoadError(err.message)
      })
    return () => {
      live = false
    }
  }, [docId])

  const iFinished = Boolean(doc && myId && (doc.teachers_done || []).includes(myId))
  // An administrator follows the paper; only its teacher works on it.
  const reviewable = !adminView && doc?.status === 'teacher_review' && !iFinished
  const otherTeacher = (doc?.teachers || []).find((t) => t !== myId)
  // The owner teacher settles repeats; the syllabus checker reads, ticks and
  // comments. A paper from before owners were kept counts its first teacher.
  const owner = doc?.teacher_owner || (doc?.teachers || [])[0] || null
  const iAmOwner = !myId || !owner || owner === myId
  const done = questions.filter((q) => q.teacher_reviewed).length
  const pct = questions.length ? Math.round((done / questions.length) * 100) : 0
  const allReviewed = questions.length > 0 && done === questions.length
  // Repeats data entry flagged that no teacher has resolved yet - the paper
  // cannot be marked Review done until each is replaced or kept.
  const openRepeats = iAmOwner ? questions.filter((q) => q.repeat?.status === 'open').length : 0
  const byId = Object.fromEntries(questions.map((x) => [x.id, x]))

  // A question replaced or kept: its card updates; if its pictures went with
  // the old question, the pictures are read again.
  async function handleRepeatChanged(updated, picturesMoved) {
    setQuestions((list) => list.map((x) => (x.id === updated.id ? updated : x)))
    toast.success(updated.repeat?.status === 'kept' ? 'Question kept.' : 'Question replaced - the old one is in its history.')
    if (picturesMoved) {
      try {
        setImages(await api.listDocumentImages(docId))
      } catch {
        // The card still shows the new question; pictures refresh on reload.
      }
    }
  }

  const labelOf = (q) => (q.question_number ? `Q${q.question_number}` : `Q${q.seq}`)

  // A small correction - a spelling, a wrong word - to the question, its
  // options or its answer. The wording before stays in its history.
  function startEdit(q) {
    setEditing({
      id: q.id,
      text: q.mcq ? q.mcq.stem : q.text || '',
      options: q.mcq ? q.mcq.options.map((o) => ({ marker: o.marker, text: o.text })) : null,
      answer: q.answer || '',
    })
  }

  async function saveEdit(q) {
    setBusy((b) => ({ ...b, [`e${q.id}`]: true }))
    setError(null)
    try {
      const updated = await api.correctQuestion(q.id, {
        text: editing.text,
        options: editing.options,
        answer: editing.answer,
      })
      setQuestions((list) => list.map((x) => (x.id === updated.id ? updated : x)))
      setEditing(null)
      toast.success('Correction saved - the earlier wording is in its history.')
    } catch (err) {
      setError(err.message)
      toast.error(`Not saved: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [`e${q.id}`]: false }))
    }
  }

  async function toggleReviewed(q) {
    setBusy((b) => ({ ...b, [q.id]: true }))
    setError(null)
    try {
      const updated = await api.updateIntakeQuestion(q.id, { teacher_reviewed: !q.teacher_reviewed })
      setQuestions((list) => list.map((x) => (x.id === updated.id ? updated : x)))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy((b) => ({ ...b, [q.id]: false }))
    }
  }

  async function saveComment(q) {
    const text = draft.trim()
    if (!text) return
    setBusy((b) => ({ ...b, [`c${q.id}`]: true }))
    setError(null)
    try {
      await api.addQuestionComment(q.id, text)
      setComments(await api.listDocumentComments(docId))
      setDraft('')
      setCommenting(null)
      toast.success('Comment added.')
    } catch (err) {
      setError(err.message)
      toast.error(`Comment not saved: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [`c${q.id}`]: false }))
    }
  }

  async function finish() {
    if (!decision) return
    setFinishing(true)
    setError(null)
    setMessage(null)
    try {
      const updated = await api.finishReview(docId, decision)
      setDoc(updated)
      const bothDone = updated.status === 'ready_to_publish'
      setMessage(
        decision !== 'review_done'
          ? 'Referred back to data entry with your comments.'
          : bothDone
            ? 'Review done - the question bank has your corrections and the paper is ready to publish.'
            : 'Review done - waiting for the other teacher to finish their review.',
      )
      setDecision(null)
      toast.success(
        decision !== 'review_done'
          ? 'Paper referred back to data entry.'
          : bothDone
            ? 'Review finished - the paper is ready to publish.'
            : 'Your review is finished.',
      )
    } catch (err) {
      setError(err.message)
      toast.error(`Could not finish the review: ${err.message}`)
    } finally {
      setFinishing(false)
    }
  }

  function applyViewAs(e) {
    e.preventDefault()
    setDevIdentity(viewAs.trim() || null)
    setPapers(null)
    setDocId(null)
    setDoc(null)
    setQuestions([])
    setLoadError(null)
    setViewAs(viewAs.trim())
    setReloadKey((k) => k + 1)
  }

  if (loadError) {
    return (
      <Notice tone="error" title="Could not load your papers">
        {loadError}
      </Notice>
    )
  }
  if (papers === null) return <Notice tone="info">Loading...</Notice>

  return (
    <>
      {adminView ? (
        <>
          <p style={{ margin: '0 0 8px' }}>
            <Link to="/app/admin">Back to Paper assignment</Link>
          </p>
          <PageHeader
            accent="Paper"
            title="review"
            subtitle="This paper as its teacher sees it - their progress, comments and the changes they made. Read only."
          />
        </>
      ) : (
        <PageHeader
          accent="Teacher"
          title="review"
          subtitle="Every question on the paper, laid out the way you check it in EdSider - the question, its marks and its answer - with a place to say what is wrong."
        />
      )}

      <div className={`tr-split${doc ? ' tr-split--with-original' : ''}`}>
      <div className="tr-main">
      {paperGone ? (
        <Notice tone="warning" title="This paper has been deleted">
          It is no longer in EdsiderAI.
        </Notice>
      ) : null}
      {isDevAuth && !adminView ? (
        <form className="es-card tr-viewas" onSubmit={applyViewAs}>
          <label className="es-field__label" htmlFor="tr-viewas">
            Viewing as (local development only)
          </label>
          <div className="tr-viewas__row">
            <input
              id="tr-viewas"
              className="es-input"
              placeholder="A teacher's identity, e.g. teacher"
              value={viewAs}
              onChange={(e) => setViewAs(e.target.value)}
            />
            <Button variant="secondary" size="sm" type="submit">
              Apply
            </Button>
          </div>
        </form>
      ) : null}

      {papers.length === 0 && !adminView ? (
        <Notice tone="info">
          No papers have been handed to you yet. The assigning administrator sends papers to teachers who have opened
          this app.
        </Notice>
      ) : null}

      {doc && adminView ? (
        <section className="es-card tr-picker" aria-label="Review progress">
          <div>
            <p className="es-field__label" style={{ margin: 0 }}>Teacher</p>
            <p style={{ margin: '4px 0 0' }}>
              {(doc.teachers || []).length
                ? doc.teachers.map((t) => `${t}${(doc.teachers_done || []).includes(t) ? ' - review done' : ' - reviewing'}`).join(', ')
                : 'Not assigned yet'}
            </p>
          </div>
          <div className="tr-progress">
            <p className="es-field__label" style={{ margin: 0 }}>
              {done} of {questions.length} marked reviewed by the teacher
            </p>
            <div className="tr-progress__bar">
              <div className="tr-progress__fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
        </section>
      ) : null}

      {doc && adminView ? (
        <details className="es-card tr-changes">
          <summary>
            <strong>Changes and history</strong>{' '}
            <span className="es-caption">
              {history ? `${history.length} entries - corrections, replaced questions, assignments and status changes` : 'Loading...'}
            </span>
          </summary>
          <ol className="tr-changes__list">
            {[...(history || [])].reverse().map((h, i) => (
              <li key={i}>
                <span className="es-caption">{when(h.at)}</span>{' '}
                <span>
                  {h.action === 'status'
                    ? `Status: ${STATUS_TEXT[h.from_status] || h.from_status || '-'} to ${STATUS_TEXT[h.to_status] || h.to_status}`
                    : h.action}
                  {h.assigned_to ? ` - ${h.assigned_to}` : ''}
                </span>{' '}
                {h.actor ? <span className="es-caption">by {h.actor}</span> : null}
              </li>
            ))}
          </ol>
          <p className="es-caption" style={{ margin: 0 }}>
            Each corrected or replaced question also shows its earlier wording under "Earlier versions".
          </p>
        </details>
      ) : null}

      {doc && !adminView ? (
        <section className="es-card tr-picker" aria-label="Paper">
          <div className="es-field">
            <label className="es-field__label" htmlFor="tr-paper">
              Your papers
            </label>
            <select
              id="tr-paper"
              className="es-input"
              value={docId || ''}
              onChange={(e) => {
                setPaperGone(false)
                setDocId(Number(e.target.value))
              }}
            >
              {papers.map((p) => (
                <option key={p.id} value={p.id}>
                  #{p.id} - {p.filename} ({STATUS_TEXT[p.status] || p.status})
                </option>
              ))}
            </select>
          </div>
          <div className="tr-progress">
            <p className="es-field__label" style={{ margin: 0 }}>
              {done} of {questions.length} marked reviewed
            </p>
            <div className="tr-progress__bar">
              <div className="tr-progress__fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
        </section>
      ) : null}

      {doc ? <PaperSummary doc={doc} questions={questions} /> : null}

      {doc && (doc.teachers || []).length > 1 ? (
        <Notice tone="info" title={iAmOwner ? 'You are the owner teacher of this paper' : 'You are the syllabus checker for this paper'}>
          {iAmOwner
            ? 'Check every question, add comments, and settle any question data entry flagged as a repeat - write a new question in its place or keep it.'
            : `Check every question against the syllabus and add comments where something is wrong. Repeats are settled by the owner teacher${owner ? ` (${owner})` : ''}.`}
        </Notice>
      ) : null}

      {adminView ? null : doc && iFinished && doc.status === 'teacher_review' ? (
        <Notice tone="info" title="Your review is done">
          The paper is ready to publish once {otherTeacher ? `the other teacher (${otherTeacher})` : 'the other teacher'}{' '}
          finishes too. You can read it here.
        </Notice>
      ) : doc && !reviewable ? (
        <Notice tone="info" title={`This paper is ${STATUS_TEXT[doc.status] || doc.status}`}>
          You can read it here, but ticks, comments and the finish decision are only open while it is with you.
        </Notice>
      ) : null}

      {message ? <Notice tone="success">{message}</Notice> : null}
      {error ? (
        <Notice tone="error" title="Could not save">
          {error}
        </Notice>
      ) : null}

      {doc ? (
        <div className="tr-doc-head">
          <StatusPill status={doc.status} />
          <span className="es-caption">
            {doc.paper_subject || 'Subject not set'} - {gradeText(doc.paper_grade) || 'Grade not set'} -{' '}
            {doc.paper_title || doc.filename}
          </span>
          {who && !adminView ? <span className="es-caption">Signed in as {who}</span> : null}
        </div>
      ) : null}

      <div className="tr-list">
        {doc?.notes ? (
          <div className="es-card" role="note">
            <strong>Instructions for the whole paper</strong>
            <p style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>{doc.notes}</p>
          </div>
        ) : null}
        {questions.map((q) => {
          const depth = q.depth || 0
          const qComments = comments[String(q.id)] || [];
          const qImages = images[String(q.id)] || []
          const answerImages = qImages.filter((i) => i.slot === 'answer')
          const optionImages = Object.fromEntries(
            qImages.filter((i) => i.slot.startsWith('option:')).map((i) => [i.slot.slice(7), i]),
          )
          const questionImages = qImages.filter((i) => i.slot === 'question')
          const stem = joinSoftBreaks(q.mcq ? q.mcq.stem : q.text)
          const isMcq = !!q.mcq
          return (
            <Fragment key={q.id}>
            {depth === 0
              ? groupsStartingAt(doc?.instruction_groups, q.section, q.question_number).map((g, gi) => (
                  <div key={`g-${gi}`} className="es-card" role="note"
                    style={{ borderLeft: '4px solid var(--primary)' }}>
                    <strong>{g.title ? `${g.title} - ` : ''}Questions {g.from} to {g.to}</strong>
                    <p style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>{g.text}</p>
                  </div>
                ))
              : null}
            <article
              className="es-card tr-q"
              style={{ marginLeft: depth * 28 }}
              aria-label={`${labelOf(q)} review`}
            >
              <div className="tr-q__head">
                <h2 className="scr-h2 tr-q__label">
                  {depth > 0 ? shownNumber(q) : `Question ${shownNumber(q)}`}
                </h2>
                <span className="tr-q__marks">{q.marks != null ? `${q.marks} ${q.marks === 1 ? 'mark' : 'marks'}` : 'Marks not set'}</span>
              </div>

              <RepeatPanel
                key={`${q.id}-${q.repeat?.status}`}
                q={q}
                original={q.repeat ? byId[q.repeat.repeat_of] : null}
                editable={reviewable && iAmOwner}
                owner={iAmOwner ? null : owner}
                onChanged={handleRepeatChanged}
              />

              {editing?.id === q.id ? (
                <div className="tr-edit">
                  <label className="es-field__label" htmlFor={`tr-e-${q.id}`}>Question</label>
                  <textarea id={`tr-e-${q.id}`} className="es-input" rows={4} value={editing.text}
                    onChange={(e) => setEditing((x) => ({ ...x, text: e.target.value }))} />
                  {editing.options
                    ? editing.options.map((o, i) => (
                        <div className="tr-repeat__row" key={i}>
                          <span className="tr-option__key">{o.marker.trim()}</span>
                          <input className="es-input" style={{ flex: 1, minWidth: 200 }} value={o.text}
                            aria-label={`Option ${o.marker.trim()}`}
                            onChange={(e) => setEditing((x) => ({
                              ...x,
                              options: x.options.map((y, j) => (j === i ? { ...y, text: e.target.value } : y)),
                            }))} />
                        </div>
                      ))
                    : null}
                  <label className="es-field__label" htmlFor={`tr-ea-${q.id}`}>
                    {isMcq ? 'Answer (the correct option)' : 'Answer / marking scheme'}
                  </label>
                  <textarea id={`tr-ea-${q.id}`} className="es-input" rows={isMcq ? 1 : 3} value={editing.answer}
                    onChange={(e) => setEditing((x) => ({ ...x, answer: e.target.value }))} />
                  <div className="tr-repeat__row">
                    <Button variant="primary" size="sm" onClick={() => saveEdit(q)} disabled={busy[`e${q.id}`]}>
                      {busy[`e${q.id}`] ? 'Saving...' : 'Save correction'}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setEditing(null)}>Cancel</Button>
                  </div>
                </div>
              ) : stem ? (
                <div>
                  <p className="tr-lab">Question</p>
                  <p className="tr-text">{stem}</p>
                </div>
              ) : null}
              <Pictures images={questionImages} className="tr-img" />

              {isMcq && editing?.id !== q.id ? (
                <div>
                  <p className="tr-lab">Answer options</p>
                  <div className="tr-options">
                    {q.mcq.options.map((o) => {
                      const correct = (q.mcq.correct_labels || []).includes(o.label.toLowerCase())
                      const img = optionImages[o.label]
                      return (
                        <div key={o.label} className={`tr-option${correct ? ' tr-option--correct' : ''}`}>
                          <span className="tr-option__key">{o.marker.trim()}</span>
                          <span>{joinSoftBreaks(o.text)}</span>
                          {correct ? <span className="tr-option__tag">Correct</span> : null}
                          {img ? <AuthImg className="tr-img" path={api.imagePath(img.id)} alt="" /> : null}
                        </div>
                      )
                    })}
                  </div>
                </div>
              ) : null}

              {q.answer || answerImages.length ? (
                <div className="tr-guide">
                  <p className="tr-lab">Marking scheme</p>
                  {q.answer ? <p className="tr-text">{joinSoftBreaks(q.answer)}</p> : null}
                  <Pictures images={answerImages} className="tr-img" />
                </div>
              ) : null}

              {isMcq && (q.mcq.correct_labels || []).length ? (
                <p className="tr-correct">
                  <span className="tr-lab">Correct answer</span> {q.mcq.correct_labels.map((l) => l.toUpperCase()).join(', ')}
                </p>
              ) : null}

              <QuestionHistory key={`h-${q.id}-${q.revisions}`} questionId={q.id} count={q.revisions} />

              {qComments.length ? (
                <div className="tr-comments">
                  <p className="tr-lab">Comments</p>
                  {qComments.map((c) => (
                    <div className="tr-comment" key={c.id}>
                      <p className="es-caption" style={{ margin: '0 0 4px' }}>
                        {c.author || 'Reviewer'} - {when(c.created_at)}
                      </p>
                      <p className="tr-text">{c.comment}</p>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="tr-q__foot">
                <label className="es-check">
                  <input
                    type="checkbox"
                    checked={!!q.teacher_reviewed}
                    disabled={!reviewable || busy[q.id]}
                    onChange={() => toggleReviewed(q)}
                  />{' '}
                  Marked as reviewed
                </label>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!reviewable}
                  onClick={() => {
                    setDraft('')
                    setCommenting(commenting === q.id ? null : q.id)
                  }}
                >
                  Add comment
                </Button>
                {reviewable && editing?.id !== q.id ? (
                  <Button variant="secondary" size="sm" onClick={() => startEdit(q)}>
                    Edit
                  </Button>
                ) : null}
              </div>

              {commenting === q.id ? (
                <div className="tr-comment-box">
                  <label className="es-field__label" htmlFor={`tr-c-${q.id}`}>
                    Comment
                  </label>
                  <textarea
                    id={`tr-c-${q.id}`}
                    className="es-input"
                    rows={3}
                    placeholder="Is this question or its answer wrong? Say what needs fixing."
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <div>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => saveComment(q)}
                      disabled={!draft.trim() || busy[`c${q.id}`]}
                    >
                      {busy[`c${q.id}`] ? 'Saving...' : 'Add comment'}
                    </Button>
                  </div>
                </div>
              ) : null}
            </article>
            </Fragment>
          )
        })}
      </div>

      {doc && !adminView ? (
        <section className="es-card tr-finish" aria-label="Finish review">
          <h2 className="scr-h2">Finish review</h2>
          <fieldset className="tr-finish__choices">
            <legend className="es-field__label">What should happen to this paper?</legend>
            <label className={`tr-choice${decision === 'refer_back' ? ' tr-choice--on' : ''}`}>
              <input
                type="radio"
                name="tr-finish"
                disabled={!reviewable}
                checked={decision === 'refer_back'}
                onChange={() => setDecision('refer_back')}
              />
              <span>
                <span className="tr-choice__title">Refer back</span>
                <span className="es-caption">
                  There are issues or errors that need correction. It goes back to data entry with your comments.
                </span>
              </span>
            </label>
            <label className={`tr-choice${decision === 'review_done' ? ' tr-choice--on' : ''}`}>
              <input
                type="radio"
                name="tr-finish"
                disabled={!reviewable}
                checked={decision === 'review_done'}
                onChange={() => setDecision('review_done')}
              />
              <span>
                <span className="tr-choice__title">Review done</span>
                <span className="es-caption">
                  The paper has no issues. It is added to the question bank, ready to publish for students.
                </span>
              </span>
            </label>
          </fieldset>
          {!allReviewed ? <Notice tone="info">Mark every question reviewed before finishing.</Notice> : null}
          {openRepeats && decision === 'review_done' ? (
            <Notice tone="warning">
              {openRepeats} question(s) are flagged as repeats on this paper - replace each with a new
              question, or keep it, before finishing as Review done.
            </Notice>
          ) : null}
          <div>
            <Button
              variant="primary"
              onClick={finish}
              disabled={!reviewable || !allReviewed || !decision || finishing
                || (decision === 'review_done' && openRepeats > 0)}
            >
              {finishing ? 'Saving...' : 'Finish review'}
            </Button>
          </div>
        </section>
      ) : null}
      </div>

      {doc ? (
        <aside className="tr-original" aria-label="Original paper">
          <OriginalPaper key={doc.id} doc={doc} />
        </aside>
      ) : null}
      </div>
    </>
  )
}
