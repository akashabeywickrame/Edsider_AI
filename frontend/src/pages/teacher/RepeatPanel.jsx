import { useState } from 'react'
import { api } from '../../lib/api.js'
import { joinSoftBreaks } from '../../lib/text.js'

// A question data entry flagged as repeated on this paper. The paper's
// owner teacher writes a new question in its place - the old one is kept in its history -
// or keeps it, saying why it is not a repeat. Once resolved, says what was
// done, so the paper's other teacher sees it too.
//
// The new question can be any of the three kinds a paper holds: a written
// answer, a multiple-choice question (its options and the correct one), or
// a question in parts (a stem, then (a), (b)... each with its own marks).

const KINDS = [
  { key: 'descriptive', label: 'Written answer' },
  { key: 'mcq', label: 'Multiple choice' },
  { key: 'sub', label: 'Question with parts' },
]

const blankPart = () => ({ text: '', marks: '', answer: '' })

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 10) : d.toLocaleDateString()
}

export default function RepeatPanel({ q, original, editable, owner, onChanged }) {
  const { Button, Notice } = window.EdSider
  const repeat = q.repeat
  const [mode, setMode] = useState(null) // 'replace' | 'keep'
  // Starts as the kind the question it replaces was.
  const [kind, setKind] = useState(q.mcq ? 'mcq' : 'descriptive')
  const [options, setOptions] = useState(['', '', '', ''])
  const [correct, setCorrect] = useState([])
  const [parts, setParts] = useState([blankPart(), blankPart()])
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
              answer: kind === 'descriptive' ? answer.trim() || null : null,
              marks: marks === '' ? null : Number(marks),
              reason: `Repeated ${of} on this paper.`,
              keep_pictures: !dropPictures,
              kind,
              options: kind === 'mcq' ? options : [],
              correct: kind === 'mcq' ? correct : [],
              parts:
                kind === 'sub'
                  ? parts
                      .filter((pt) => pt.text.trim())
                      .map((pt) => ({
                        text: pt.text.trim(),
                        answer: pt.answer.trim() || null,
                        marks: pt.marks === '' ? null : Number(pt.marks),
                      }))
                  : [],
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
        {when(repeat.flagged_at)}.{repeat.note ? ` "${repeat.note}"` : ''}{' '}
        {owner
          ? `The paper's owner teacher (${owner}) writes a new question to take its place, or keeps it if it is not really a repeat.`
          : 'Write a new question to take its place, or keep it if it is not really a repeat.'}
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
          <fieldset className="tr-kinds">
            <legend className="es-field__label">What kind of question is the new one?</legend>
            {KINDS.map((k) => (
              <label key={k.key} className={`tr-kind${kind === k.key ? ' tr-kind--on' : ''}`}>
                <input type="radio" name={`rp-kind-${q.id}`} checked={kind === k.key}
                  onChange={() => setKind(k.key)} />
                {k.label}
              </label>
            ))}
          </fieldset>

          <label className="es-field__label" htmlFor={`rp-text-${q.id}`}>
            {kind === 'mcq' ? 'The question' : kind === 'sub' ? 'The question (what all its parts share)' : 'The new question'}
          </label>
          <textarea id={`rp-text-${q.id}`} className="es-input" rows={kind === 'descriptive' ? 6 : 3} value={text}
            onChange={(e) => setText(e.target.value)} />

          {kind === 'mcq' ? (
            <div className="tr-repeat__form">
              <span className="es-field__label">Options - tick the correct one</span>
              {options.map((o, i) => (
                <div className="tr-repeat__row" key={i}>
                  <span className="tr-option__key">({i + 1})</span>
                  <input className="es-input" style={{ flex: 1, minWidth: 200 }} value={o}
                    aria-label={`Option ${i + 1}`}
                    onChange={(e) => setOptions((list) => list.map((x, j) => (j === i ? e.target.value : x)))} />
                  <label className="es-check">
                    <input type="checkbox" checked={correct.includes(i)}
                      onChange={(e) => setCorrect((c) => (e.target.checked ? [...c, i] : c.filter((x) => x !== i)))} />{' '}
                    Correct
                  </label>
                  {options.length > 2 ? (
                    <Button variant="ghost" size="sm" onClick={() => {
                      setOptions((list) => list.filter((_, j) => j !== i))
                      setCorrect((c) => c.filter((x) => x !== i).map((x) => (x > i ? x - 1 : x)))
                    }}>Remove</Button>
                  ) : null}
                </div>
              ))}
              <div>
                <Button variant="secondary" size="sm" onClick={() => setOptions((list) => [...list, ''])}>
                  Add an option
                </Button>
              </div>
            </div>
          ) : null}

          {kind === 'sub' ? (
            <div className="tr-repeat__form">
              {parts.map((pt, i) => (
                <div className="tr-part" key={i}>
                  <div className="tr-repeat__row">
                    <span className="tr-lab">Part ({'abcdefghijklmnopqrstuvwxyz'[i]})</span>
                    <label className="es-field__label" htmlFor={`rp-pm-${q.id}-${i}`}>Marks</label>
                    <input id={`rp-pm-${q.id}-${i}`} className="es-input" type="number" min="0" step="any" style={{ width: 90 }}
                      value={pt.marks}
                      onChange={(e) => setParts((list) => list.map((x, j) => (j === i ? { ...x, marks: e.target.value } : x)))} />
                    {parts.length > 1 ? (
                      <Button variant="ghost" size="sm" onClick={() => setParts((list) => list.filter((_, j) => j !== i))}>
                        Remove
                      </Button>
                    ) : null}
                  </div>
                  <textarea className="es-input" rows={2} value={pt.text} aria-label={`Part ${i + 1}`}
                    placeholder="The part's question"
                    onChange={(e) => setParts((list) => list.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))} />
                  <textarea className="es-input" rows={2} value={pt.answer} aria-label={`Part ${i + 1} answer`}
                    placeholder="Its answer (optional)"
                    onChange={(e) => setParts((list) => list.map((x, j) => (j === i ? { ...x, answer: e.target.value } : x)))} />
                </div>
              ))}
              <div>
                <Button variant="secondary" size="sm" onClick={() => setParts((list) => [...list, blankPart()])}>
                  Add a part
                </Button>
              </div>
            </div>
          ) : null}

          {kind === 'descriptive' ? (
            <>
              <label className="es-field__label" htmlFor={`rp-answer-${q.id}`}>
                Its answer
              </label>
              <textarea id={`rp-answer-${q.id}`} className="es-input" rows={3} value={answer}
                onChange={(e) => setAnswer(e.target.value)} />
            </>
          ) : null}

          <div className="tr-repeat__row">
            {kind !== 'sub' ? (
              <>
                <label className="es-field__label" htmlFor={`rp-marks-${q.id}`}>Marks</label>
                <input id={`rp-marks-${q.id}`} className="es-input" type="number" min="0" step="any" style={{ width: 90 }}
                  value={marks} onChange={(e) => setMarks(e.target.value)} />
              </>
            ) : (
              <span className="es-caption">The question's marks are its parts' marks added up.</span>
            )}
            <label className="es-check">
              <input type="checkbox" checked={dropPictures} onChange={(e) => setDropPictures(e.target.checked)} />{' '}
              Its pictures belong to the old question
            </label>
          </div>
          <div className="tr-repeat__row">
            <Button variant="primary" size="sm" onClick={save}
              disabled={busy || !text.trim()
                || (kind === 'mcq' && (options.filter((o) => o.trim()).length < 2 || !correct.length))
                || (kind === 'sub' && !parts.some((pt) => pt.text.trim()))}>
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
