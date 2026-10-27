import { useState } from 'react'
import { api } from '../../lib/api.js'
import { joinSoftBreaks } from '../../lib/text.js'

// A question data entry flagged as repeated on this paper. The teacher
// writes a new question in its place - the old one is kept in its history -
// or keeps it, saying why it is not a repeat. Once resolved, says what was
// done, so the paper's other teacher sees it too.
function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 10) : d.toLocaleDateString()
}

export default function RepeatPanel({ q, original, editable, onChanged }) {
  const { Button, Notice } = window.EdSider
  const repeat = q.repeat
  const [mode, setMode] = useState(null) // 'replace' | 'keep'
  const [text, setText] = useState('')
  const [answer, setAnswer] = useState(q.answer || '')
  const [marks, setMarks] = useState(q.marks ?? '')
  const [dropPictures, setDropPictures] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  if (!repeat) return null
  const of = repeat.repeat_of_number ? `Q${repeat.repeat_of_number}` : 'another question'

  if (repeat.status === 'replaced') {
    return (
      <Notice tone="info" title="Replaced with a new question">
        It repeated {of} on this paper; {repeat.resolved_by || 'a teacher'} wrote a new question
        in its place on {when(repeat.resolved_at)}. The earlier question is kept in its history.
      </Notice>
    )
  }
  if (repeat.status === 'kept') {
    return (
      <Notice tone="info" title="Kept - not a repeat">
        Flagged as a repeat of {of}; {repeat.resolved_by || 'a teacher'} kept it on{' '}
        {when(repeat.resolved_at)}: {repeat.resolution_note}
      </Notice>
    )
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      const updated =
        mode === 'replace'
          ? await api.replaceQuestion(q.id, {
              text: text.trim(),
              answer: answer.trim() || null,
              marks: marks === '' ? null : Number(marks),
              reason: `Repeated ${of} on this paper.`,
              keep_pictures: !dropPictures,
            })
          : await api.keepQuestion(q.id, note.trim())
      setMode(null)
      onChanged(updated, mode === 'replace' && dropPictures)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="tr-repeat" role="alert">
      <p className="tr-repeat__title">Repeated in this paper - the same as {of}</p>
      <p className="es-caption" style={{ margin: 0 }}>
        Flagged by data entry{repeat.flagged_by ? ` (${repeat.flagged_by})` : ''} on{' '}
        {when(repeat.flagged_at)}.{repeat.note ? ` "${repeat.note}"` : ''} Write a new question to
        take its place, or keep it if it is not really a repeat.
      </p>
      {original ? (
        <div className="tr-repeat__original">
          <p className="tr-lab">{of} on this paper</p>
          <p className="tr-text">{joinSoftBreaks(original.text)}</p>
        </div>
      ) : null}
      {error ? <Notice tone="error">{error}</Notice> : null}

      {mode === 'replace' ? (
        <div className="tr-repeat__form">
          <label className="es-field__label" htmlFor={`rp-text-${q.id}`}>
            The new question (for an MCQ, its options on their own lines: (1) ... (2) ...)
          </label>
          <textarea id={`rp-text-${q.id}`} className="es-input" rows={6} value={text}
            onChange={(e) => setText(e.target.value)} />
          <label className="es-field__label" htmlFor={`rp-answer-${q.id}`}>
            Its answer (for an MCQ, the correct option, e.g. (2))
          </label>
          <textarea id={`rp-answer-${q.id}`} className="es-input" rows={3} value={answer}
            onChange={(e) => setAnswer(e.target.value)} />
          <div className="tr-repeat__row">
            <label className="es-field__label" htmlFor={`rp-marks-${q.id}`}>Marks</label>
            <input id={`rp-marks-${q.id}`} className="es-input" type="number" min="0" style={{ width: 90 }}
              value={marks} onChange={(e) => setMarks(e.target.value)} />
            <label className="es-check">
              <input type="checkbox" checked={dropPictures} onChange={(e) => setDropPictures(e.target.checked)} />{' '}
              Its pictures belong to the old question
            </label>
          </div>
          <div className="tr-repeat__row">
            <Button variant="primary" size="sm" onClick={save} disabled={busy || !text.trim()}>
              {busy ? 'Saving...' : 'Replace the question'}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setMode(null)} disabled={busy}>Cancel</Button>
          </div>
        </div>
      ) : mode === 'keep' ? (
        <div className="tr-repeat__form">
          <label className="es-field__label" htmlFor={`rp-note-${q.id}`}>
            Why it is not a repeat
          </label>
          <textarea id={`rp-note-${q.id}`} className="es-input" rows={2} value={note}
            onChange={(e) => setNote(e.target.value)} />
          <div className="tr-repeat__row">
            <Button variant="primary" size="sm" onClick={save} disabled={busy || !note.trim()}>
              {busy ? 'Saving...' : 'Keep the question'}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setMode(null)} disabled={busy}>Cancel</Button>
          </div>
        </div>
      ) : editable ? (
        <div className="tr-repeat__row">
          <Button variant="primary" size="sm" onClick={() => setMode('replace')}>Write a new question</Button>
          <Button variant="ghost" size="sm" onClick={() => setMode('keep')}>Keep it - not a repeat</Button>
        </div>
      ) : null}
    </div>
  )
}
