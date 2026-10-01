import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'
import './CheckQuestion.css'

// "G07" -> "Grade 7" - the bank's own grade codes (CLAUDE.md: "stores
// grades as G06-G13") shown the way a reviewer actually reads them.
function gradeLabel(code) {
  const m = /^G(\d{2})$/.exec(code || '')
  return m ? `Grade ${Number(m[1])}` : code
}

const LANGS = [
  { key: 'en', label: 'English' },
  { key: 'si', label: 'සිංහල', lang: 'si' },
  { key: 'ta', label: 'தமிழ்', lang: 'ta' },
]

export default function CheckQuestion() {
  const { Button, Notice, Verdict, Score, QuestionCard, GuardNote, PageHeader } = window.EdSider

  const [facets, setFacets] = useState({ grades: [], subjects: [] })
  const [comparableCount, setComparableCount] = useState(null)
  const [text, setText] = useState('')
  const [grade, setGrade] = useState('')
  const [subject, setSubject] = useState('')
  const [lang, setLang] = useState('en')

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [addNote, setAddNote] = useState(null)

  useEffect(() => {
    api.questionFacets().then(setFacets).catch(() => {})
    api.health().then((h) => setComparableCount(h.comparable_questions)).catch(() => {})
  }, [])

  async function handleCheck() {
    if (!text.trim()) return
    setLoading(true)
    setError(null)
    setAddNote(null)
    try {
      const res = await api.checkQuestion({
        text,
        grade: grade || undefined,
        subject: subject || undefined,
      })
      setResult(res)
    } catch (err) {
      setError(err.message)
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  // There is no endpoint yet for actually saving a new question into
  // the bank (service.py only exposes GET /questions, for browsing) -
  // see CLAUDE.md's "Paper intake... promote reviewed drafts into the
  // bank" note for the closest equivalent that does exist. Rather than
  // pretend this button does something, it says plainly that it does
  // not yet.
  function handleAdd() {
    setAddNote('Adding a question to the bank from this page is not built yet.')
  }

  const flaggedMatch = result?.matches?.some((m) => m.verdict === 'LIKELY DUPLICATE')
  const canAdd = result != null && result.verdict !== 'EXACT DUPLICATE' && !flaggedMatch

  return (
    <>
      <PageHeader
        accent="Check"
        title="a new question"
        subtitle="Paste a question before adding it. First the bank is searched for an exact copy, then the meaning is compared with every question already there."
      />

      <div className="cq-grid">
        <section className="es-card cq-form" aria-label="New question">
          <h2 className="scr-h2">New question</h2>

          <div className="es-field">
            <label className="es-field__label" htmlFor="cq-text">
              Question text
            </label>
            <textarea
              id="cq-text"
              className="es-input"
              rows={4}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Calculate the perimeter of a rectangle 12 cm by 5 cm."
            />
          </div>

          <div className="cq-row2">
            <div className="es-field">
              <label className="es-field__label" htmlFor="cq-grade">
                Grade
              </label>
              <select id="cq-grade" className="es-input" value={grade} onChange={(e) => setGrade(e.target.value)}>
                <option value="">Not set</option>
                {facets.grades.map((g) => (
                  <option key={g} value={g}>
                    {gradeLabel(g)}
                  </option>
                ))}
              </select>
            </div>
            <div className="es-field">
              <label className="es-field__label" htmlFor="cq-subject">
                Subject
              </label>
              <select
                id="cq-subject"
                className="es-input"
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
              >
                <option value="">Not set</option>
                {facets.subjects.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <fieldset className="cq-lang">
            <legend className="es-field__label">Language</legend>
            <div className="es-row">
              {LANGS.map((l) => (
                <label className="scr-radio" key={l.key} lang={l.lang}>
                  <input
                    type="radio"
                    name="lang"
                    checked={lang === l.key}
                    onChange={() => setLang(l.key)}
                  />
                  {l.label}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="es-row cq-actions">
            <Button variant="primary" arrow disabled={!text.trim() || loading} onClick={handleCheck}>
              {loading ? 'Checking...' : 'Check against the bank'}
            </Button>
            <Button variant="secondary" disabled={!canAdd} onClick={handleAdd}>
              Add to the bank
            </Button>
          </div>
          <p className="es-caption">
            A question can only be added when nothing is flagged. A flagged match needs a
            person's decision first.
          </p>
          {addNote ? <Notice tone="info">{addNote}</Notice> : null}
        </section>

        <section aria-label="Result" className="cq-result">
          <div className="cq-result__head">
            <h2 className="scr-h2">Result</h2>
            {result ? (
              <p className="es-caption">
                Checked against {comparableCount != null ? comparableCount.toLocaleString() : '?'}{' '}
                questions in {result.elapsed_ms} ms
              </p>
            ) : null}
          </div>

          {error ? <Notice tone="error" title="Could not check this question">{error}</Notice> : null}

          {!result && !error && !loading ? (
            <Notice tone="info">
              Paste a question on the left and press "Check against the bank" to see results
              here.
            </Notice>
          ) : null}

          {result && result.verdict === 'EXACT DUPLICATE' ? (
            <Notice tone="error" title="Exact copy found">
              This exact text is already in the bank, as{' '}
              {result.exact_duplicate_of.map((id, i) => (
                <span key={id}>
                  {i > 0 ? ', ' : ''}
                  <b>#{id}</b>
                </span>
              ))}
              .
            </Notice>
          ) : null}

          {result && result.verdict !== 'EXACT DUPLICATE' && result.matches.length === 0 ? (
            <Notice tone="success" title="No matches">
              No question in the bank reads like this one.
            </Notice>
          ) : null}

          {result && result.verdict !== 'EXACT DUPLICATE' && result.matches.length > 0 ? (
            <Notice tone="info" title="No exact copy">
              No question in the bank has this exact text. Closest matches by meaning are below.
            </Notice>
          ) : null}

          {result?.matches?.map((m) => (
            <article className="es-card cq-match" key={m.id}>
              <div className="es-row cq-match__head">
                <Verdict verdict={m.verdict} />
                <Score score={m.score} raw={m.raw_score} legend={false} />
              </div>
              <QuestionCard
                id={m.id}
                text={m.text}
                grade={m.grade}
                subject={m.subject}
              />
              <GuardNote reasons={m.reasons} raw={m.raw_score} score={m.score} />
            </article>
          ))}

          {flaggedMatch ? (
            <Notice tone="warning" title="Not added to the bank">
              At least one likely duplicate needs a person's decision before this question can be
              added.
            </Notice>
          ) : null}
        </section>
      </div>
    </>
  )
}
