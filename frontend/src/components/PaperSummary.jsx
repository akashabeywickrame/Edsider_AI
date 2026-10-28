import { joinSoftBreaks } from '../lib/text.js'
import { roundMarks } from '../lib/numbers.js'
import '../pages/data-entry/PaperIntake.css'

// The paper's title, facts and instructions - the same header on the final
// view and on the teacher's review, so both read the paper the same way.

export function gradeText(code) {
  if (!code) return null
  return code.replace(/^G0*/, 'Grade ')
}

export function durationText(settings) {
  if (!settings?.time_limit || !settings.duration_value) return null
  return `Duration: ${settings.duration_value} ${settings.duration_unit || 'minutes'}`
}

// Sum of the marks a student can actually earn: the leaves, since a question
// with parts is worth what its parts add up to.
export function totalMarks(questions) {
  const parents = new Set(questions.map((q) => q.parent_seq).filter((x) => x != null))
  return roundMarks(questions
    .filter((q) => !parents.has(q.seq) && q.marks != null)
    .reduce((sum, q) => sum + Number(q.marks), 0))
}

// The paper's total: the figure the paper states (questions to answer x marks
// each, per part), else what its questions add up to.
export function paperTotalOf(doc, questions) {
  const marking = doc.assessment_settings?.marking
  return marking?.target_total ?? marking?.total ?? doc.suggested_settings?.total_marks ?? totalMarks(questions)
}

export default function PaperSummary({ doc, questions }) {
  const settings = doc.assessment_settings || {}
  const topLevel = questions.filter((q) => !q.depth)
  const total = paperTotalOf(doc, questions)
  return (
    <>
      <header className="pi-preview__header">
        <div className="pi-preview__brand">
          <img src="/edsider-logo.png" alt="EdSider" />
        </div>
        <div className="pi-preview__titlebox">
          <h1 className="pi-preview__title">{doc.paper_title || doc.filename}</h1>
          <div className="pi-preview__pills">
            {[
              doc.paper_subject,
              gradeText(doc.paper_grade),
              durationText(settings),
              `Questions: ${topLevel.length}`,
              // A zero means no marks are known yet, not a paper worth nothing.
              total ? `Total marks: ${total}` : null,
              doc.sections?.length ? `Parts: ${doc.sections.length}` : null,
            ]
              .filter(Boolean)
              .map((t) => (
                <span key={t} className="es-pill es-tone--neutral">
                  {t}
                </span>
              ))}
          </div>
        </div>
      </header>

      {doc.notes ? (
        <div className="es-card">
          <p className="scr-h3" style={{ margin: '0 0 6px' }}>Instructions</p>
          <p className="pi-preview__text" style={{ whiteSpace: 'pre-wrap' }}>{joinSoftBreaks(doc.notes)}</p>
        </div>
      ) : null}
    </>
  )
}
