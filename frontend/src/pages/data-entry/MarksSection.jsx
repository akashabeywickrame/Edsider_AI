import { useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'

// Marks for a paper, set part by part. Each question's own marks are the
// figures that count; the part's total is what a student can earn from it -
// the best "questions to answer" marks, so a part where the student picks
// some questions is worth the picked ones, and an answer-all part is worth
// every question. The paper's total is the sum of its parts.

const KIND_LABEL = { mcq: 'Multiple choice', structured: 'Structured / essay' }

// The questions that make up one part of the paper (top-level, not parts).
export function partQuestions(questions, sec) {
  return questions.filter((q) => !q.depth && (q.section ?? null) === (sec.label ?? null))
}

// What a student can earn from a part: the largest `answerCount` marks.
export function partTotal(qs, answerCount) {
  const marks = qs.map((q) => (q.marks == null ? 0 : Number(q.marks))).sort((a, b) => b - a)
  return marks.slice(0, answerCount).reduce((sum, m) => sum + m, 0)
}

function explain(label, n, answerCount, total) {
  const what = answerCount >= n ? `answer all ${n} question(s)` : `answer ${answerCount} of ${n} question(s)`
  return `${label}: ${what} - ${total} marks`
}

export default function MarksSection({ doc, questions, onQuestionChanged, onSaved }) {
  const { Button, Notice } = window.EdSider
  const toast = useToast()
  const saved = doc.assessment_settings?.marking || {}
  const suggested = doc.suggested_settings?.parts_marks || []
  const parts = doc.sections || []

  // Starting values: what the data-entry person saved, else what the paper
  // states, else the questions as they stand.
  const [plan, setPlan] = useState(() =>
    Object.fromEntries(
      parts.map((sec) => {
        const qs = partQuestions(questions, sec)
        const sug = suggested.find((p) => p.label === sec.label) || {}
        const mine = saved.parts?.[sec.label] || {}
        return [
          sec.label ?? '_',
          {
            // How many questions the paper has in this part. The extractor's count
            // can be wrong when question numbers did not read - the paper's own
            // count is what the marks are worked out from.
            question_count: mine.question_count ?? qs.length,
            answer_count: mine.answer_count ?? sug.answer_count ?? qs.length,
            marks_each: mine.marks_each ?? sug.marks_each ?? qs[0]?.marks ?? '',
          },
        ]
      }),
    ),
  )
  const [notes, setNotes] = useState(saved.notes ?? '')
  // The total the paper should show, when it differs from its own marks - a
  // 200-mark paper set up to 100, say. Blank keeps the paper's own total.
  const [target, setTarget] = useState(saved.target_total ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const keyOf = (sec) => sec.label ?? '_'
  const setPart = (sec, patch) =>
    setPlan((p) => ({ ...p, [keyOf(sec)]: { ...p[keyOf(sec)], ...patch } }))

  // Sets every question in a part to the same marks.
  async function applyToPart(sec, qs) {
    const each = Number(plan[keyOf(sec)].marks_each)
    if (!Number.isFinite(each) || each < 0) return
    setBusy(true)
    setError(null)
    try {
      // One request for the whole part, however many questions it has.
      const updated = await api.setQuestionMarks(doc.id, qs.map((q) => ({ question_id: q.id, marks: each })))
      updated.filter((u) => qs.some((q) => q.id === u.id)).forEach(onQuestionChanged)
      toast.success(`${sec.label || 'Part'}: ${qs.length} question(s) set to ${each} mark(s).`)
    } catch (err) {
      setError(err.message)
      toast.error(`Marks not set: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function setOneQuestion(q, value) {
    const next = value === '' ? null : Number(value)
    if (next === q.marks || (next == null && q.marks == null)) return
    try {
      const updated = await api.updateIntakeQuestion(q.id, { marks: next })
      onQuestionChanged(updated)
    } catch (err) {
      setError(err.message)
    }
  }

  // The totals as they stand, from the question marks on screen.
  const rows = parts.map((sec) => {
    const qs = partQuestions(questions, sec)
    const p = plan[keyOf(sec)]
    const count = Math.max(Number(p.question_count) || qs.length, 0)
    const answerCount = Math.min(Math.max(Number(p.answer_count) || 0, 0), count)
    // When the paper's count differs from the questions found, the questions'
    // own marks cannot be trusted - the total comes from marks per question.
    const countMismatch = count !== qs.length
    const total = countMismatch
      ? answerCount * (Number(p.marks_each) || 0)
      : partTotal(qs, answerCount)
    const unmarked = qs.filter((q) => q.marks == null).length
    return { sec, qs, count, answerCount, total, unmarked, countMismatch }
  })
  const grandTotal = rows.reduce((sum, r) => sum + r.total, 0)
  // The paper's own total is the sum of its marks. A target scales every
  // mark by the same factor; the questions keep the paper's own marks.
  const targetNumber = Number(target)
  const scale = targetNumber > 0 && grandTotal > 0 ? targetNumber / grandTotal : null
  const shownTotal = scale ? targetNumber : grandTotal
  const stated = doc.suggested_settings?.total_marks

  const autoNotes = rows.map((r) => explain(r.sec.label || 'Paper', r.count, r.answerCount, r.total))
  const notesText = notes.trim() ? notes : autoNotes.join('\n') + `\nTotal: ${grandTotal} marks`

  async function saveMarking() {
    setBusy(true)
    setError(null)
    try {
      const marking = {
        parts: Object.fromEntries(
          rows.map((r) => [
            keyOf(r.sec),
            {
              question_count: r.count,
              answer_count: r.answerCount,
              marks_each: plan[keyOf(r.sec)].marks_each === '' ? null : Number(plan[keyOf(r.sec)].marks_each),
            },
          ]),
        ),
        notes,
        total: grandTotal,
        target_total: targetNumber > 0 ? targetNumber : null,
        scale,
      }
      const updated = await api.updateIntakeDocument(doc.id, {
        assessment_settings: { ...(doc.assessment_settings || {}), marking },
      })
      onSaved(updated)
      toast.success(`Marks saved - total ${grandTotal}.`)
    } catch (err) {
      setError(err.message)
      toast.error(`Marks not saved: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  if (parts.length === 0) {
    return (
      <section className="es-card" aria-label="Marks">
        <h2 className="scr-h2">Marks</h2>
        <Notice tone="info">This paper has no parts yet - split it into parts to set marks.</Notice>
      </section>
    )
  }

  return (
    <section className="es-card" aria-label="Marks" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <h2 className="scr-h2">Marks</h2>
      <p className="es-caption" style={{ margin: 0 }}>
        Set the marks for each part. Each question's marks can be set here or on its own card, and the total is worked out
        from them.
      </p>

      {rows.map(({ sec, qs, count, answerCount, total, unmarked, countMismatch }) => {
        const key = keyOf(sec)
        const each = plan[key].marks_each
        return (
          <div key={key} style={{ border: '1px solid var(--border-soft)', borderRadius: 14, padding: 18 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
              <p className="scr-h3" style={{ margin: 0 }}>
                {sec.label || 'Paper'}
                {sec.heading ? <span className="es-caption"> - {sec.heading}</span> : null}
              </p>
              <span className="es-caption">
                {KIND_LABEL[sec.kind] || 'Mixed'} - {count} question(s)
              </span>
            </div>

            <div className="pi-row" style={{ marginTop: 12, alignItems: 'flex-end' }}>
              <div className="es-field" style={{ minWidth: 180 }}>
                <label className="es-field__label" htmlFor={`mk-each-${key}`}>
                  Marks per question
                </label>
                <input
                  id={`mk-each-${key}`}
                  className="es-input"
                  type="number"
                  min="0"
                  value={each}
                  onChange={(e) => setPart(sec, { marks_each: e.target.value })}
                />
              </div>
              <Button variant="secondary" size="sm" onClick={() => applyToPart(sec, qs)} disabled={busy || !qs.length}>
                Apply to all questions
              </Button>

              <div className="es-field" style={{ minWidth: 180 }}>
                <label className="es-field__label" htmlFor={`mk-count-${key}`}>
                  Questions in this part
                </label>
                <input
                  id={`mk-count-${key}`}
                  className="es-input"
                  type="number"
                  min="1"
                  value={plan[key].question_count}
                  onChange={(e) => setPart(sec, { question_count: e.target.value })}
                />
                {countMismatch ? (
                  <span className="es-caption" style={{ color: '#b45309' }}>
                    The extractor found {qs.length} here - check the question numbers against the paper.
                  </span>
                ) : null}
              </div>

              {sec.kind !== 'mcq' ? (
                <div className="es-field" style={{ minWidth: 220 }}>
                  <label className="es-field__label" htmlFor={`mk-answer-${key}`}>
                    Questions to answer
                  </label>
                  <select
                    id={`mk-answer-${key}`}
                    className="es-input"
                    value={answerCount}
                    onChange={(e) => setPart(sec, { answer_count: Number(e.target.value) })}
                  >
                    {Array.from({ length: count }, (_, i) => i + 1).map((n) => (
                      <option key={n} value={n}>
                        {n === count ? `All ${n}` : n}
                      </option>
                    ))}
                  </select>
                  {answerCount < count ? (
                    <span className="es-caption">
                      {count - answerCount} optional - the student picks {answerCount}.
                    </span>
                  ) : null}
                </div>
              ) : null}
            </div>

            {qs.length ? (
              <div className="pi-marks-grid">
                {qs.map((q) => (
                  <label className="es-field" key={q.id}>
                    <span className="es-field__label">Q{q.question_number || q.seq} marks</span>
                    <input
                      className="es-input"
                      type="number"
                      min="0"
                      defaultValue={q.marks ?? ''}
                      key={`${q.id}-${q.marks ?? ''}`}
                      onBlur={(e) => setOneQuestion(q, e.target.value)}
                    />
                  </label>
                ))}
              </div>
            ) : null}

            <p style={{ margin: '12px 0 0', fontWeight: 600 }}>
              {explain(sec.label || 'Paper', count, answerCount, total)}
              {unmarked ? <span className="es-caption"> - {unmarked} question(s) have no marks yet</span> : null}
            </p>
          </div>
        )
      })}

      <div className="es-card" style={{ background: 'var(--surface-sunk)', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <p className="es-field__label" style={{ margin: 0 }}>Total marks</p>
        <p className="scr-h2" style={{ margin: 0, color: 'var(--primary)' }}>{shownTotal}</p>
        {scale ? (
          <p className="es-caption" style={{ margin: 0 }}>
            The paper's own marks add to {grandTotal}, scaled by {scale.toFixed(3)} to {targetNumber}.
          </p>
        ) : null}
        <div className="pi-row" style={{ alignItems: 'flex-end', marginTop: 8 }}>
          <div className="es-field" style={{ minWidth: 220 }}>
            <label className="es-field__label" htmlFor="mk-target">
              Paper total to show (optional)
            </label>
            <input
              id="mk-target"
              className="es-input"
              type="number"
              min="1"
              placeholder={`${grandTotal} - the paper's own total`}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
            <span className="es-caption">For a paper set up to a different total, such as 200 marks as 100.</span>
          </div>
        </div>
        {stated != null && stated !== grandTotal ? (
          <p className="es-caption" style={{ margin: 0 }}>
            The paper states {stated} - check the marks above against the paper.
          </p>
        ) : null}
      </div>

      <div className="es-field">
        <label className="es-field__label" htmlFor="mk-notes">
          How the marks are calculated
        </label>
        <textarea
          id="mk-notes"
          className="es-input"
          rows={4}
          value={notes}
          placeholder={notesText}
          onChange={(e) => setNotes(e.target.value)}
        />
        <span className="es-caption">
          Leave this empty to use the calculation written above, or add the marking details here.
        </span>
      </div>

      {error ? (
        <Notice tone="error" title="Could not save the marks">
          {error}
        </Notice>
      ) : null}
      <div>
        <Button variant="primary" onClick={saveMarking} disabled={busy}>
          {busy ? 'Saving...' : 'Save marks'}
        </Button>
      </div>
    </section>
  )
}
