import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'

// Every staff profile in one place (service.py /admin/users, the `staff`
// table): EdSider's own teachers, copied by the teacher sync; people a
// super admin added here (EdSider's own staff pattern - added by hand, no
// self-service sign-up); and anyone seen in the teacher app. Anyone whose
// "Signs in as" is blank is turned away at sign-in (see RequireUser).

// "Employee" covers the data-entry / question-review people - "staff" was
// confirmed to mean the same job, so it is not a separate option here.
const ROLES = [
  { value: 'superadmin', label: 'Super admin' },
  { value: 'admin', label: 'Admin' },
  { value: 'teacher', label: 'Teacher' },
  { value: 'employee', label: 'Data Entry Operator' },
]

function roleLabel(role) {
  return ROLES.find((r) => r.value === role)?.label || role
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleDateString()
}

function subjectsFrom(text) {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

// Where a profile came from, in words a super admin needs.
function sourceLabel(s) {
  if (s.in_edsider) return 'EdSider teacher'
  if (s.added_by) return 'Added here'
  return 'Seen in teacher app'
}

function StaffRow({ s, me, onChanged, onRemoved }) {
  const { Button } = window.EdSider
  const toast = useToast()
  const [role, setRole] = useState(s.role || '')
  const [subjects, setSubjects] = useState((s.subjects || []).join(', '))
  const [busy, setBusy] = useState(false)
  const dirty = role !== (s.role || '') || subjects !== (s.subjects || []).join(', ')
  const isMe = me && me.identity === s.email

  async function run(action, fields, message) {
    setBusy(true)
    try {
      if (action === 'remove') {
        await api.removeStaff(s.email)
        onRemoved(s.email)
      } else {
        onChanged(await api.updateStaff(s.email, fields))
      }
      toast.success(message)
    } catch (err) {
      toast.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <tr style={s.active ? undefined : { opacity: 0.55 }}>
      <td>
        <span style={{ fontWeight: 600 }}>{s.name || '-'}</span>
        <br />
        <span className="es-caption">{s.email}</span>
      </td>
      <td className="es-caption">{sourceLabel(s)}</td>
      <td>
        <select
          className="es-input"
          aria-label={`Role for ${s.email}`}
          value={role}
          disabled={busy}
          onChange={(e) => setRole(e.target.value)}
        >
          <option value="">{s.in_edsider ? 'As in EdSider (Teacher)' : 'No access'}</option>
          {ROLES.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>
      </td>
      <td>
        <input
          className="es-input"
          aria-label={`Subjects for ${s.email}`}
          value={subjects}
          disabled={busy}
          placeholder="e.g. ICT, Physics"
          onChange={(e) => setSubjects(e.target.value)}
        />
      </td>
      <td>
        {s.effective_role ? roleLabel(s.effective_role) : <span className="es-caption">Cannot sign in</span>}
        {!s.active ? <span className="es-caption"> (switched off)</span> : null}
        {s.positions ? <div className="es-caption">In EdSider: {s.positions}</div> : null}
      </td>
      <td className="es-caption">{s.last_seen_at ? when(s.last_seen_at) : '-'}</td>
      <td style={{ whiteSpace: 'nowrap' }}>
        {dirty ? (
          <Button
            variant="primary"
            size="sm"
            disabled={busy}
            onClick={() =>
              run('update', { role: role || null, subjects: subjectsFrom(subjects) }, `${s.name || s.email} saved.`)
            }
          >
            Save
          </Button>
        ) : null}{' '}
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || isMe}
          title={isMe ? 'You cannot switch off your own account' : undefined}
          onClick={() =>
            run(
              'update',
              { active: !s.active },
              s.active ? `${s.name || s.email} can no longer sign in.` : `${s.name || s.email} switched back on.`,
            )
          }
        >
          {s.active ? 'Switch off' : 'Switch on'}
        </Button>
        {!s.in_edsider && s.added_by ? (
          <>
            {' '}
            <Button
              variant="ghost"
              size="sm"
              disabled={busy || isMe}
              onClick={() => run('remove', null, `${s.email} removed.`)}
            >
              Remove
            </Button>
          </>
        ) : null}
      </td>
    </tr>
  )
}

export default function ManageUsers() {
  const { PageHeader, Button, Notice } = window.EdSider
  const me = useOutletContext()
  // role is null on a local run with no Easy Auth header - open there,
  // same as service.py's require_superadmin().
  const allowed = !me || me.role === null || me.role === 'superadmin'
  const toast = useToast()
  const [staff, setStaff] = useState(null)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [form, setForm] = useState({ email: '', name: '', role: 'employee', subjects: '' })
  const [saving, setSaving] = useState(false)

  function load() {
    api
      .listStaff()
      .then(setStaff)
      .catch((err) => setError(err.message))
  }

  useEffect(() => {
    if (allowed) load()
  }, [allowed])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!staff || !q) return staff || []
    return staff.filter((s) =>
      [s.name, s.email, ...(s.subjects || [])].some((v) => (v || '').toLowerCase().includes(q)),
    )
  }, [staff, query])

  if (!allowed) {
    return (
      <Notice tone="warning" title="Super admin access required">
        Only a super admin can add people or change their roles. Ask a super admin if someone
        needs access.
      </Notice>
    )
  }

  async function addPerson(e) {
    e.preventDefault()
    if (!form.email.trim() || !form.name.trim()) return
    setSaving(true)
    setError(null)
    try {
      await api.addStaff({
        email: form.email.trim(),
        name: form.name.trim(),
        role: form.role,
        subjects: subjectsFrom(form.subjects),
      })
      toast.success(`${form.name.trim()} can now sign in.`)
      setForm({ email: '', name: '', role: 'employee', subjects: '' })
      load()
    } catch (err) {
      setError(err.message)
      toast.error(`Could not add that person: ${err.message}`)
    } finally {
      setSaving(false)
    }
  }

  const replace = (updated) => setStaff((list) => list.map((s) => (s.email === updated.email ? updated : s)))
  const drop = (email) => setStaff((list) => list.filter((s) => s.email !== email))
  const canSignIn = staff ? staff.filter((s) => s.effective_role).length : 0

  return (
    <>
      <PageHeader
        accent="Manage"
        title="staff"
        subtitle="Everyone who works in EdsiderAI. EdSider's teachers appear automatically and can sign in as teachers; add anyone else here with their email and role. Switching someone off stops them signing in to EdsiderAI only, not EdSider."
      />

      {error ? (
        <Notice tone="error" title="Something went wrong">
          {error}
        </Notice>
      ) : null}

      <form className="es-card" onSubmit={addPerson} style={{ display: 'grid', gap: 12, maxWidth: 560 }}>
        <p className="scr-h3" style={{ margin: 0 }}>
          Add a person
        </p>
        <div className="es-field">
          <label className="es-field__label" htmlFor="mu-email">
            Email (the one they sign in with)
          </label>
          <input
            id="mu-email"
            className="es-input"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
            placeholder="name@example.com"
          />
        </div>
        <div className="es-field">
          <label className="es-field__label" htmlFor="mu-name">
            Name
          </label>
          <input
            id="mu-name"
            className="es-input"
            type="text"
            required
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="How their name should appear in the app"
          />
        </div>
        <div className="es-field">
          <label className="es-field__label" htmlFor="mu-role">
            Role
          </label>
          <select
            id="mu-role"
            className="es-input"
            value={form.role}
            onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
          >
            {ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        <div className="es-field">
          <label className="es-field__label" htmlFor="mu-subjects">
            Subjects (optional, comma-separated)
          </label>
          <input
            id="mu-subjects"
            className="es-input"
            value={form.subjects}
            onChange={(e) => setForm((f) => ({ ...f, subjects: e.target.value }))}
            placeholder="e.g. ICT, Physics"
          />
        </div>
        <div>
          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? 'Adding...' : 'Add person'}
          </Button>
        </div>
      </form>

      <div style={{ display: 'flex', alignItems: 'end', gap: 16, marginTop: 32, flexWrap: 'wrap' }}>
        <h2 className="scr-h2" style={{ margin: 0 }}>
          Staff ({staff ? `${staff.length}, ${canSignIn} can sign in` : '...'})
        </h2>
        <input
          className="es-input"
          style={{ maxWidth: 320 }}
          aria-label="Search staff"
          placeholder="Search by name, email or subject"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {staff === null ? (
        <Notice tone="info">Loading...</Notice>
      ) : staff.length === 0 ? (
        <Notice tone="info">
          Nobody yet. EdSider's teachers appear after the first teacher sync (open Assign papers, or
          run scripts/sync.py); only the bootstrap super admin can sign in until then.
        </Notice>
      ) : (
        <section className="es-card" aria-label="Staff" style={{ overflowX: 'auto' }}>
          <table className="scr-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>From</th>
                <th>Role set here</th>
                <th>Subjects</th>
                <th>Signs in as</th>
                <th>Last seen</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <StaffRow key={s.email} s={s} me={me} onChanged={replace} onRemoved={drop} />
              ))}
            </tbody>
          </table>
          {shown.length === 0 ? (
            <p className="es-caption" style={{ margin: 0 }}>
              Nobody matches "{query}".
            </p>
          ) : null}
        </section>
      )}
    </>
  )
}
