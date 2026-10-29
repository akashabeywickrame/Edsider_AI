import { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { STATUS_LABEL } from './StatusPill.jsx'
import { loadLists } from '../pages/data-entry/EdsiderLabels.jsx'
import './PaperFilters.css'

// Finding papers among hundreds, a page at a time from the server (GET
// /intake/documents/search): the filters EdSider's assessment library has -
// upload dates, grade, medium, subject, term, author, chapter, difficulty,
// status, exam category, source, who uploaded it and year - and a search
// over file name and title. Data entry's paper finder and Paper
// assignment's table both use it.

export const PAGE = 25

// In EdSider's order. `list` is the EdSider list the choices come from;
// `sort` lists them alphabetically rather than in EdSider's own order;
// `people` takes them from the papers themselves, by name.
export const MENUS = [
  // EdSider names its grades "1" to "13".
  { key: 'grade_id', list: 'grade', label: 'Grade', name: (n) => `Grade ${n}` },
  { key: 'medium_id', list: 'language', label: 'Medium' },
  { key: 'subject_id', list: 'subject', label: 'Subject', sort: true },
  { key: 'term_id', list: 'term', label: 'Term' },
  { key: 'author_id', list: 'author', label: 'Author', sort: true },
  { key: 'chapter_id', list: 'chapter', label: 'Chapter' },
  { key: 'difficulty_id', list: 'difficulty', label: 'Difficulty' },
  { key: 'status', label: 'Status' },
  { key: 'exam_category_id', list: 'exam_category', label: 'Exam category' },
  { key: 'source_id', list: 'source', label: 'Source' },
  { key: 'uploaded_by', label: 'Created by', people: true },
  { key: 'year_id', list: 'year', label: 'Year' },
]

export const TEACHER_MENU = { key: 'teacher', label: 'Teacher', people: true }

const byName = (a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })

function withCount(label, n) {
  return `${label} (${n})`
}

export function nameOn(lists, list, id) {
  return (lists?.[list] || []).find((item) => item.id === id)?.name
}

// A menu's choices, each with how many papers have it. `statuses` narrows
// the Status menu to the ones the list can show.
function choices(menu, lists, result, statuses) {
  const counts = result?.facets?.[menu.key] || {}
  let out
  if (menu.key === 'status') {
    out = Object.entries(STATUS_LABEL)
      .filter(([value]) => !statuses || statuses.includes(value))
      .map(([value, label]) => ({ value, label }))
  } else if (menu.people) {
    const names = result?.uploaded_by_names || {}
    out = Object.keys(counts)
      .map((value) => ({ value, label: (names[value] || '').trim() || value }))
      .sort(byName)
  } else {
    const items = lists?.[menu.list] || []
    out = items.map((item) => {
      const name = (item.name || '').trim()
      return { value: String(item.id), label: name ? (menu.name ? menu.name(name) : name) : `#${item.id}` }
    })
    // A paper may carry an entry EdSider has since dropped from its list.
    for (const value of Object.keys(counts)) {
      if (!out.some((o) => o.value === value)) out.push({ value, label: `#${value}` })
    }
    if (menu.sort) out.sort(byName)
  }
  return out.map((o) => ({ ...o, label: withCount(o.label, counts[o.value] || 0) }))
}

