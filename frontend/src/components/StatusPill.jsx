// A paper's status as people read it. The design bundle's own StatusPill
// knew only the first few statuses and printed the rest as their codes
// ("ready_to_publish"), so every page uses this one instead.

export const STATUS_LABEL = {
  extracted: 'Extracted',
  in_review: 'In review',
  checked: 'Duplicate check done',
  data_entry_done: 'Ready for a teacher',
  teacher_review: 'Awaiting teacher review',
  refer_back: 'Sent back for changes',
  review_done: 'Review done',
  ready_to_publish: 'Ready to publish',
  published: 'Published',
  archived: 'Archived',
}

const TONE = {
  checked: 'primary',
  data_entry_done: 'primary',
  teacher_review: 'review',
  refer_back: 'dup',
  review_done: 'ok',
  ready_to_publish: 'ok',
  published: 'ok',
}

// A status added later still reads as words, never as its code.
export function statusLabel(status) {
  if (!status) return ''
  if (STATUS_LABEL[status]) return STATUS_LABEL[status]
  const words = String(status).replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export default function StatusPill({ status, children }) {
  return (
    <span className={`es-pill es-tone--${TONE[status] || 'neutral'}`}>{children || statusLabel(status)}</span>
  )
}
