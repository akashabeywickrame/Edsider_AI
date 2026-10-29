import { useEffect, useRef } from 'react'
import { createRoot } from 'react-dom/client'
import './ConfirmDialog.css'

// "Delete this paper permanently?" - Yes / No. One short question in our
// own dialog, in place of the browser's OK / Cancel box. `danger` makes Yes
// red, for what cannot be undone.
//
//   if (!(await confirmDialog('Delete this paper permanently?', { danger: true }))) return

function Dialog({ message, danger, onAnswer }) {
  const { Button } = window.EdSider
  const noRef = useRef(null)
  const yesRef = useRef(null)

  useEffect(() => {
    // The safe answer has the focus when the action cannot be undone.
    ;(danger ? noRef : yesRef).current?.querySelector('button, .es-btn')?.focus()
    const onKey = (e) => {
      if (e.key === 'Escape') onAnswer(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [danger, onAnswer])

  return (
    <div className="cd-backdrop" onClick={() => onAnswer(false)}>
      <div
        className="es-card cd-box"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="cd-message"
        onClick={(e) => e.stopPropagation()}
      >
        <p id="cd-message" className="cd-message">
          {message}
        </p>
        <div className="cd-actions">
          <span ref={noRef}>
            <Button variant="secondary" onClick={() => onAnswer(false)}>
              No
            </Button>
          </span>
          <span ref={yesRef}>
            <Button variant={danger ? 'danger' : 'primary'} onClick={() => onAnswer(true)}>
              Yes
            </Button>
          </span>
        </div>
      </div>
    </div>
  )
}

export function confirmDialog(message, { danger = false } = {}) {
  return new Promise((resolve) => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const root = createRoot(host)
    const answer = (yes) => {
      root.unmount()
      host.remove()
      resolve(yes)
    }
    root.render(<Dialog message={message} danger={danger} onAnswer={answer} />)
  })
}
