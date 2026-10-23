import { useState } from 'react'
import { api } from '../../lib/api.js'

// What sending this paper to EdSider would create - its paper, parts,
// sections, questions and links, in EdSider's own columns - and everything
// that would stop it. Nothing is sent from here (GET .../edsider-preview,
// src/edsider/export.py). For administrators: they send papers.

const ARRIVAL = [
  { key: 'ReviewCompleted', label: 'Review completed (suggested - our teacher has reviewed it)' },
  { key: 'ReadyToPublish', label: 'Ready to publish' },
  { key: 'Ready', label: 'Ready' },
  { key: 'Initial', label: 'Initial' },
]
const QTYPE = { 1: 'MCQ', 2: 'Descriptive', 5: 'Question with parts' }
const SHOWN = 60

export default function EdsiderPreview({ doc }) {
  const { Button, Notice } = window.EdSider
  const [examNumber, setExamNumber] = useState('')
  const [arrival, setArrival] = useState('ReviewCompleted')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.edsiderPreview(doc.id, { arrival_state: arrival, exam_number: examNumber }))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function download() {
    const blob = new Blob([JSON.stringify(result.package, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `edsider-paper-${doc.id}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const pkg = result?.package
  const errors = (result?.problems || []).filter((p) => p.level === 'error')
  const warnings = (result?.problems || []).filter((p) => p.level === 'warning')
  const rows = Object.fromEntries((pkg?.questions || []).map((q) => [q.key, q]))
  const sectionTitle = {}
  ;(pkg?.parts || []).forEach((p) =>
    p.sections.forEach((s) => {
      sectionTitle[s.key] = `${p.row.part_title} - ${s.row.section_title}`
    }),
  )
  const where = (p) => (p.question_number ? `Question ${p.question_number}: ` : '')

  return (
    <section className="es-card" aria-label="Preview for EdSider" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <h2 className="scr-h2">Preview for EdSider</h2>
      <p className="es-caption" style={{ margin: 0 }}>
        Exactly what sending this paper would create in EdSider, and anything that would stop it. Nothing is sent from here.
      </p>
      <div className="es-row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div className="es-field" style={{ minWidth: 140 }}>
          <label className="es-field__label" htmlFor="ep-exam">
            Exam number
          </label>
          <input
            id="ep-exam"
            className="es-input"
            type="number"
            min="1"
            value={examNumber}
            placeholder="e.g. 2"
            onChange={(e) => setExamNumber(e.target.value)}
          />
        </div>
        <div className="es-field" style={{ minWidth: 260 }}>
          <label className="es-field__label" htmlFor="ep-state">
            Arrives in EdSider as
          </label>
          <select id="ep-state" className="es-input" value={arrival} onChange={(e) => setArrival(e.target.value)}>
            {ARRIVAL.map((a) => (
              <option key={a.key} value={a.key}>
                {a.label}
              </option>
            ))}
          </select>
        </div>
        <Button variant="secondary" size="sm" onClick={run} disabled={busy}>
          {busy ? 'Building...' : result ? 'Build again' : 'Build the preview'}
        </Button>
        {pkg ? (
          <Button variant="ghost" size="sm" onClick={download}>
            Download as JSON
          </Button>
        ) : null}
      </div>

      {error ? (
        <Notice tone="error" title="Could not build the preview">
          {error}
        </Notice>
      ) : null}

      {result ? (
        <>
          <p style={{ margin: 0 }}>
            <b>{result.summary.title}</b> - {result.summary.parts} part(s), {result.summary.sections} section(s),{' '}
            {result.summary.questions} question(s) written as {result.summary.rows} EdSider question row(s),{' '}
            {result.summary.marks} marks.
          </p>
          {errors.length ? (
            <Notice tone="error" title={`${errors.length} thing(s) to fix before it can be sent`}>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {errors.map((p, i) => (
                  <li key={i}>
                    {where(p)}
                    {p.message}
                  </li>
                ))}
              </ul>
            </Notice>
          ) : (
            <Notice tone="success">Nothing stops this paper from being sent.</Notice>
          )}
          {warnings.length ? (
            <Notice tone="warning" title={`${warnings.length} thing(s) to check`}>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {warnings.map((p, i) => (
                  <li key={i}>
                    {where(p)}
                    {p.message}
                  </li>
                ))}
              </ul>
            </Notice>
          ) : null}

          <h3 className="scr-h3" style={{ margin: 0 }}>Parts and sections</h3>
          <table className="scr-table">
            <thead>
              <tr>
                <th>Part</th>
                <th>Section</th>
                <th>Questions</th>
                <th>Answer</th>
                <th>Marks</th>
                <th>Instructions</th>
              </tr>
            </thead>
            <tbody>
              {pkg.parts.flatMap((p) =>
                p.sections.map((s) => (
                  <tr key={s.key}>
                    <td>{p.row.part_title}</td>
                    <td>{s.row.section_title}</td>
                    <td>{s.row.no_of_questions}</td>
                    <td>{s.row.allow_optional_questions ? `any ${s.row.no_of_questions_to_answer}` : 'all'}</td>
                    <td>{s.marks}</td>
                    <td className="es-caption">{(s.row.section_instruction || '').slice(0, 120)}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>

          <h3 className="scr-h3" style={{ margin: 0 }}>Questions as EdSider will number them</h3>
          <table className="scr-table">
            <thead>
              <tr>
                <th>Code</th>
                <th>Section</th>
                <th>No.</th>
                <th>Type</th>
                <th>Marks</th>
                <th>Answer</th>
              </tr>
            </thead>
            <tbody>
              {pkg.links.slice(0, SHOWN).map((l) => {
                const r = rows[l.question_key]
                const isParent = Boolean(r?.sub_question_keys)
                return (
                  <tr key={l.question_key}>
                    <td className="es-caption">{r?.title}</td>
                    <td className="es-caption">{sectionTitle[l.section_key]}</td>
                    <td>{l.row.question_number}</td>
                    <td>{isParent ? 'Question stem' : r?.is_subquestion ? 'Part' : QTYPE[r?.qtype]}</td>
                    <td>{l.row.mark_allocation ?? '-'}</td>
                    <td className="es-caption">{r?.qtype === 1 ? r.answer : isParent ? '-' : 'model answer'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {pkg.links.length > SHOWN ? (
            <p className="es-caption" style={{ margin: 0 }}>
              And {pkg.links.length - SHOWN} more - download the JSON for every row.
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  )
}
