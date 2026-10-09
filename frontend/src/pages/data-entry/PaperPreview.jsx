import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api.js'
import RichEditor from '../../components/RichEditor.jsx'
import { joinSoftBreaks } from '../../lib/text.js'
import PaperSummary from '../../components/PaperSummary.jsx'
import './PaperIntake.css'

// The paper as a student sees it: the same content and layout EdSider shows
// on an assessment, read from this paper's own stored data. Answers are never
// shown here - a student does not see them.

// One question as the student sees it. A passage or stem that has parts
// under it shows its text and total marks only - it is not answered itself.
// An MCQ shows each option as a card to pick from. Every other question that
// is answered on its own gets the same editor the reviewer used, placed after
// its own text.
function QuestionBody({ q, images, depth, hasParts, marks, showAnswers }) {
  const optionImages = Object.fromEntries(
    (images || []).filter((i) => i.slot.startsWith('option:')).map((i) => [i.slot.slice(7), i]),
  )
  const questionImages = (images || []).filter((i) => i.slot === 'question')
  const answerImages = (images || []).filter((i) => i.slot === 'answer')
  const stemText = joinSoftBreaks(q.mcq ? q.mcq.stem : q.text)
  const correctLabels = q.mcq?.correct_labels || []
  const choices = q.mcq
    ? q.mcq.options.map((o) => ({ key: o.label, marker: o.marker.trim(), text: joinSoftBreaks(o.text), img: optionImages[o.label], correct: showAnswers && !!o.label && correctLabels.includes(o.label.toLowerCase()) }))
    : Object.entries(optionImages).map(([label, img]) => ({ key: label, marker: label, text: '', img }))
  const isMcq = q.question_type === 'mcq' && choices.length > 0
  const blanks = q.blanks || []
  const isFillBlank = q.question_type === 'fill_blank' && blanks.length > 0
  const number = q.question_number || `Q${q.seq}`

  return (
    <div className="pi-preview__q" style={{ marginLeft: depth * 28 }}>
      <div className="pi-preview__qhead">
        <span className="pi-preview__num">{number}</span>
        {marks != null ? <span className="pi-preview__marks">[{marks} marks]</span> : null}
      </div>
      {stemText ? <p className="pi-preview__text">{stemText}</p> : null}
      {questionImages.map((im) => (
        <img key={im.id} className="pi-preview__img" src={api.imageUrl(im.id)} alt="" />
      ))}
      {isMcq ? (
        <div className="pi-preview__choices" role="radiogroup" aria-label={`Options for ${number}`}>
          {choices.map((c) => (
            <label className={`pi-preview__choice${c.correct ? ' pi-preview__choice--correct' : ''}`} key={c.key}>
              <input type="radio" name={`choice-${q.id}`} />
              <span className="pi-preview__key">{c.marker}</span>
              {c.text ? <span>{c.text}</span> : null}
              {c.img ? <img className="pi-preview__img" src={api.imageUrl(c.img.id)} alt="" /> : null}
            </label>
          ))}
        </div>
      ) : null}
      {showAnswers && isMcq && correctLabels.length ? (
        <p className="pi-preview__scheme-line">
          <span className="pi-preview__scheme-label">Marking scheme</span> Correct answer: {correctLabels.map((l) => l.toUpperCase()).join(', ')}
        </p>
      ) : null}
      {showAnswers && !hasParts && !isMcq && !isFillBlank && (q.answer || answerImages.length) ? (
        <div className="pi-preview__scheme">
          <span className="pi-preview__scheme-label">Marking scheme</span>
          {q.answer ? <p className="pi-preview__text">{joinSoftBreaks(q.answer)}</p> : null}
          {answerImages.map((im) => (
            <img key={im.id} className="pi-preview__img" src={api.imageUrl(im.id)} alt="" />
          ))}
        </div>
      ) : null}
      {showAnswers && isFillBlank ? (
        <p className="pi-preview__scheme-line">
          <span className="pi-preview__scheme-label">Marking scheme</span>{' '}
          {blanks.map((b) => `${b.number}. ${b.answer || '-'}`).join('  ')}
        </p>
      ) : null}
      {!hasParts && !isMcq && !isFillBlank ? (
        <div className="pi-preview__answer">
          <RichEditor initialValue="" ariaLabel={`Answer for ${number}`} />
        </div>
      ) : null}
      {!hasParts && isFillBlank ? (
        <div className="pi-preview__blanks" role="group" aria-label={`Answers for ${number}`}>
          {blanks.map((b) => (
            <label className="pi-preview__blank" key={b.number}>
              <span className="pi-preview__key">{b.number}</span>
              <input type="text" className="pi-preview__blank-input" aria-label={`${number} blank ${b.number} answer`} />
            </label>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export default function PaperPreview() {
  const { Button, Notice } = window.EdSider
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const docId = params.get('doc')
  // Final view keeps the marking scheme and the correct options; the student
  // view shows only what a student is given.
  const [showAnswers, setShowAnswers] = useState(true)
  const [data, setData] = useState({ doc: null, questions: null, images: {}, error: null })

  useEffect(() => {
    let live = true
    Promise.all([
      api.getIntakeDocument(docId),
      api.listIntakeQuestions(docId),
      api.listDocumentImages(docId),
    ])
      .then(([doc, questions, images]) => {
        if (live) setData({ doc, questions, images, error: null })
      })
      .catch((err) => {
        if (live) setData((d) => ({ ...d, error: err.message }))
      })
    return () => {
      live = false
    }
  }, [docId])

  const { doc, questions, images, error } = data

  if (error) {
    return (
      <Notice tone="error" title="Could not load the paper">
        {error}
      </Notice>
    )
  }
  if (!doc || !questions) return <Notice tone="info">Loading...</Notice>

  // The paper's total is worked out from its own instructions (questions to
  // answer x marks each, per part) - see the extractor's suggest_settings.
  const topLevel = questions.filter((q) => !q.depth)
  const childrenOf = {}
  for (const q of questions) {
    if (q.parent_seq != null) (childrenOf[q.parent_seq] ||= []).push(q)
  }

  // Each part of the paper, with its own questions in reading order.
  const sections = (doc.sections || []).length
    ? doc.sections
    : [{ label: null, heading: null, kind: null, instructions: null, marks_each: null }]

  // A question's total: the marks the paper states for it, or - when it has
  // parts and states none of its own - what its parts add up to.
  function totalOf(q) {
    if (q.marks != null) return Number(q.marks)
    const kids = childrenOf[q.seq] || []
    if (!kids.length) return null
    const parts = kids.map(totalOf).filter((m) => m != null)
    return parts.length ? parts.reduce((a, b) => a + b, 0) : null
  }

  // A part that states "each question carries N marks" makes every top-level
  // question in it worth N, whatever its sub-part figures add up to.
  function sectionMarks(q) {
    if (q.depth) return null
    const sec = sections.find((x) => (x.label ?? null) === (q.section ?? null))
    return sec?.marks_each ?? null
  }

  function renderTree(q, depth) {
    const children = childrenOf[q.seq] || []
    return (
      <div key={q.id}>
        <QuestionBody
          q={q}
          images={images[String(q.id)]}
          depth={depth}
          hasParts={children.length > 0}
          marks={sectionMarks(q) ?? totalOf(q)}
          showAnswers={showAnswers}
        />
        {children.map((c) => renderTree(c, depth + 1))}
      </div>
    )
  }

  return (
    <div className="pi-preview" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div className="es-card" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div role="group" aria-label="Preview mode" style={{ display: 'flex', gap: 8 }}>
            <Button
              variant={showAnswers ? 'primary' : 'secondary'}
              size="sm"
              aria-pressed={showAnswers}
              onClick={() => setShowAnswers(true)}
            >
              Final view
            </Button>
            <Button
              variant={showAnswers ? 'secondary' : 'primary'}
              size="sm"
              aria-pressed={!showAnswers}
              onClick={() => setShowAnswers(false)}
            >
              Student view
            </Button>
          </div>
          <p className="es-caption" style={{ margin: 0 }}>
            {showAnswers
              ? 'Final view - the paper with the marking scheme and correct options, for checking.'
              : 'Student view - only the questions, options and answer boxes, as a student sees them.'}
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => navigate(`/app/data-entry/paper-intake?doc=${doc.id}`)}>
          Back to review
        </Button>
      </div>

      <PaperSummary doc={doc} questions={questions} />

      {sections.map((sec, si) => {
        const inSection = topLevel.filter((q) => (q.section ?? null) === (sec.label ?? null))
        return (
          <section key={sec.label ?? si} className="es-card" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {sec.label ? <h2 className="scr-h2" style={{ color: 'var(--primary)', margin: 0 }}>{sec.label}</h2> : null}
            {sec.instructions ? <p className="pi-preview__text" style={{ margin: 0 }}>{sec.instructions}</p> : null}
            {inSection.map((q) => renderTree(q, 0))}
          </section>
        )
      })}
    </div>
  )
}
