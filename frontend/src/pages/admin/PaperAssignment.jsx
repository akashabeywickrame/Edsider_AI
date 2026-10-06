import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import './PaperAssignment.css'

// Papers data entry has checked for internal repeats, waiting to be handed to
// a teacher - and the ones already with a teacher. Mirrors
// design-system/screens/project/AssignPapers.dc.html. Nothing here edits a
// question; it only decides who reviews a paper.

const WITH_TEACHER = ['teacher_review', 'refer_back', 'review_done', 'published']

function gradeText(code) {
  return code ? code.replace(/^G0*/, 'Grade ') : ''
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleString()
}

export default function PaperAssignment() {
  const { PageHeader, Button, Notice, StatusPill } = window.EdSider
  const toast = useToast()
  const [docs, setDocs] = useState(null)
  const [teachers, setTeachers] = useState([])
  const [chosen, setChosen] = useState({})
  const [busy, setBusy] = useState({})
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    Promise.all([api.listIntakeDocuments(), api.listTeachers()])
      .then(([all, people]) => {
        if (!live) return
        setDocs(all)
        setTeachers(people)
      })
      .catch((err) => {
        if (live) setError(err.message)
      })
    return () => {
      live = false
    }
  }, [])

  async function assign(doc) {
    const identity = chosen[doc.id]
    if (!identity) return
    setBusy((b) => ({ ...b, [doc.id]: true }))
    setError(null)
    setMessage(null)
    try {
      const updated = await api.assignDocument(doc.id, identity)
      setDocs((list) => list.map((d) => (d.id === updated.id ? updated : d)))
      const who = teachers.find((t) => t.identity === identity)
      const text = `#${doc.id} ${doc.paper_subject || doc.filename} assigned to ${who?.display_name || identity}.`
      setMessage(text)
      toast.success(text)
    } catch (err) {
      setError(err.message)
      toast.error(`Assignment failed: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [doc.id]: false }))
    }
  }

  if (docs === null && !error) return <Notice tone="info">Loading...</Notice>

  const ready = (docs || []).filter((d) => d.status === 'checked')
  const assigned = (docs || []).filter((d) => WITH_TEACHER.includes(d.status))
  const nameOf = (identity) => teachers.find((t) => t.identity === identity)?.display_name || identity

  return (
    <>
      <PageHeader
        accent="Assign"
        title="papers to teachers"
        subtitle="Papers data entry has already checked for internal repeats, waiting to be handed to a teacher for subject review."
      />

      {error ? (
        <Notice tone="error" title="Could not load the papers">
          {error}
        </Notice>
      ) : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      <div className="pa-head">
        <h2 className="scr-h2">Ready to assign ({ready.length})</h2>
        <p className="es-caption">Teachers appear here after they first open the Teacher review app.</p>
      </div>

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
                Assign to
              </label>
              <select
                id={`pa-teacher-${d.id}`}
                className="es-input"
                value={chosen[d.id] || ''}
                onChange={(e) => setChosen((c) => ({ ...c, [d.id]: e.target.value }))}
              >
                <option value="">Choose a teacher</option>
                {teachers.map((t) => (
                  <option key={t.identity} value={t.identity}>
                    {t.display_name || t.identity}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Button variant="primary" size="sm" onClick={() => assign(d)} disabled={!chosen[d.id] || busy[d.id]}>
                {busy[d.id] ? 'Assigning...' : 'Assign'}
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
                <th>Teacher</th>
                <th>Status</th>
                <th>Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {assigned.map((a) => (
                <tr key={a.id}>
                  <td>
                    <span className="pa-bold">
                      {a.paper_subject || 'Subject not set'} - {gradeText(a.paper_grade) || 'Grade not set'}
                    </span>
                    <br />
                    <span className="es-caption">
                      #{a.id} - {a.filename}
                    </span>
                  </td>
                  <td>{a.assigned_to ? nameOf(a.assigned_to) : '-'}</td>
                  <td>
                    <StatusPill status={a.status} />
                  </td>
                  <td className="es-caption">{when(a.uploaded_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  )
}
