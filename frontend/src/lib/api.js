import { devIdentity } from './auth.js'
// A thin wrapper around service.py's FastAPI routes - one function per
// endpoint, matching exactly what each one actually takes and returns
// (see service.py). No endpoint-specific logic lives here; that stays
// in the page that calls it.

export const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
// Matches EDSIDER_API_KEY on the server, when one is set there - a build-time
// setting (frontend/.env), never committed. Unset, the service stays open and
// this is simply never sent, exactly as it already works locally and in the
// tests. A key baked into a browser bundle is visible to anyone who opens the
// network tab or the page source - this is a gate on casual public access to
// the API, not a substitute for real sign-in.
const API_KEY = import.meta.env.VITE_API_KEY || null

// The key as a URL query parameter, for the few endpoints used directly as a
// src (see require_api_key's own note on why a header cannot reach these).
function keyParam() {
  return API_KEY ? `?api_key=${encodeURIComponent(API_KEY)}` : ''
}

function query(params) {
  const usp = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') usp.set(k, String(v))
  })
  const s = usp.toString()
  return s ? `?${s}` : ''
}

async function request(path, { method = 'GET', body, ...rest } = {}) {
  // A FormData body (file upload) sets its own multipart Content-Type,
  // boundary included - JSON.stringify-ing it or overriding the header
  // would break the upload, so it goes through untouched.
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const headers = {}
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'
  // Dev-only identity (see auth.js) - in production Easy Auth sets this header.
  const who = devIdentity()
  if (who) headers['x-ms-client-principal-name'] = who
  if (API_KEY) headers['X-API-Key'] = API_KEY
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body !== undefined ? (isForm ? body : JSON.stringify(body)) : undefined,
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

  // ---- paper intake ----
  listIntakeDocuments: (status) => request(`/intake/documents${query({ status })}`),
  uploadIntakeDocument: (file, lang, style, forceOcr = false) => {
    const form = new FormData()
    form.set('file', file)
    form.set('lang', lang)
    form.set('style', style)
    form.set('force_ocr', forceOcr ? 'true' : 'false')
    return request('/intake/documents', { method: 'POST', body: form })
  },
  getIntakeDocument: (docId) => request(`/intake/documents/${docId}`),
  // Many questions' marks in one request - [{question_id, marks}] - returns the paper's questions.
  setQuestionMarks: (docId, items) =>
    request(`/intake/documents/${docId}/marks`, { method: 'POST', body: { marks: items } }),
  updateIntakeDocument: (docId, fields) =>
    request(`/intake/documents/${docId}`, { method: 'PATCH', body: fields }),
  previewResplit: (docId, style) =>
    request(`/intake/documents/${docId}/resplit/preview`, { method: 'POST', body: { style } }),
  resplitDocument: (docId, style) =>
    request(`/intake/documents/${docId}/resplit`, { method: 'POST', body: { style } }),
  checkDocumentDuplicates: (docId) =>
    request(`/intake/documents/${docId}/check-duplicates`, { method: 'POST' }),
  // Informational only - runs before anyone has reviewed a single
  // question, so never requires it and never changes the document's status.
  precheckDocumentDuplicates: (docId) =>
    request(`/intake/documents/${docId}/precheck-duplicates`, { method: 'POST' }),

  listIntakeQuestions: (docId) => request(`/intake/documents/${docId}/questions`),
  createIntakeQuestion: (docId, payload) =>
    request(`/intake/documents/${docId}/questions`, { method: 'POST', body: payload }),
  updateIntakeQuestion: (qId, fields) =>
    request(`/intake/questions/${qId}`, { method: 'PATCH', body: fields }),
  deleteIntakeQuestion: (qId) => request(`/intake/questions/${qId}`, { method: 'DELETE' }),
  // MCQ info (stem/options/correct) rides along on every IntakeQuestionOut
  // already - see service.py's _question_out - so there is no separate
  // GET here; only saving an edited stem/options needs its own call.
  updateQuestionMcq: (qId, payload) =>
    request(`/intake/questions/${qId}/mcq`, { method: 'PATCH', body: payload }),
  // Blanks (one per fill-in-the-blank gap) ride along the same way -
  // only saving an edited answer needs its own call.
  updateQuestionBlanks: (qId, payload) =>
    request(`/intake/questions/${qId}/blanks`, { method: 'PATCH', body: payload }),

  // GET /intake/documents/{id}/file returns the uploaded file's own raw
  // bytes (a real PDF, not a transcript) - used directly as an <iframe>/
  // <img> src, never through request().
  documentFileUrl: (docId) => `${API_BASE}/intake/documents/${docId}/file${keyParam()}`,

  // ---- question images (attached to a question or its answer) ----
  listDocumentImages: (docId) => request(`/intake/documents/${docId}/images`),
  attachQuestionImage: (qId, slot, file) => {
    const form = new FormData()
    form.set('slot', slot)
    form.set('file', file)
    return request(`/intake/questions/${qId}/images`, { method: 'POST', body: form })
  },
  deleteImage: (imageId) => request(`/intake/images/${imageId}`, { method: 'DELETE' }),
  // GET /intake/images/{id} returns raw image bytes, not JSON - used
  // directly as an <img src>, never through request().
  imageUrl: (imageId) => `${API_BASE}/intake/images/${imageId}${keyParam()}`,

  // ---- teacher review (the subject teacher's app) ----
  // Registers the signed-in teacher the first time the page opens, so the
  // assigning administrator can name them (see teachers/me/touch).
  touchTeacher: () => request('/teachers/me/touch', { method: 'POST' }),
  listMyDocuments: () => request('/teachers/me/documents'),
  // The uploaded file itself, for an <iframe> or <img> - like imageUrl, not JSON.
  documentFileUrl: (docId) => `${API_BASE}/intake/documents/${docId}/file${keyParam()}`,
  listDocumentComments: (docId) => request(`/intake/documents/${docId}/comments`),
  addQuestionComment: (qId, comment) =>
    request(`/intake/questions/${qId}/comments`, { method: 'POST', body: { comment } }),
  finishReview: (docId, status) =>
    request(`/intake/documents/${docId}/finish`, { method: 'POST', body: { status } }),

  // ---- paper assignment (the assigning administrator's app) ----
  // Teachers appear here once they have opened the teacher app at least once.
  listTeachers: () => request('/teachers'),
  assignDocument: (docId, teacherIdentity) =>
    request(`/intake/documents/${docId}/assign`, {
      method: 'POST',
      body: { teacher_identity: teacherIdentity },
    }),
}
