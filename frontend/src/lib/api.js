import { devIdentity, isDevAuth } from './auth.js'
// A thin wrapper around service.py's FastAPI routes - one function per
// endpoint, matching exactly what each one actually takes and returns
// (see service.py). No endpoint-specific logic lives here; that stays
// in the page that calls it.

// Empty means "the address this page came from" - the API serves the
// built page itself (see service.py's FRONTEND_DIR), which is what lets
// the sign-in reach the API. `??`, not `||`: an empty value is a real
// choice here, not a missing one. Unset (npm run dev with no .env) it is
// the local API.
// A trailing slash is dropped: every path below starts with one, and the
// pre-prod build's address ended in "/", so every call went to "//me".
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/+$/, '')
// Matches EDSIDER_API_KEY on the server, when one is set there - a build-time
// setting (frontend/.env), never committed. Unset, the service stays open and
// this is simply never sent, exactly as it already works locally and in the
// tests. A key baked into a browser bundle is visible to anyone who opens the
// network tab or the page source - this is a gate on casual public access to
// the API, not a substitute for real sign-in.
const API_KEY = import.meta.env.VITE_API_KEY || null


function query(params) {
  const usp = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') usp.set(k, String(v))
  })
  const s = usp.toString()
  return s ? `?${s}` : ''
}

// The signed-in person's Google/Microsoft ID token, from Easy Auth's own
// session on the address this page came from (/.auth/me). The API is a
// different App Service, which Easy Auth never tells who signed in - so
// the page forwards this token and the API verifies it itself (signature,
// audience, expiry - see src/id_tokens.py). Kept until a minute before it
// expires, so it costs one /.auth/me per hour or so, not one per call.
let session = { token: null, exp: 0, provider: null, fetched: false }

function tokenExpiry(token) {
  try {
    const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(part)).exp || 0
  } catch {
    return 0
  }
}

export async function signInSession() {
  if (isDevAuth) return session
  const now = Date.now() / 1000
  if (session.fetched && (!session.token || session.exp - 60 > now)) return session
  try {
    const res = await fetch('/.auth/me', { credentials: 'same-origin' })
    const entries = res.ok ? await res.json() : []
    const entry = Array.isArray(entries) ? entries.find((e) => e.id_token) || entries[0] : null
    const token = entry?.id_token || null
    session = { token, exp: token ? tokenExpiry(token) : 0, provider: entry?.provider_name || null, fetched: true }
  } catch {
    session = { token: null, exp: 0, provider: null, fetched: true }
  }
  return session
}

// Who is calling, and the API key - on every request, JSON or file alike.
async function authHeaders() {
  const headers = {}
  // Dev-only identity (see auth.js) - in production Easy Auth sets this header.
  const who = devIdentity()
  if (who) headers['x-ms-client-principal-name'] = who
  const { token } = await signInSession()
  if (token) headers['X-EdsiderAI-Id-Token'] = token
  if (API_KEY) headers['X-API-Key'] = API_KEY
  return headers
}

