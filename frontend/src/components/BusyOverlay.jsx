import './BusyOverlay.css'

// While something slow runs - copying a paper to the question bank, sending
// it to EdSider, reading an upload - the page is covered with a spinner and
// what is happening, so nobody clicks again or leaves thinking it stuck.
// Nothing is shown while `message` is empty.
export default function BusyOverlay({ message, detail = 'This can take a little while. Please keep this page open.' }) {
  if (!message) return null
  return (
    <div className="bo-backdrop" role="alertdialog" aria-modal="true" aria-live="polite" aria-busy="true">
      <div className="es-card bo-box">
        <div className="bo-spinner" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <p className="bo-message">{message}</p>
        <p className="es-caption bo-detail">{detail}</p>
      </div>
    </div>
  )
}
