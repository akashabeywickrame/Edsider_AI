import { Fragment, useEffect, useMemo, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import StatusPill from '../../components/StatusPill.jsx'
import { loadLists } from '../data-entry/EdsiderLabels.jsx'
import PaperFilters, { MENUS, PaperCount, Pager, TEACHER_MENU, usePaperSearch } from '../../components/PaperFilters.jsx'
import './PaperAssignment.css'
import { confirmDialog } from '../../components/ConfirmDialog.jsx'
import BusyOverlay from '../../components/BusyOverlay.jsx'

// Papers data entry has checked for internal repeats, waiting to be handed to
// a teacher - and the ones already with a teacher. Mirrors
// design-system/screens/project/AssignPapers.dc.html. Nothing here edits a
// question; it decides who reviews a paper and, once its teacher has
// finished, a Main Assessment Administrator sends it to EdSider from here.
// One teacher a paper: normally its author; another teacher when the author
// does not want to review it.

const WITH_TEACHER = ['teacher_review', 'refer_back', 'review_done', 'ready_to_publish', 'published', 'archived']
// Where an administrator may take a paper off its teacher (not once sent or
// archived). Any paper here may be deleted - one sent to EdSider stays there.
const UNASSIGNABLE = ['teacher_review', 'review_done', 'ready_to_publish']
const ASSIGNED_MENUS = [...MENUS, TEACHER_MENU]

function gradeText(code) {
  return code ? code.replace(/^G0*/, 'Grade ') : ''
}

// EdSider's names carry stray and doubled spaces.
function plain(name) {
  return (name || '').trim().replace(/\s+/g, ' ')
}

function teacherLabel(t) {
  const name = plain(t.display_name)
  return name && name !== t.identity ? `${name} (${t.identity})` : t.identity
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleString()
}

export default function PaperAssignment() {
  const { PageHeader, Button, Notice } = window.EdSider
  const toast = useToast()
  // Only EdSider's Main Assessment Administrators send papers (the API
  // refuses anyone else). me.role is null on a local run, where it is open.
  const me = useOutletContext()
  const navigate = useNavigate()
  const canSend = !me || !me.role || Boolean(me.masad)
  const sendingOn = !me || !me.role || Boolean(me.edsider_sending)
  const [sending, setSending] = useState(null)
  const [sendProblems, setSendProblems] = useState({})
  // Papers waiting for a teacher; the assigned ones are searched a page at
  // a time, with the paper finder's filters and a teacher's.
  const [docs, setDocs] = useState(null)
  const assignedSearch = usePaperSearch({ menus: ASSIGNED_MENUS, statuses: WITH_TEACHER })
  const [teachers, setTeachers] = useState([])
  const [chosen, setChosen] = useState({})
  const [busy, setBusy] = useState({})
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)
  // Fetched on demand, one paper at a time, when its comments are opened -
  // not every paper in the table needs them, and most never will.
  const [openComments, setOpenComments] = useState(null)
  const [commentsByDoc, setCommentsByDoc] = useState({})
  const [commentsBusy, setCommentsBusy] = useState(false)

  const [teachersBusy, setTeachersBusy] = useState(false)
  const [teachersNote, setTeachersNote] = useState(null)
  // EdSider's author list - the paper's author is its usual teacher.
  const [authors, setAuthors] = useState([])
  // The paper whose teacher is being changed, in the assigned table.
  const [changing, setChanging] = useState(null)

  // Alphabetical by the name shown, so a teacher is easy to find.
  const sortedTeachers = useMemo(
    () => [...teachers].sort((a, b) => teacherLabel(a).localeCompare(teacherLabel(b), undefined, { sensitivity: 'base' })),
    [teachers],
  )

  // Pulls EdSider's current teacher list first, so someone added there a
  // minute ago is already in the dropdown. If EdSider cannot be reached,
  // the last list copied is still shown, with a note saying so.
  // The saved copy is shown at once; the refresh (a few seconds) replaces
  // it when it lands, so the dropdown is usable while EdSider is asked.
  async function refreshTeachers() {
    setTeachersBusy(true)
    setTeachersNote(null)
    try {
      setTeachers(await api.listTeachers())
    } catch (err) {
      setError(err.message)
    }
    try {
      await api.syncTeachers()
      setTeachers(await api.listTeachers())
    } catch (err) {
      setTeachersNote(`Showing the last teacher list copied from EdSider - ${err.message}`)
    } finally {
      setTeachersBusy(false)
    }
  }

  function loadReady() {
    return api
      .listIntakeDocuments('data_entry_done')
      .then(setDocs)
      .catch((err) => setError(err.message))
  }

  // After a paper moves: the waiting list and the table both change.
  function refreshPapers() {
    loadReady()
    assignedSearch.reload()
  }

  useEffect(() => {
    let live = true
    loadReady()
    refreshTeachers()
    loadLists()
      .then((r) => live && setAuthors(r.lists?.author || []))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  // The paper's author (a "For EdSider" label) and the teacher of the same
  // name, when there is one - EdSider keeps authors and teachers apart, so
  // they are matched by name.
  function authorOf(doc) {
    const id = doc.edsider_labels?.author_id
    const author = id != null ? authors.find((a) => a.id === id) : null
    if (!author) return { name: null, teacher: null }
    const name = plain(author.name)
    const teacher = teachers.find((t) => plain(t.display_name).toLowerCase() === name.toLowerCase()) || null
    return { name, teacher }
  }

  // One teacher a paper; it is ready to publish once they have finished.
  // Until someone chooses, the author's own teacher entry is chosen.
  const chosenFor = (doc) => chosen[doc.id] ?? authorOf(doc).teacher?.identity ?? ''
  const choose = (docId, identity) => setChosen((c) => ({ ...c, [docId]: identity }))

  async function assign(doc) {
    const identity = chosenFor(doc)
    if (!identity) return
    setBusy((b) => ({ ...b, [doc.id]: true }))
    setError(null)
    setMessage(null)
    try {
      await api.assignDocument(doc.id, [identity])
      refreshPapers()
      setChanging(null)
      const t = teachers.find((x) => x.identity === identity)
      const text = `#${doc.id} ${doc.paper_subject || doc.filename} assigned to ${plain(t?.display_name) || identity}.`
      setMessage(text)
      toast.success(text)
    } catch (err) {
      setError(err.message)
      toast.error(`Assignment failed: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [doc.id]: false }))
    }
  }

  // The question bank is refreshed one last time, the paper is written into
  // EdSider ready to publish there, and it is archived here as 'published'.
  async function sendToEdsider(doc) {
    if (!(await confirmDialog('Send this paper to EdSider?'))) return
    setSending(doc.id)
    setError(null)
    setMessage(null)
    setSendProblems((p) => ({ ...p, [doc.id]: null }))
    try {
      const sent = await api.sendToEdsider(doc.id)
      assignedSearch.reload()
      const text = `#${doc.id} sent to EdSider as assessment #${sent.edsider_assessment_id}.`
      setMessage(text)
      toast.success(text)
    } catch (err) {
      setSendProblems((p) => ({ ...p, [doc.id]: { message: err.message, problems: err.problems || [] } }))
      toast.error(`Not sent: ${err.message}`)
    } finally {
      setSending(null)
    }
  }

  // Back to "Ready to assign", its questions as they are.
  async function unassign(doc) {
    if (!(await confirmDialog('Unassign this paper from its teacher?'))) return
    setBusy((b) => ({ ...b, [doc.id]: true }))
    setError(null)
    setMessage(null)
    try {
      await api.unassignDocument(doc.id)
      refreshPapers()
      const text = `#${doc.id} is back in Ready to assign.`
      setMessage(text)
      toast.success(text)
    } catch (err) {
      setError(err.message)
      toast.error(`Not unassigned: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [doc.id]: false }))
    }
  }

  // Gone for good - its questions, its question-bank copy, pictures, file
  // and history.
  async function removePaper(doc) {
    if (!(await confirmDialog('Delete this paper permanently?', { danger: true }))) return
    setBusy((b) => ({ ...b, [doc.id]: true }))
    setError(null)
    setMessage(null)
    try {
      await api.deleteIntakeDocument(doc.id)
      refreshPapers()
      const text = `#${doc.id} deleted.`
      setMessage(text)
      toast.success(text)
    } catch (err) {
      setError(err.message)
      toast.error(`Not deleted: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [doc.id]: false }))
    }
  }

  function toggleComments(docId) {
    if (openComments === docId) {
      setOpenComments(null)
      return
    }
    setOpenComments(docId)
    if (!commentsByDoc[docId]) {
      setCommentsBusy(true)
      api
        .listDocumentComments(docId)
        .then((cmts) => setCommentsByDoc((prev) => ({ ...prev, [docId]: cmts })))
        .catch((err) => toast.error(`Could not load comments: ${err.message}`))
        .finally(() => setCommentsBusy(false))
    }
  }

  if (docs === null && !error) return <Notice tone="info">Loading...</Notice>

  // Data entry has finished it (and copied it to the question bank).
  const ready = (docs || []).filter((d) => d.status === 'data_entry_done')
  const assigned = assignedSearch.result?.items || []
  const nameOf = (identity) => plain(teachers.find((t) => t.identity === identity)?.display_name) || identity

  // One teacher dropdown, alphabetical; `id` keeps each one's label unique.
  const teacherSelect = (d, id) => (
    <select id={id} className="es-input" value={chosenFor(d)} onChange={(e) => choose(d.id, e.target.value)}>
      <option value="">Choose a teacher</option>
      {sortedTeachers.map((t) => (
        <option key={t.identity} value={t.identity}>
          {teacherLabel(t)}
        </option>
      ))}
    </select>
  )

  return (
    <>
      <BusyOverlay message={sending !== null ? 'Sending the paper to EdSider...' : null} />
      <PageHeader
        accent="Assign"
        title="papers to teachers"
        subtitle="Papers data entry has finished, each waiting for a teacher - usually its author. A paper is ready to publish once its teacher has finished."
      />

      {error ? (
        <Notice tone="error" title="Could not load the papers">
          {error}
        </Notice>
      ) : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      <div className="pa-head">
        <h2 className="scr-h2">Ready to assign ({ready.length})</h2>
        <p className="es-caption">
          {teachersBusy
            ? 'Loading teachers from EdSider...'
            : `${teachers.length} teacher(s) - every teacher registered in EdSider.`}{' '}
          <Button variant="ghost" size="sm" onClick={refreshTeachers} disabled={teachersBusy}>
            Refresh from EdSider
          </Button>
        </p>
      </div>
      {teachersNote ? <Notice tone="warning">{teachersNote}</Notice> : null}

      {ready.length === 0 ? <Notice tone="info">No checked papers are waiting for a teacher.</Notice> : null}

      <section aria-label="Ready to assign" className="pa-grid">
        {ready.map((d) => (
          <article className="es-card pa-card" key={d.id}>
            <div className="pa-card__head">
              <div>
                <p className="scr-h3">{d.paper_subject || 'Subject not set'}</p>
                <p className="es-caption">
                  {gradeText(d.paper_grade) || 'Grade not set'} - {d.paper_title || 'Title not set'}
                </p>
              </div>
              <StatusPill status={d.status} />
            </div>
            <p className="es-caption pa-card__meta">
              #{d.id} - {d.filename}
              {d.uploaded_at ? ` - uploaded ${when(d.uploaded_at)}` : ''}
            </p>
            <div className="es-field">
              <label className="es-field__label" htmlFor={`pa-teacher-${d.id}`}>
                Teacher
              </label>
              <p className="es-caption" style={{ margin: '0 0 4px' }}>
                {(() => {
                  const { name, teacher } = authorOf(d)
                  if (teacher) return `The paper's author, ${name}, is chosen. Choose another teacher if they do not want to review it.`
                  if (name) return `The paper's author is ${name}, who is not on the teacher list - choose the teacher who reviews it.`
                  return 'Usually the paper\'s author. Choose another teacher if the author does not want to review it.'
                })()}
              </p>
              {teacherSelect(d, `pa-teacher-${d.id}`)}
            </div>
            <div>
              <Button variant="primary" size="sm" onClick={() => assign(d)} disabled={!chosenFor(d) || busy[d.id]}>
                {busy[d.id] ? 'Assigning...' : 'Assign'}
              </Button>
            </div>
          </article>
        ))}
      </section>

      <h2 className="scr-h2 pa-section">Already assigned</h2>
      <section className="es-card pa-table-card" aria-label="Already assigned">
        <div className="pa-tools">
          <PaperFilters search={assignedSearch} idPrefix="pa-f" />
          {assignedSearch.error ? (
            <Notice tone="error" title="Could not load the assigned papers">
              {assignedSearch.error}
            </Notice>
          ) : null}
          <PaperCount search={assignedSearch} />
          {assigned.length === 0 ? (
            <p className="es-caption" style={{ margin: 0 }}>
              {assignedSearch.filtered ? 'No assigned papers match these filters.' : 'No papers have been handed to a teacher yet.'}
            </p>
          ) : null}
        </div>
        {assigned.length === 0 ? null : (
          <table className="scr-table">
            <thead>
              <tr>
                <th>Paper</th>
                <th>Teacher</th>
                <th>Status</th>
                <th>Uploaded</th>
                <th>Comments</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {assigned.map((a) => (
                <Fragment key={a.id}>
                  <tr>
                    <td>
                      <span className="pa-bold">
                        {a.paper_subject || 'Subject not set'} - {gradeText(a.paper_grade) || 'Grade not set'}
                      </span>
                      <br />
                      <span className="es-caption">
                        #{a.id} - {a.filename}
                      </span>
                    </td>
                    <td>
                      {(a.teachers || []).length === 0
                        ? '-'
                        : a.teachers.map((t) => (
                            <div key={t}>
                              {nameOf(t)}{' '}
                              <span className="es-caption">
                                {(a.teachers || []).length > 1 ? (t === (a.teacher_owner || a.teachers[0]) ? '(owner) ' : '(syllabus checker) ') : ''}
                                {(a.teachers_done || []).includes(t) ? '- review done' : '- reviewing'}
                              </span>
                            </div>
                          ))}
                      {/* The author may turn the review down once it is theirs. */}
                      {a.status === 'teacher_review' ? (
                        changing === a.id ? (
                          <div style={{ display: 'grid', gap: 6, marginTop: 6 }}>
                            <label className="es-field__label" htmlFor={`pa-change-${a.id}`}>
                              Give it to
                            </label>
                            {teacherSelect(a, `pa-change-${a.id}`)}
                            <div className="es-row" style={{ gap: 6 }}>
                              <Button
                                variant="primary"
                                size="sm"
                                onClick={() => assign(a)}
                                disabled={!chosen[a.id] || (a.teachers || []).includes(chosen[a.id]) || busy[a.id]}
                              >
                                {busy[a.id] ? 'Assigning...' : 'Assign'}
                              </Button>
                              <Button variant="ghost" size="sm" onClick={() => setChanging(null)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <Button
                            variant="secondary"
                            size="sm"
                            className="pa-btn-gap"
                            onClick={() => {
                              choose(a.id, '')
                              setChanging(a.id)
                            }}
                          >
                            Change teacher
                          </Button>
                        )
                      ) : null}
                    </td>
                    <td>
                      <div className="pa-status">
                        <StatusPill status={a.status} />
                        {a.status === 'ready_to_publish' && canSend ? (
                          <Button
                            variant="primary"
                            size="sm"
                            onClick={() => sendToEdsider(a)}
                            disabled={sending !== null || !sendingOn}
                            title={sendingOn ? undefined : 'Sending to EdSider is not switched on for this server (EDSIDER_SEND_DB).'}
                          >
                            {sending === a.id ? 'Sending...' : 'Send to EdSider'}
                          </Button>
                        ) : null}
                      </div>
                    </td>
                    <td className="es-caption">{when(a.uploaded_at)}</td>
                    <td>
                      {a.status === 'refer_back' ? (
                        <Button variant="ghost" size="sm" onClick={() => toggleComments(a.id)}>
                          {openComments === a.id ? 'Hide' : 'View'}
                        </Button>
                      ) : (
                        <span className="es-caption">-</span>
                      )}
                    </td>
                    <td>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'stretch' }}>
                        {/* The paper as its teacher sees it: progress, comments, changes. */}
                        <Button variant="secondary" size="sm" onClick={() => navigate(`/app/admin/papers/${a.id}`)}>
                          View
                        </Button>
                        {UNASSIGNABLE.includes(a.status) && !a.archived_at ? (
                          <Button variant="secondary" size="sm" className="pa-btn-theme" onClick={() => unassign(a)} disabled={busy[a.id]}>
                            Unassign
                          </Button>
                        ) : null}
                        <Button variant="danger" size="sm" onClick={() => removePaper(a)} disabled={busy[a.id]}>
                          Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                  {sendProblems[a.id] ? (
                    <tr>
                      <td colSpan={6}>
                        <Notice tone="error" title="Not sent to EdSider">
                          {sendProblems[a.id].message}
                          {sendProblems[a.id].problems.length > 0 ? (
                            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                              {sendProblems[a.id].problems.map((pr, i) => (
                                <li key={i}>
                                  {pr.question_number ? `Q${pr.question_number}: ` : ''}
                                  {pr.message}
                                </li>
                              ))}
                            </ul>
                          ) : null}
                        </Notice>
                      </td>
                    </tr>
                  ) : null}
                  {openComments === a.id ? (
                    <tr>
                      <td colSpan={6}>
                        {commentsBusy && !commentsByDoc[a.id] ? (
                          <p className="es-caption" style={{ margin: 0 }}>
                            Loading comments...
                          </p>
                        ) : (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                            {Object.values(commentsByDoc[a.id] || {}).flat().length === 0 ? (
                              <p className="es-caption" style={{ margin: 0 }}>
                                No comments recorded for this paper.
                              </p>
                            ) : (
                              Object.values(commentsByDoc[a.id] || {})
                                .flat()
                                .map((c) => (
                                  <Notice
                                    key={c.id}
                                    tone="warning"
                                    title={`${c.author || 'Teacher'} on question #${c.question_id} - ${when(c.created_at)}`}
                                  >
                                    {c.comment}
                                  </Notice>
                                ))
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
        <div className="pa-tools pa-tools--foot">
          <Pager search={assignedSearch} />
        </div>
      </section>
    </>
  )
}
