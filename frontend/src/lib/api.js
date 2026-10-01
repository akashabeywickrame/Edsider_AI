// A thin wrapper around service.py's FastAPI routes - one function per
// endpoint, matching exactly what each one actually takes and returns
// (see service.py). No endpoint-specific logic lives here; that stays
// in the page that calls it.

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

function query(params) {
  const usp = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') usp.set(k, String(v))
  })
  const s = usp.toString()
  return s ? `?${s}` : ''
}

async function request(path, { method = 'GET', body, ...rest } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    ...rest,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const err = await res.json()
      if (err.detail) detail = err.detail
    } catch {
      // Not a JSON error body - the status line is all there is.
    }
    throw new Error(detail)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/health'),

  // ---- check a question (CheckQuestion.dc.html) ----
  checkQuestion: (payload) => request('/check', { method: 'POST', body: payload }),

  // ---- whole-bank + within-paper duplicates ----
  findDuplicates: (params) => request(`/duplicates${query(params)}`),
  withinPaperDuplicates: (params) => request(`/within-paper-duplicates${query(params)}`),
  listAssessmentIds: () => request('/assessments'),

  // ---- reviewed decisions ----
  listDecisions: (verdict) => request(`/decisions${query({ verdict })}`),
  createDecision: (payload) => request('/decisions', { method: 'POST', body: payload }),
  undoDecision: (a, b) => request(`/decisions${query({ a, b })}`, { method: 'DELETE' }),
  decisionsSchema: () => request('/decisions/schema'),

  // ---- search by meaning ----
  searchByMeaning: (q, topK) => request(`/search${query({ q, top_k: topK })}`),

  // ---- question bank ----
  listQuestions: (params) => request(`/questions${query(params)}`),
  questionFacets: () => request('/questions/facets'),

  // Paper intake/teacher/assignment endpoints (file uploads, multipart
  // form bodies) are added here once the pages that use them are built -
  // no point shaping that wrapper before there is a real caller for it.
}
