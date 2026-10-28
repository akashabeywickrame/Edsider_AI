import { useMemo, useState } from 'react'
import { api } from '../../lib/api.js'
import { partChoices, partParents, wholeQuestionChoices } from './addQuestionChoices.js'

// "Add a question the extractor missed" - chosen from dropdowns only. The
// number is never typed: it comes from the paper (a gap, the next number, or
// the next part label in the paper's own style), and the question is placed
// where it belongs. See addQuestionChoices.js.
export default function AddQuestion({ docId, tree, parts, onAdded }) {
  const { Button, Notice } = window.EdSider
  const [kind, setKind] = useState('whole')
  const [wholeIndex, setWholeIndex] = useState('')
  const [parentSeq, setParentSeq] = useState('')
  const [partIndex, setPartIndex] = useState('')
  const [text, setText] = useState('')
  const [marks, setMarks] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const wholeChoices = useMemo(() => wholeQuestionChoices(tree, parts), [tree, parts])
  const parents = useMemo(() => partParents(tree), [tree])
  const parent = parents.find((q) => String(q.seq) === parentSeq) || null
  const labelChoices = useMemo(() => (parent ? partChoices(tree, parent) : []), [tree, parent])

  const chosen =
    kind === 'whole' ? wholeChoices[Number(wholeIndex)] : parent ? labelChoices[Number(partIndex)] : undefined
  const ready = Boolean(chosen && (kind === 'whole' ? wholeIndex !== '' : partIndex !== '') && text.trim())

  function reset() {
    setText('')
    setMarks('')
    setWholeIndex('')
    setPartIndex('')
  }

  async function submit(e) {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setError(null)
    try {
      await api.createIntakeQuestion(docId, {
        text,
        question_number: chosen.number,
        marks: marks === '' ? undefined : Number(marks),
        parent_seq: kind === 'part' ? parent.seq : undefined,
        section: kind === 'whole' ? chosen.section || undefined : undefined,
        after_seq: chosen.afterSeq,
      })
      reset()
      // The questions after it moved down one place, so the whole list is
      // read again rather than this one appended at the end.
      await onAdded(chosen.number)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="pi-panel" onSubmit={submit}>
      <div className="pi-add__grid">
        <div className="es-field">
          <label className="es-field__label" htmlFor="add-kind">
            What are you adding?
          </label>
          <select
            id="add-kind"
            className="es-input"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value)
              setWholeIndex('')
              setPartIndex('')
            }}
          >
            <option value="whole">A whole question</option>
            <option value="part">A part of a question</option>
          </select>
        </div>

        {kind === 'whole' ? (
          <div className="es-field">
            <label className="es-field__label" htmlFor="add-where">
              Which question is it?
            </label>
            <select id="add-where" className="es-input" value={wholeIndex} onChange={(e) => setWholeIndex(e.target.value)}>
              <option value="">Choose...</option>
              {wholeChoices.map((c, i) => (
                <option key={`${c.section}-${c.number}-${i}`} value={i}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <>
            <div className="es-field">
              <label className="es-field__label" htmlFor="add-parent">
                Part of which question?
              </label>
              <select
                id="add-parent"
                className="es-input"
                value={parentSeq}
                onChange={(e) => {
                  setParentSeq(e.target.value)
                  setPartIndex('')
                }}
              >
                <option value="">Choose...</option>
                {parents.map((q) => (
                  <option key={q.seq} value={q.seq}>
                    {'  '.repeat(q.depth || 0)}
                    {q.section ? `${q.section} ` : ''}Q{q.question_number || q.seq}: {(q.text || '').slice(0, 50)}
                  </option>
                ))}
              </select>
            </div>
            <div className="es-field">
              <label className="es-field__label" htmlFor="add-label">
                Which part is it?
              </label>
              <select
                id="add-label"
                className="es-input"
                value={partIndex}
                disabled={!parent}
                onChange={(e) => setPartIndex(e.target.value)}
              >
                <option value="">{parent ? 'Choose...' : 'Choose the question first'}</option>
                {labelChoices.map((c, i) => (
                  <option key={c.number} value={i}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}

        <div className="es-field">
          <label className="es-field__label" htmlFor="add-marks">
            Marks
          </label>
          <input
            id="add-marks"
            className="es-input"
            type="number"
            min="0"
            step="any"
            value={marks}
            onChange={(e) => setMarks(e.target.value)}
          />
        </div>
      </div>

      <div className="es-field">
        <label className="es-field__label" htmlFor="add-text">
          Question text
        </label>
        <textarea id="add-text" className="es-input" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
      </div>

      {chosen ? (
        <p className="es-caption" style={{ margin: 0 }}>
          It will be added as {kind === 'whole' ? 'question' : 'part'} <strong>{chosen.number}</strong>, in its place on
          the paper.
        </p>
      ) : null}

      {error ? (
        <Notice tone="error" title="Could not add this question">
          {error}
        </Notice>
      ) : null}
      <div>
        <Button variant="primary" type="submit" disabled={!ready || busy}>
          {busy ? 'Adding...' : kind === 'whole' ? 'Add question' : 'Add part'}
        </Button>
      </div>
    </form>
  )
}
