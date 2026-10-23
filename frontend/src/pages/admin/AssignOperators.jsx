import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import { STATUS_LABEL } from '../data-entry/PaperFinder.jsx'
import './PaperAssignment.css'

// Giving uploaded papers to Data Entry Operators - EdSider's own "assign an
// operator" step. Only the operator a paper is given to (and admins) can open
// and correct it; the API refuses everyone else. Giving it to someone else
// takes it off the previous person, and the paper's history keeps both.

const PAGE = 25

function gradeText(code) {
  return code ? code.replace(/^G0*/, 'Grade ') : ''
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 16) : d.toLocaleString()
}

const STAGE = { data_entry: 'data entry', teacher: 'teacher review' }

function describe(h, nameOf) {
  const by = h.actor ? ` by ${nameOf(h.actor)}` : ''
  switch (h.action) {
    case 'uploaded':
      return `Uploaded${by}`
    case 'moved':
      return 'Moved into the new intake tables'
    case 'assigned':
      return `Given to ${nameOf(h.assigned_to)} for ${STAGE[h.stage] || h.stage}${by}`
    case 'unassigned':
      return `Taken off everyone for ${STAGE[h.stage] || h.stage}${by}`
    case 'finished':
      return `${STAGE[h.stage] === 'data entry' ? 'Data entry' : 'Teacher review'} finished${by}`
    case 'reopened':
      return `Back with data entry${by}`
    case 'published':
      return `Added to the question bank${by}`
    default:
      return `${STATUS_LABEL[h.from_status] || h.from_status || '-'} -> ${STATUS_LABEL[h.to_status] || h.to_status}${by}`
  }
}

