import { Fragment, useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import StatusPill from '../../components/StatusPill.jsx'
import './PaperAssignment.css'

// Papers data entry has checked for internal repeats, waiting to be handed to
// a teacher - and the ones already with a teacher. Mirrors
// design-system/screens/project/AssignPapers.dc.html. Nothing here edits a
// question; it decides who reviews a paper and, once both teachers have
// finished, a Main Assessment Administrator sends it to EdSider from here.

const WITH_TEACHER = ['teacher_review', 'refer_back', 'review_done', 'ready_to_publish', 'published', 'archived']

function gradeText(code) {
  return code ? code.replace(/^G0*/, 'Grade ') : ''
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
  const canSend = !me || !me.role || Boolean(me.masad)
  const sendingOn = !me || !me.role || Boolean(me.edsider_sending)
  const [sending, setSending] = useState(null)
  const [sendProblems, setSendProblems] = useState({})
  const [docs, setDocs] = useState(null)
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

  useEffect(() => {
    let live = true
    api
      .listIntakeDocuments()
      .then((all) => {
        if (live) setDocs(all)
      })
      .catch((err) => {
        if (live) setError(err.message)
      })
    refreshTeachers()
    return () => {
      live = false
    }
  }, [])

  // Each paper goes to two different teachers; it is ready to publish once
  // both have finished their review.
  const pair = (docId) => chosen[docId] || ['', '']
  const choose = (docId, slot, identity) =>
    setChosen((c) => {
      const next = [...pair(docId)]
      next[slot] = identity
      return { ...c, [docId]: next }
    })
  const canAssign = (docId) => {
    const [a, b] = pair(docId)
    return Boolean(a && b && a !== b)
  }

  async function assign(doc) {
    if (!canAssign(doc.id)) return
    const identities = pair(doc.id)
    setBusy((b) => ({ ...b, [doc.id]: true }))
    setError(null)
    setMessage(null)
    try {
      const updated = await api.assignDocument(doc.id, identities)
      setDocs((list) => list.map((d) => (d.id === updated.id ? updated : d)))
      const names = identities.map((id) => teachers.find((t) => t.identity === id)?.display_name || id)
      const text = `#${doc.id} ${doc.paper_subject || doc.filename} assigned to ${names.join(' and ')}.`
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
    const ok = window.confirm(
      `Send #${doc.id} ${doc.paper_subject || doc.filename} to EdSider? It is written into EdSider's database ready to publish and marked Published here - it can no longer be changed.`,
    )
    if (!ok) return
    setSending(doc.id)
    setError(null)
    setMessage(null)
    setSendProblems((p) => ({ ...p, [doc.id]: null }))
    try {
      const sent = await api.sendToEdsider(doc.id)
      setDocs((list) => list.map((d) => (d.id === sent.id ? { ...d, ...sent } : d)))
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
  const assigned = (docs || []).filter((d) => WITH_TEACHER.includes(d.status))
  const nameOf = (identity) => teachers.find((t) => t.identity === identity)?.display_name || identity

  return (
    <>
      <PageHeader
        accent="Assign"
        title="papers to teachers"
        subtitle="Papers data entry has finished, waiting for two teachers to review each one. A paper is ready to publish once both have finished."
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
            {[0, 1].map((slot) => (
              <div className="es-field" key={slot}>
                <label className="es-field__label" htmlFor={`pa-teacher-${d.id}-${slot}`}>
                  {slot === 0 ? 'First teacher' : 'Second teacher'}
                </label>
                <select
                  id={`pa-teacher-${d.id}-${slot}`}
                  className="es-input"
                  value={pair(d.id)[slot]}
                  onChange={(e) => choose(d.id, slot, e.target.value)}
                >
                  <option value="">Choose a teacher</option>
                  {teachers
                    .filter((t) => t.identity !== pair(d.id)[1 - slot])
                    .map((t) => (
                      <option key={t.identity} value={t.identity}>
                        {t.display_name && t.display_name !== t.identity
                          ? `${t.display_name} (${t.identity})`
                          : t.identity}
                      </option>
                    ))}
                </select>
              </div>
            ))}
            <div>
              <Button variant="primary" size="sm" onClick={() => assign(d)} disabled={!canAssign(d.id) || busy[d.id]}>
                {busy[d.id] ? 'Assigning...' : 'Assign to both'}
              </Button>
            </div>
          </article>
        ))}
      </section>

      <h2 className="scr-h2 pa-section">Already assigned</h2>
      <section className="es-card pa-table-card" aria-label="Already assigned">
        {assigned.length === 0 ? (
          <p className="es-caption" style={{ margin: 0 }}>No papers have been handed to a teacher yet.</p>
        ) : (
          <table className="scr-table">
            <thead>
              <tr>
                <th>Paper</th>
                <th>Teachers</th>
                <th>Status</th>
                <th>Uploaded</th>
                <th>Comments</th>
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
                                {(a.teachers_done || []).includes(t) ? '- review done' : '- reviewing'}
                              </span>
                            </div>
                          ))}
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
                  </tr>
                  {sendProblems[a.id] ? (
                    <tr>
                      <td colSpan={5}>
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
                      <td colSpan={5}>
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
      </section>
    </>
  )
}