// The search's state and the page of papers it found. `statuses` keeps the
// papers - and the menus' counts - to those statuses.
export function usePaperSearch({ menus = MENUS, statuses = null, refreshKey } = {}) {
  const empty = () =>
    Object.fromEntries([...menus.map((m) => [m.key, '']), ['uploaded_from', ''], ['uploaded_to', '']])
  const [filters, setFilters] = useState(empty)
  const [lists, setLists] = useState(null)
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const scope = statuses ? statuses.join(',') : ''

  // EdSider's lists name the choices; without them the menus still work,
  // showing ids.
  useEffect(() => {
    let live = true
    loadLists()
      .then((r) => live && setLists(r.lists || {}))
      .catch(() => live && setLists({}))
    return () => {
      live = false
    }
  }, [])

  // Search as the person types, once they pause.
  useEffect(() => {
    const t = setTimeout(() => {
      setQuery(text)
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [text])

  useEffect(() => {
    let live = true
    setLoading(true)
    api
      .searchIntakeDocuments({ ...filters, statuses: scope, q: query, limit: PAGE, offset: page * PAGE })
      .then((r) => {
        if (!live) return
        setResult(r)
        setError(null)
      })
      .catch((err) => live && setError(err.message))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [filters, scope, query, page, refreshKey, reloadKey])

  const total = result?.total ?? 0
  return {
    menus,
    statuses,
    filters,
    lists,
    text,
    setText,
    page,
    setPage,
    pages: Math.max(1, Math.ceil(total / PAGE)),
    total,
    result,
    error,
    loading,
    filtered: Boolean(query || text || Object.values(filters).some(Boolean)),
    setFilter(key, value) {
      setFilters((f) => ({ ...f, [key]: value }))
      setPage(0)
    },
    clear() {
      setFilters(empty())
      setText('')
      setPage(0)
    },
    reload: () => setReloadKey((n) => n + 1),
  }
}

// The search box, the two dates and a menu per filter.
export default function PaperFilters({ search, idPrefix = 'pf', label = 'Find a paper' }) {
  const { Button } = window.EdSider
  const { menus, statuses, filters, lists, result, text, setText, setFilter, filtered, clear } = search
  return (
    <>
      <div className="pi-row" style={{ alignItems: 'flex-end', flexWrap: 'wrap', gap: 10 }}>
        <div className="es-field" style={{ flex: '2 1 220px' }}>
          <label className="es-field__label" htmlFor={`${idPrefix}-search`}>
            {label}
          </label>
          <input
            id={`${idPrefix}-search`}
            className="es-input"
            type="search"
            placeholder="File name or paper title"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <div className="es-field" style={{ flex: '1 1 150px' }}>
          <label className="es-field__label" htmlFor={`${idPrefix}-from`}>
            Start date
          </label>
          <input
            id={`${idPrefix}-from`}
            className="es-input"
            type="date"
            value={filters.uploaded_from}
            max={filters.uploaded_to || undefined}
            onChange={(e) => setFilter('uploaded_from', e.target.value)}
          />
        </div>
        <div className="es-field" style={{ flex: '1 1 150px' }}>
          <label className="es-field__label" htmlFor={`${idPrefix}-to`}>
            End date
          </label>
          <input
            id={`${idPrefix}-to`}
            className="es-input"
            type="date"
            value={filters.uploaded_to}
            min={filters.uploaded_from || undefined}
            onChange={(e) => setFilter('uploaded_to', e.target.value)}
          />
        </div>
        {/* Always there, as EdSider's library has it; nothing to clear, it rests. */}
        <Button variant="secondary" size="sm" className="pf-clear" onClick={clear} disabled={!filtered}>
          Clear filters
        </Button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, alignItems: 'end' }}>
        {menus.map((menu) => (
          <div className="es-field" key={menu.key} style={{ minWidth: 0 }}>
            <label className="es-field__label" htmlFor={`${idPrefix}-${menu.key}`}>
              {menu.label}
            </label>
            <select
              id={`${idPrefix}-${menu.key}`}
              className="es-input"
              value={filters[menu.key]}
              onChange={(e) => setFilter(menu.key, e.target.value)}
            >
              <option value="">All</option>
              {choices(menu, lists, result, statuses).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>
    </>
  )
}

// "N papers - page 2 of 40"
export function PaperCount({ search }) {
  const { loading, result, total, filtered, page, pages } = search
  return (
    <p className="es-caption" style={{ margin: 0 }}>
      {loading && !result
        ? 'Loading papers...'
        : `${total} paper${total === 1 ? '' : 's'}${filtered ? ' match' : ''}${total > PAGE ? ` - page ${page + 1} of ${pages}` : ''}`}
    </p>
  )
}

export function Pager({ search }) {
  const { Button } = window.EdSider
  const { total, page, pages, setPage } = search
  if (total <= PAGE) return null
  return (
    <div className="es-row" style={{ gap: 8, alignItems: 'center' }}>
      <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(0)}>
        First
      </Button>
      <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
        Previous
      </Button>
      <span className="es-caption">
        Page {page + 1} of {pages}
      </span>
      <Button variant="secondary" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
        Next
      </Button>
      <Button variant="secondary" size="sm" disabled={page + 1 >= pages} onClick={() => setPage(pages - 1)}>
        Last
      </Button>
    </div>
  )
}
