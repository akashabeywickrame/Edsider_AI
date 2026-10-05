import { useState } from 'react'
import { api } from '../../lib/api.js'
import './Search.css'

export default function Search() {
  const { Button, Notice, Score, QuestionCard, PageHeader } = window.EdSider

  const [q, setQ] = useState('')
  const [topK, setTopK] = useState(5)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [hits, setHits] = useState(null)

  async function handleSearch(e) {
    e.preventDefault()
    if (!q.trim()) return
    setLoading(true)
    setError(null)
    try {
      const res = await api.searchByMeaning(q, topK)
      setHits(res)
    } catch (err) {
      setError(err.message)
      setHits(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <PageHeader
        accent="Search"
        title="by meaning"
        subtitle="Finds questions by what they ask about, not by matching words - a short search phrase is compared against every question's meaning, the same way a new question is checked against the bank."
      />

      <form className="es-card sr-form" onSubmit={handleSearch}>
        <div className="es-field">
          <label className="es-field__label" htmlFor="sr-q">
            What are you looking for?
          </label>
          <input
            id="sr-q"
            className="es-input"
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. the causes of the French Revolution"
          />
        </div>

        <div className="es-row sr-row2">
          <div className="es-field sr-topk">
            <label className="es-field__label" htmlFor="sr-topk">
              Results
            </label>
            <select
              id="sr-topk"
              className="es-input"
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
            >
              {[5, 10, 20].map((n) => (
                <option key={n} value={n}>
                  Top {n}
                </option>
              ))}
            </select>
          </div>
          <Button variant="primary" arrow type="submit" disabled={!q.trim() || loading}>
            {loading ? 'Searching...' : 'Search'}
          </Button>
        </div>
      </form>

      {error ? (
        <Notice tone="error" title="Could not search">
          {error}
        </Notice>
      ) : null}

      {hits && hits.length === 0 ? (
        <Notice tone="info">No question in the bank reads like that.</Notice>
      ) : null}

      <div className="sr-results">
        {(hits || []).map((hit) => (
          <article className="es-card sr-hit" key={hit.question.id}>
            <Score score={hit.score} legend={false} />
            <QuestionCard
              id={hit.question.id}
              text={hit.question.text}
              grade={hit.question.grade}
              subject={hit.question.subject}
              topic={hit.question.topic}
              contributor={hit.question.contributor}
            />
          </article>
        ))}
      </div>
    </>
  )
}
