import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import './Toast.css'

// Short confirmations for button presses - "Paper assigned to ...",
// "Duplicate check completed" - that appear in the corner and go away on their
// own. Errors stay a little longer, and any of them can be closed early.
// Inline notices on the page are still the place for anything that needs
// reading in context; these are for "that worked" and "that failed".

const ToastContext = createContext(null)

const DURATION_MS = { success: 4000, info: 4000, error: 7000 }
const MAX_VISIBLE = 4

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])
  const nextId = useRef(0)

  const dismiss = useCallback((id) => {
    setItems((list) => list.filter((t) => t.id !== id))
  }, [])

  const show = useCallback(
    (tone, message) => {
      const id = ++nextId.current
      setItems((list) => [...list.slice(-(MAX_VISIBLE - 1)), { id, tone, message }])
      window.setTimeout(() => dismiss(id), DURATION_MS[tone])
    },
    [dismiss],
  )

  const toast = useMemo(
    () => ({
      success: (message) => show('success', message),
      info: (message) => show('info', message),
      error: (message) => show('error', message),
    }),
    [show],
  )

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((t) => (
          <div
            key={t.id}
            role={t.tone === 'error' ? 'alert' : 'status'}
            className={`toast toast--${t.tone}`}
          >
            <span className="toast__text">{t.message}</span>
            <button
              type="button"
              className="toast__close"
              aria-label="Dismiss notification"
              onClick={() => dismiss(t.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

// Returns { success, info, error } - call toast.success("...") from any page.
export function useToast() {
  const toast = useContext(ToastContext)
  if (!toast) throw new Error('useToast() must be used inside <ToastProvider>')
  return toast
}
