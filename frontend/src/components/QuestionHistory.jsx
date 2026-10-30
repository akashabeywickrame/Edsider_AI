import { useState } from 'react'
import { api } from '../lib/api.js'
import { AuthImg } from '../lib/useFileUrl.jsx'
import './QuestionHistory.css'

// A question's earlier versions - what it was before each time it was
// replaced (a repeat on its paper), who replaced it, when and why - beside
// what it is now. Read only when opened.
function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleString()
}

const SLOT_TEXT = { question: 'Question', answer: 'Answer' }

export default function QuestionHistory({ questionId, count }) {
  const { Button, Notice } = window.EdSider
  const [open, setOpen] = useState(false)
  const [history, setHistory] = useState(null)
  const [error, setError] = useState(null)

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && !history) {
      try {
        setHistory(await api.questionHistory(questionId))
      } catch (err) {
        setError(err.message)
      }
    }
  }

  if (!count) return null
  return (
    <div className="qh">
      <Button variant="ghost" size="sm" onClick={toggle} aria-expanded={open}>
        {open ? 'Hide earlier versions' : `Earlier versions (${count})`}
      </Button>
      {open ? (
        <div className="qh__list">
          {error ? <Notice tone="error">{error}</Notice> : null}
          {!history && !error ? <p className="es-caption">Loading...</p> : null}
          {history
            ? [...history.earlier].reverse().map((v, i) => (
                <div className="qh__version" key={v.id}>
                  <p className="es-caption qh__meta">
                    {i === 0 ? 'Before the latest change' : 'Earlier'} - changed{' '}
                    {v.replaced_by ? `by ${v.replaced_by} ` : ''}on {when(v.replaced_at)}
                    {v.reason ? ` - ${v.reason}` : ''}
                  </p>
                  <p className="qh__text">{v.text}</p>
                  {v.answer ? (
                    <p className="qh__text">
                      <span className="tr-lab">Answer </span>
                      {v.answer}
                    </p>
                  ) : null}
                  {v.marks != null ? <p className="es-caption">{v.marks} mark(s)</p> : null}
                  {(v.pictures || []).map((p) => (
                    <figure key={p.id} className="qh__pic">
                      <figcaption className="es-caption">
                        {SLOT_TEXT[p.slot] || p.slot}
                        {p.label ? `, labelled ${p.label}` : ''}
                      </figcaption>
                      <AuthImg path={api.imagePath(p.id)} alt="" />
                    </figure>
                  ))}
                </div>
              ))
            : null}
          {history ? (
            <div className="qh__version qh__version--now">
              <p className="es-caption qh__meta">Now</p>
              <p className="qh__text">{history.current.text}</p>
              {history.current.answer ? (
                <p className="qh__text">
                  <span className="tr-lab">Answer </span>
                  {history.current.answer}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
