import { useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import { roundMarks } from '../../lib/numbers.js'
import { confirmDialog } from '../../components/ConfirmDialog.jsx'

// Marks for a paper, set part by part. Each question's own marks are the
// figures that count; the part's total is what a student can earn from it -
// the best "questions to answer" marks, so a part where the student picks
// some questions is worth the picked ones, and an answer-all part is worth
// every question. The paper's total is the sum of its parts.
//
// A part divided into sections ("Part II - A", "Part II - B") can also say
// how many to answer in all: "at least 2 from each section, 5 in all". That
// is EdSider's own rule (optionalQuestionScoring.js) - each section's count
// first, then the best of the rest from any section - so the totals here are
// worked out the same way EdSider will mark them.

const KIND_LABEL = { mcq: 'Multiple choice', structured: 'Structured / essay' }

// The questions that make up one part of the paper (top-level, not parts).
export function partQuestions(questions, sec) {
  return questions.filter((q) => !q.depth && (q.section ?? null) === (sec.label ?? null))
}

// "Part II: answer Q01 (compulsory) and any 3 of the other 4 - 40 marks"
function explain(label, n, answerCount, total, compulsory = []) {
  let what
  if (answerCount >= n) what = `answer all ${n} question(s)`
  else if (compulsory.length) {
    const named = compulsory.map((q) => `Q${q.question_number || q.seq}`).join(', ')
    const more = answerCount - compulsory.length
    what = more > 0
      ? `answer ${named} (compulsory) and any ${more} of the other ${n - compulsory.length}`
      : `answer ${named} (compulsory)`
  } else what = `answer ${answerCount} of ${n} question(s)`
  return `${label}: ${what} - ${total} marks`
}

// A section of a part, as the extractor and the EdSider converter name one
// (src/edsider/export.py's _SUB_PART).
const SECTION_LABEL = /^(.+?)\s*[-\u2013\u2014]\s*([A-Z])$/

// The paper's parts as EdSider's: "Part II - A" and "Part II - B" are one
// part with two sections; any other part is a part of its own.
export function groupParts(parts) {
  const groups = []
  for (const sec of parts) {
    const m = SECTION_LABEL.exec(sec.label || '')
    const title = m ? m[1].trim() : sec.label
    const last = groups[groups.length - 1]
    if (m && last && last.sub && last.title === title) last.sections.push(sec)
    else groups.push({ title, sub: Boolean(m), sections: [sec] })
  }
  return groups
}

// What a student can earn from a part, as EdSider marks it: in each
// section its compulsory questions (`required` - their marks) and the best of
// the others up to `answerCount` in all (every question when all are
// answered), then the best of what is left anywhere in the part until
// `inAll` are counted.
export function groupTotal(sections, inAll) {
  let total = 0
  let counted = 0
  const rest = []
  for (const { marks, answerCount, required = [] } of sections) {
    const sorted = [...marks].sort((a, b) => b - a)
    const picking = answerCount < sorted.length + required.length
    const take = picking ? Math.max(0, answerCount - required.length) : sorted.length
    const sum = (list) => list.reduce((acc, m) => acc + m, 0)
    total += sum(required) + sum(sorted.slice(0, take))
    counted += required.length + Math.min(take, sorted.length)
    rest.push(...sorted.slice(take))
  }
  const extra = Math.max(0, (Number(inAll) || 0) - counted)
  total += rest.sort((a, b) => b - a).slice(0, extra).reduce((sum, m) => sum + m, 0)
  return roundMarks(total)
}

export default function MarksSection({ doc, questions, onQuestionChanged, onSaved, onSectionsChanged }) {
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
            // On a part's first section: how many to answer across all its
            // sections. Blank is just each section's own count.
            group_answer_count: mine.group_answer_count ?? '',
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
  // Dividing a part into sections: the part, and the question each new
  // section starts at.
  const [dividing, setDividing] = useState(null)
  const [starts, setStarts] = useState([''])

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

  // "Question 1 must be answered, then any 3 of the rest."
  async function setCompulsory(q, compulsory) {
    try {
      onQuestionChanged(await api.updateIntakeQuestion(q.id, { compulsory }))
    } catch (err) {
      setError(err.message)
      toast.error(`Not changed: ${err.message}`)
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
  const groups = groupParts(parts)
  const rowOf = {}
  const rows = parts.map((sec) => {
    const qs = partQuestions(questions, sec)
    const p = plan[keyOf(sec)]
    const count = Math.max(Number(p.question_count) || qs.length, 0)
    const answerCount = Math.min(Math.max(Number(p.answer_count) || 0, 0), count)
    // When the paper's count differs from the questions found, the questions'
    // own marks cannot be trusted - the total comes from marks per question.
    const countMismatch = count !== qs.length
    // Compulsory questions always count; the student picks among the rest.
    const compulsory = countMismatch ? [] : qs.filter((q) => q.compulsory)
    const markOf = (q) => (q.marks == null ? 0 : Number(q.marks))
    const marks = countMismatch
      ? Array.from({ length: count }, () => Number(p.marks_each) || 0)
      : qs.filter((q) => !q.compulsory).map(markOf)
    const required = compulsory.map(markOf)
    const total = groupTotal([{ marks, answerCount, required }], 0)
    const unmarked = qs.filter((q) => q.marks == null).length
    const row = { sec, qs, count, answerCount, total, unmarked, countMismatch, marks, required, compulsory }
    rowOf[keyOf(sec)] = row
    return row
  })
  // Each EdSider part: its sections, how many in all, and what it is worth.
  const groupRows = groups.map((g) => {
    const secRows = g.sections.map((sec) => rowOf[keyOf(sec)])
    const first = keyOf(g.sections[0])
    const least = secRows.reduce((sum, r) => sum + Math.min(r.answerCount, r.count), 0)
    const most = secRows.reduce((sum, r) => sum + r.count, 0)
    const asked = Number(plan[first].group_answer_count) || 0
    const inAll = g.sections.length > 1 && asked > least ? Math.min(asked, most) : 0
    return { ...g, secRows, first, least, most, inAll, total: groupTotal(secRows, inAll) }
  })
  const grandTotal = roundMarks(groupRows.reduce((sum, g) => sum + g.total, 0))
  // The paper's own total is the sum of its marks. A target scales every
  // mark by the same factor; the questions keep the paper's own marks.
  const targetNumber = Number(target)
  const scale = targetNumber > 0 && grandTotal > 0 ? targetNumber / grandTotal : null
  const shownTotal = scale ? targetNumber : grandTotal
  const stated = doc.suggested_settings?.total_marks

  const autoNotes = groupRows.flatMap((g) => [
    ...g.secRows.map((r) => explain(r.sec.label || 'Paper', r.count, r.answerCount, r.total, r.compulsory)),
    ...(g.inAll ? [inAllText(g)] : []),
  ])
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
              group_answer_count: groupRows.find((g) => g.first === keyOf(r.sec))?.inAll || null,
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

  // "Part II: answer 5 in all - 2 from Part II - A, 2 from Part II - B, then 1 more from any section"
  function inAllText(g) {
    const each = g.secRows.map((r) => `${r.answerCount >= r.count ? 'all' : r.answerCount} from ${r.sec.label}`)
    const more = g.inAll - g.least
    return `${g.title}: answer ${g.inAll} in all - ${each.join(', ')}, then ${more} more from any section - ${g.total} marks`
  }

  async function divide(sec) {
    const ids = starts.filter(Boolean).map(Number)
    if (!ids.length) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.splitSections(doc.id, sec.label ?? null, ids)
      toast.success(`${sec.label || 'The paper'} divided into ${ids.length + 1} sections.`)
      setDividing(null)
      await onSectionsChanged(updated)
    } catch (err) {
      setError(err.message)
      toast.error(`Not divided: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function join(title) {
    if (!(await confirmDialog(`Join the sections of ${title} back into one part?`))) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.joinSections(doc.id, title)
      toast.success(`${title} is one part again.`)
      await onSectionsChanged(updated)
    } catch (err) {
      setError(err.message)
      toast.error(`Not joined: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  // Where each new section starts - any question of the part after its first.
  function divideForm(sec, qs) {
    const later = qs.slice(1)
    return (
      <div style={{ display: 'grid', gap: 8, marginTop: 12, padding: 12, background: 'var(--surface-sunk)', borderRadius: 10 }}>
        <p className="es-caption" style={{ margin: 0 }}>
          For a part whose questions are in sections - "answer at least 2 from Section A and 2 from Section B". Choose the
          question each section after the first starts at; each question keeps its own parts.
        </p>
        {starts.map((value, i) => (
          <div className="es-field" key={i} style={{ maxWidth: 360 }}>
            <label className="es-field__label" htmlFor={`mk-start-${i}`}>
              Section {String.fromCharCode(66 + i)} starts at
            </label>
            <select
              id={`mk-start-${i}`}
              className="es-input"
              value={value}
              onChange={(e) => setStarts((list) => list.map((v, j) => (j === i ? e.target.value : v)))}
            >
              <option value="">Choose a question</option>
              {later.map((q) => (
                <option key={q.id} value={q.id}>
                  Q{q.question_number || q.seq} - {(q.text || '').slice(0, 60)}
                </option>
              ))}
            </select>
          </div>
        ))}
        <div className="es-row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <Button variant="ghost" size="sm" onClick={() => setStarts((list) => [...list, ''])} disabled={starts.length >= later.length}>
            Add another section
          </Button>
          <Button variant="primary" size="sm" onClick={() => divide(sec)} disabled={busy || !starts.some(Boolean)}>
            {busy ? 'Dividing...' : 'Divide into sections'}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setDividing(null)}>
            Cancel
          </Button>
        </div>
      </div>
    )
  }

  function divideButton(sec) {
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          setDividing(keyOf(sec))
          setStarts([''])
        }}
      >
        Divide into sections
      </Button>
    )
  }

  if (parts.length === 0) {
    const whole = { label: null }
    const qs = partQuestions(questions, whole)
    return (
      <section className="es-card" aria-label="Marks">
        <h2 className="scr-h2">Marks</h2>
        <Notice tone="info">This paper has no parts yet - split it into parts to set marks, or divide it into sections.</Notice>
        {error ? <Notice tone="error">{error}</Notice> : null}
        {qs.length > 1 ? (dividing === keyOf(whole) ? divideForm(whole, qs) : divideButton(whole)) : null}
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

      {groupRows.map((g) => (
      <div key={g.first} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {g.sections.length > 1 ? (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
          <p className="scr-h3" style={{ margin: 0 }}>
            {g.title} <span className="es-caption">- {g.sections.length} sections</span>
          </p>
          <Button variant="ghost" size="sm" onClick={() => join(g.title)} disabled={busy}>
            Join the sections
          </Button>
        </div>
      ) : null}
      {g.secRows.map(({ sec, qs, count, answerCount, total, unmarked, countMismatch, compulsory }) => {
        const key = keyOf(sec)
        const each = plan[key].marks_each
        // In a part divided into sections, a section may ask for none: its
        // questions are only picked from by the part's "in all" count.
        const lowest = Math.max(g.sections.length > 1 ? 0 : 1, compulsory.length)
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
                  step="any"
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
                    {Array.from({ length: count - lowest + 1 }, (_, i) => i + lowest).map((n) => (
                      <option key={n} value={n}>
                        {n === count ? `All ${n}` : n === 0 ? 'None required' : g.sections.length > 1 ? `At least ${n}` : n}
                      </option>
                    ))}
                  </select>
                  {answerCount < count ? (
                    <span className="es-caption">
                      {g.sections.length > 1
                        ? `At least ${answerCount} from this section.`
                        : `${count - answerCount} optional - the student picks ${answerCount}.`}
                    </span>
                  ) : null}
                </div>
              ) : null}
            </div>

            {qs.length ? (
              <div className="pi-marks-grid">
                {qs.map((q) => (
                  <div className="es-field" key={q.id}>
                    <label className="es-field__label" htmlFor={`mk-q-${q.id}`}>
                      Q{q.question_number || q.seq} marks
                    </label>
                    <input
                      id={`mk-q-${q.id}`}
                      className="es-input"
                      type="number"
                      min="0"
                      step="any"
                      defaultValue={q.marks ?? ''}
                      key={`${q.id}-${q.marks ?? ''}`}
                      onBlur={(e) => setOneQuestion(q, e.target.value)}
                    />
                    {/* Only where the student picks: elsewhere every question is answered. */}
                    {answerCount < count || q.compulsory ? (
                      <label className="es-caption" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <input
                          type="checkbox"
                          checked={Boolean(q.compulsory)}
                          disabled={busy}
                          onChange={(e) => setCompulsory(q, e.target.checked)}
                        />
                        Compulsory
                      </label>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            <p style={{ margin: '12px 0 0', fontWeight: 600 }}>
              {explain(sec.label || 'Paper', count, answerCount, total, compulsory)}
              {unmarked ? <span className="es-caption"> - {unmarked} question(s) have no marks yet</span> : null}
            </p>
            {g.sections.length === 1 && !SECTION_LABEL.test(sec.label || '') && qs.length > 1 ? (
              dividing === key ? divideForm(sec, qs) : <div style={{ marginTop: 8 }}>{divideButton(sec)}</div>
            ) : null}
          </div>
        )
      })}
      {g.sections.length > 1 ? (
        <div style={{ border: '1px solid var(--primary)', borderRadius: 14, padding: 18 }}>
          <div className="es-field" style={{ maxWidth: 360 }}>
            <label className="es-field__label" htmlFor={`mk-inall-${g.first}`}>
              Questions to answer in all of {g.title}
            </label>
            <select
              id={`mk-inall-${g.first}`}
              className="es-input"
              value={g.inAll || ''}
              onChange={(e) => setPlan((p) => ({ ...p, [g.first]: { ...p[g.first], group_answer_count: e.target.value } }))}
            >
              <option value="">Just each section's own ({g.least})</option>
              {Array.from({ length: g.most - g.least }, (_, i) => g.least + 1 + i).map((n) => (
                <option key={n} value={n}>
                  {n} - {n - g.least} more from any section
                </option>
              ))}
            </select>
            <span className="es-caption">
              Each section's own count first, then the best of the rest from any section - as EdSider marks it.
            </span>
          </div>
          <p style={{ margin: '12px 0 0', fontWeight: 600 }}>
            {g.inAll ? inAllText(g) : `${g.title}: ${g.total} marks`}
          </p>
        </div>
      ) : null}
      </div>
      ))}

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
              step="any"
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