function failure(res, detail) {
  // A detail may be {message, problems} - what stops a paper being sent to
  // EdSider, one problem per question - kept on the error for the page.
  let text = typeof detail === 'string' ? detail : detail?.message
  // A paper someone has deleted while another page still had it open.
  const gone = res.status === 404 && /^No (intake document with id|paper #)/.test(text || '')
  if (gone) text = 'This paper has been deleted.'
  const error = new Error(text || `${res.status} ${res.statusText}`)
  error.paperDeleted = gone
  // Lets a caller tell "refused" (403) apart from "service down" or a
  // bug - RequireUser needs exactly that distinction.
  error.status = res.status
  if (detail && Array.isArray(detail.problems)) error.problems = detail.problems
  return error
}

// A file's raw bytes (a PDF, a picture) as a Blob - fetched with the same
// headers as every other call. A file used to be shown by pointing an
// <iframe>/<img> straight at its address, but a browser loading a src
// cannot attach a header, so once the API needed the sign-in token every
// paper viewer and attached picture showed {"detail":"Sign in first."}.
// See useFileUrl, which turns this into something an <iframe> can show.
export async function fetchFile(path) {
  const res = await fetch(`${API_BASE}${path}`, { headers: await authHeaders() })
  if (!res.ok) throw failure(res)
  return res.blob()
}

async function request(path, { method = 'GET', body, ...rest } = {}) {
  // A FormData body (file upload) sets its own multipart Content-Type,
  // boundary included - JSON.stringify-ing it or overriding the header
  // would break the upload, so it goes through untouched.
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const headers = await authHeaders()
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body !== undefined ? (isForm ? body : JSON.stringify(body)) : undefined,
    ...rest,
  })
  if (!res.ok) {
    let detail = null
    try {
      const err = await res.json()
      if (err.detail) detail = err.detail
    } catch {
      // Not a JSON error body - the status line is all there is.
    }
    throw failure(res, detail)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/health'),

  // ---- who is signed in ----
  // A 403 here means Easy Auth signed someone in but their email is not
  // on the allowlist (see service.py's current_identity()) - every
  // top-level Layout calls this once, through useMe()/RequireUser, and
  // shows a "contact your administrator" page instead of the app rather
  // than letting the error surface mid-page.
  me: () => request('/me'),

  // ---- staff profiles (super admin only) ----
  listStaff: () => request('/admin/users'),
  addStaff: (payload) => request('/admin/users', { method: 'POST', body: payload }),
  // Only the fields sent change - {role: null} clears a super admin's role.
  updateStaff: (email, fields) =>
    request(`/admin/users/${encodeURIComponent(email)}`, { method: 'PATCH', body: fields }),
  removeStaff: (email) =>
    request(`/admin/users/${encodeURIComponent(email)}`, { method: 'DELETE' }),

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
  // EdSider's own reference lists as last copied - {synced_at, lists: {kind: [...]}}.
  edsiderLists: () => request('/edsider/lists'),
  // One page of papers for the paper finder - {total, items, facets}.
  searchIntakeDocuments: (filters) => request(`/intake/documents/search${query(filters)}`),
  uploadIntakeDocument: (file, lang, style, forceOcr = false) => {
    const form = new FormData()
    form.set('file', file)
    form.set('lang', lang)
    form.set('style', style)
    form.set('force_ocr', forceOcr ? 'true' : 'false')
    return request('/intake/documents', { method: 'POST', body: form })
  },
  // A paper's text read elsewhere (the local OCR tool), pasted in -
  // split and stored exactly like an upload.
  createDocumentFromText: (text, title, style) =>
    request('/intake/documents/from-text', { method: 'POST', body: { text, title: title || null, style } }),
  getIntakeDocument: (docId) => request(`/intake/documents/${docId}`),
  // A paper uploaded by mistake - only while it is still Extracted.
  deleteIntakeDocument: (docId) => request(`/intake/documents/${docId}`, { method: 'DELETE' }),
  // Many questions' marks in one request - [{question_id, marks}] - returns the paper's questions.
  // Divides a part into sections at the questions named; joins them back.
  splitSections: (docId, label, starts) =>
    request(`/intake/documents/${docId}/sections/split`, { method: 'POST', body: { label, starts } }),
  joinSections: (docId, part) =>
    request(`/intake/documents/${docId}/sections/join`, { method: 'POST', body: { part } }),
  setQuestionMarks: (docId, items) =>
    request(`/intake/documents/${docId}/marks`, { method: 'POST', body: { marks: items } }),
  updateIntakeDocument: (docId, fields) =>
    request(`/intake/documents/${docId}`, { method: 'PATCH', body: fields }),
  previewResplit: (docId, style) =>
    request(`/intake/documents/${docId}/resplit/preview`, { method: 'POST', body: { style } }),
  resplitDocument: (docId, style) =>
    request(`/intake/documents/${docId}/resplit`, { method: 'POST', body: { style } }),
  // A question repeated on its own paper: data entry flags it (repeat_of is
  // the question it copies); the teacher replaces it or keeps it.
  flagRepeat: (qid, repeatOf, note) =>
    request(`/intake/questions/${qid}/flag`, { method: 'POST', body: { repeat_of: repeatOf, note } }),
  unflagRepeat: (qid) => request(`/intake/questions/${qid}/flag`, { method: 'DELETE' }),
  replaceQuestion: (qid, body) => request(`/intake/questions/${qid}/replace`, { method: 'POST', body }),
  // A teacher's correction to a question's wording; the earlier wording is kept in its history.
  correctQuestion: (qid, body) => request(`/intake/questions/${qid}/correct`, { method: 'POST', body }),
  keepQuestion: (qid, note) => request(`/intake/questions/${qid}/keep`, { method: 'POST', body: { note } }),
  // A question now and every earlier version of it.
  questionHistory: (qid) => request(`/intake/questions/${qid}/history`),
  // Reads the paper's own file again - its tables as pictures, and any
  // reader fix since the upload - and replaces its questions.
  rereadDocument: (docId, style) =>
    request(`/intake/documents/${docId}/reread`, { method: 'POST', body: { style } }),
  // Data entry's last step: the paper's questions are copied to the question
  // bank and the paper goes on for a teacher's review.
  finishDataEntry: (docId) =>
    request(`/intake/documents/${docId}/finish-data-entry`, { method: 'POST' }),
  // An administrator's last step once a paper is ready to publish.
  archiveDocument: (docId) => request(`/intake/documents/${docId}/archive`, { method: 'POST' }),
  // Writes the paper into EdSider (ready to publish there) and archives it here.
  sendToEdsider: (docId) =>
    request(`/intake/documents/${docId}/send-to-edsider`, { method: 'POST', body: {} }),
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

  // The uploaded file's own raw bytes (a real PDF, not a transcript) and an
  // attached picture's - paths for fetchFile / useFileUrl, never a src.
  documentFilePath: (docId) => `/intake/documents/${docId}/file`,
  imagePath: (imageId) => `/intake/images/${imageId}`,

  // ---- question images (attached to a question or its answer) ----
  listDocumentImages: (docId) => request(`/intake/documents/${docId}/images`),
  attachQuestionImage: (qId, slot, file) => {
    const form = new FormData()
    form.set('slot', slot)
    form.set('file', file)
    return request(`/intake/questions/${qId}/images`, { method: 'POST', body: form })
  },
  deleteImage: (imageId) => request(`/intake/images/${imageId}`, { method: 'DELETE' }),
  // A picture's label (printed under it, "A"; empty clears it) and/or
  // where it belongs: { label, slot } - slot 'question', 'answer', 'option:<label>'.
  editImage: (imageId, changes) =>
    request(`/intake/images/${imageId}`, { method: 'PATCH', body: changes }),

  // ---- teacher review (the subject teacher's app) ----
  // Registers the signed-in teacher the first time the page opens, so the
  // assigning administrator can name them (see teachers/me/touch).
  touchTeacher: () => request('/teachers/me/touch', { method: 'POST' }),
  listMyDocuments: () => request('/teachers/me/documents'),
  listDocumentComments: (docId) => request(`/intake/documents/${docId}/comments`),
  addQuestionComment: (qId, comment) =>
    request(`/intake/questions/${qId}/comments`, { method: 'POST', body: { comment } }),
  finishReview: (docId, status) =>
    request(`/intake/documents/${docId}/finish`, { method: 'POST', body: { status } }),

  // ---- paper assignment (the assigning administrator's app) ----
  // EdSider's own teachers, plus anyone who has opened the teacher app here.
  listTeachers: () => request('/teachers'),
  // Re-reads EdSider's teacher list into our copy - fails with a clear
  // message (e.g. EdSider unreachable) while the last copy stays usable.
  syncTeachers: () => request('/teachers/sync', { method: 'POST' }),
  // A paper is reviewed by one teacher - its author, or another teacher.
  assignDocument: (docId, teacherIdentities) =>
    request(`/intake/documents/${docId}/assign`, {
      method: 'POST',
      body: { teacher_identities: teacherIdentities },
    }),

  // Takes a paper off its teacher - back to Ready to assign.
  unassignDocument: (docId) => request(`/intake/documents/${docId}/unassign`, { method: 'POST' }),

  // Everything that happened to a paper, oldest first.
  documentHistory: (docId) => request(`/intake/documents/${docId}/history`),
}