export default function AssignOperators() {
  const { PageHeader, Button, Notice, StatusPill } = window.EdSider
  const toast = useToast()
  const [operators, setOperators] = useState([])
  const [who, setWho] = useState('none')
  const [status, setStatus] = useState('')
  const [text, setText] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [version, setVersion] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [chosen, setChosen] = useState({})
  const [busy, setBusy] = useState({})
  const [openHistory, setOpenHistory] = useState(null)
  const [histories, setHistories] = useState({})
  const [operatorsBusy, setOperatorsBusy] = useState(false)
  const [operatorsNote, setOperatorsNote] = useState(null)

  // EdSider's Data Entry Operators come in with the same sync as its
  // teachers. The saved list shows at once; the refresh replaces it when
  // it lands, or a note says EdSider could not be reached.
  async function refreshOperators() {
    setOperatorsBusy(true)
    setOperatorsNote(null)
    try {
      setOperators(await api.listOperators())
    } catch (err) {
      setError(err.message)
    }
    try {
      await api.syncTeachers()
      setOperators(await api.listOperators())
    } catch (err) {
      setOperatorsNote(`Showing the last list copied from EdSider - ${err.message}`)
    } finally {
      setOperatorsBusy(false)
    }
  }

  useEffect(() => {
    refreshOperators()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(text)
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [text])

  useEffect(() => {
    let live = true
    api
      .searchIntakeDocuments({ operator: who, status, q: search, limit: PAGE, offset: page * PAGE })
      .then((r) => {
        if (!live) return
        setResult(r)
        setError(null)
      })
      .catch((err) => live && setError(err.message))
    return () => {
      live = false
    }
  }, [who, status, search, page, version])

  const nameOf = (identity) => operators.find((o) => o.identity === identity)?.display_name || identity
  const facets = result?.facets || { status: {}, operator: {} }
  const total = result?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE))

  async function give(doc) {
    const identity = chosen[doc.id] ?? doc.operator ?? ''
    setBusy((b) => ({ ...b, [doc.id]: true }))
    try {
      await api.assignOperator(doc.id, identity)
      toast.success(
        identity
          ? `#${doc.id} ${doc.paper_title || doc.filename} given to ${nameOf(identity)}.`
          : `#${doc.id} taken off ${nameOf(doc.operator)}.`,
      )
      setChosen((c) => {
        const next = { ...c }
        delete next[doc.id]
        return next
      })
      setHistories((h) => ({ ...h, [doc.id]: undefined }))
      setVersion((v) => v + 1)
    } catch (err) {
      toast.error(`Not assigned: ${err.message}`)
    } finally {
      setBusy((b) => ({ ...b, [doc.id]: false }))
    }
  }

  function toggleHistory(docId) {
    if (openHistory === docId) {
      setOpenHistory(null)
      return
    }
    setOpenHistory(docId)
    if (!histories[docId]) {
      api
        .documentHistory(docId)
        .then((h) => setHistories((prev) => ({ ...prev, [docId]: h })))
        .catch((err) => toast.error(`Could not load the history: ${err.message}`))
    }
  }

  function change(setter) {
    return (e) => {
      setter(e.target.value)
      setPage(0)
    }
  }

  return (
    <>
      <PageHeader
        accent="Assign"
        title="papers to data entry"
        subtitle="Give each uploaded paper to a Data Entry Operator. Only that person can open and correct it; once they run its duplicate check it comes back here, ready for a teacher."
      />

      <div className="es-row" style={{ gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
        <Link className="es-btn es-btn--secondary es-btn--sm" to="/app/data-entry/paper-intake">
          Upload papers
        </Link>
        <span className="es-caption">
          {operatorsBusy
            ? 'Loading Data Entry Operators from EdSider...'
            : `${operators.length} Data Entry Operator(s) - everyone EdSider lists as one, and anyone given the role on Manage users.`}
        </span>
        <Button variant="ghost" size="sm" onClick={refreshOperators} disabled={operatorsBusy}>
          Refresh from EdSider
        </Button>
      </div>
      {operatorsNote ? <Notice tone="warning">{operatorsNote}</Notice> : null}

      {error ? (
        <Notice tone="error" title="Could not load the papers">
          {error}
        </Notice>
      ) : null}

      <div className="pi-row" style={{ display: 'flex', alignItems: 'flex-end', flexWrap: 'wrap', gap: 10, marginBottom: 12 }}>
        <div className="es-field" style={{ flex: '2 1 220px' }}>
          <label className="es-field__label" htmlFor="ao-search">
            Find a paper
          </label>
          <input
            id="ao-search"
            className="es-input"
            type="search"
            placeholder="File name or paper title"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <div className="es-field" style={{ flex: '1 1 200px' }}>
          <label className="es-field__label" htmlFor="ao-who">
            Data Entry Operator
          </label>
          <select id="ao-who" className="es-input" value={who} onChange={change(setWho)}>
            <option value="none">Not given to anyone ({facets.operator?.none || 0})</option>
            <option value="">Everyone's papers</option>
            {operators.map((o) => (
              <option key={o.identity} value={o.identity}>
                {o.display_name || o.identity} ({facets.operator?.[o.identity] || 0})
              </option>
            ))}
          </select>
        </div>
        <div className="es-field" style={{ flex: '1 1 170px' }}>
          <label className="es-field__label" htmlFor="ao-status">
            Status
          </label>
          <select id="ao-status" className="es-input" value={status} onChange={change(setStatus)}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([key, label]) => (
              <option key={key} value={key}>
                {label} ({facets.status?.[key] || 0})
              </option>
            ))}
          </select>
        </div>
      </div>

      <p className="es-caption">
        {result ? `${total} paper${total === 1 ? '' : 's'}${total > PAGE ? ` - page ${page + 1} of ${pages}` : ''}` : 'Loading papers...'}
      </p>

      <section className="es-card pa-table-card" aria-label="Papers">
        {result && !result.items.length ? (
          <p className="es-caption" style={{ margin: 0 }}>
            {who === 'none' ? 'Every paper has been given to someone.' : 'No papers match these filters.'}
          </p>
        ) : (
          <table className="scr-table">
            <thead>
              <tr>
                <th>Paper</th>
                <th>Status</th>
                <th>Data Entry Operator</th>
                <th>History</th>
              </tr>
            </thead>
            <tbody>
              {(result?.items || []).map((d) => {
                const value = chosen[d.id] ?? d.operator ?? ''
                const changed = value !== (d.operator ?? '')
                return (
                  <Fragment key={d.id}>
                    <tr>
                      <td>
                        <span className="pa-bold">
                          {d.paper_subject || 'Subject not set'} - {gradeText(d.paper_grade) || 'Grade not set'}
                        </span>
                        <br />
                        <span className="es-caption">
                          #{d.id} - {d.paper_title || d.filename}
                          {d.uploaded_at ? ` - uploaded ${when(d.uploaded_at)}` : ''}
                        </span>
                        <br />
                        <Link className="es-caption" to={`/app/data-entry/paper-intake?doc=${d.id}`}>
                          Open the paper
                        </Link>
                      </td>
                      <td>
                        <StatusPill status={d.status} />
                        {d.assigned_to ? <div className="es-caption">Teacher: {d.assigned_to}</div> : null}
                      </td>
                      <td>
                        <div className="es-row" style={{ gap: 8, flexWrap: 'wrap' }}>
                          <select
                            aria-label={`Data Entry Operator for paper ${d.id}`}
                            className="es-input"
                            value={value}
                            onChange={(e) => setChosen((c) => ({ ...c, [d.id]: e.target.value }))}
                          >
                            <option value="">Nobody</option>
                            {operators.map((o) => (
                              <option key={o.identity} value={o.identity}>
                                {o.display_name && o.display_name !== o.identity
                                  ? `${o.display_name} (${o.identity})`
                                  : o.identity}
                                {o.positions ? ` - ${o.positions}` : ''}
                              </option>
                            ))}
                            {d.operator && !operators.some((o) => o.identity === d.operator) ? (
                              <option value={d.operator}>{d.operator} (no longer an operator)</option>
                            ) : null}
                          </select>
                          <Button variant="primary" size="sm" onClick={() => give(d)} disabled={!changed || busy[d.id]}>
                            {busy[d.id] ? 'Saving...' : !value ? 'Take off' : d.operator ? 'Reassign' : 'Assign'}
                          </Button>
                        </div>
                      </td>
                      <td>
                        <Button variant="ghost" size="sm" onClick={() => toggleHistory(d.id)}>
                          {openHistory === d.id ? 'Hide' : 'View'}
                        </Button>
                      </td>
                    </tr>
                    {openHistory === d.id ? (
                      <tr>
                        <td colSpan={4}>
                          {!histories[d.id] ? (
                            <p className="es-caption" style={{ margin: 0 }}>
                              Loading...
                            </p>
                          ) : (
                            <ul style={{ margin: 0, paddingLeft: 18 }}>
                              {histories[d.id].map((h, i) => (
                                <li key={i} className="es-caption">
                                  {when(h.at)} - {describe(h, nameOf)}
                                </li>
                              ))}
                            </ul>
                          )}
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {total > PAGE ? (
        <div className="es-row" style={{ gap: 8, marginTop: 12 }}>
          <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
            Previous
          </Button>
          <Button variant="secondary" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
            Next
          </Button>
        </div>
      ) : null}
    </>
  )
}
