export type UploadResult = { document_id: string; status: string }

export type ReferenceItem = {
  index: number
  text: string
}

export type StageItem = {
  key: string
  label: string
  weight: number
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
  started_at?: number | null
  ended_at?: number | null
  duration_ms?: number | null
}

export type FailureItem = {
  stage: string
  message: string
  retryable: boolean
  chunk?: number | null
  retry_count: number
}

export type DocumentStatus = {
  document_id: string
  status: string
  source_type: string
  source_filename: string
  updated_at?: string | null
  last_opened_at?: string | null
  original_pdf_url?: string | null
  translated_pdf_url?: string | null
  annotated_pdf_url?: string | null
  merged_pdf_url?: string | null
  references: ReferenceItem[]
  progress: number
  current_stage?: string | null
  current_stage_label?: string | null
  eta_seconds?: number | null
  stages: StageItem[]
  failure?: FailureItem | null
  last_read_page: number
  last_read_ratio: number
}

export type DocumentSummary = {
  document_id: string
  status: string
  source_type: string
  source_filename: string
  size_bytes: number
  created_at?: string | null
  updated_at?: string | null
  last_opened_at?: string | null
  has_translated_pdf: boolean
  title: string
  year: string
}

export type AnnotationItem = {
  id: string
  page: number
  quote: string
  color: string
  note: string
  position_ratio: number
  created_at: string
}

export type LibrarySearchHit = {
  document_id: string
  document_title: string
  side: 'original' | 'translated'
  snippet: string
  position_ratio: number
}

// Reading theme: light, a gray dim that keeps dark ink, or dark.
export type Theme = 'light' | 'gray' | 'dark'

export type UserSettings = {
  api_key_configured: boolean
  provider: string
  base_url: string
  model: string
  theme: Theme
  show_annotated_pdf: boolean
  translation_domain: TranslationDomain
  favorites: string[]
}

export type TranslationDomain = 'cs' | 'medical' | 'general'

export const TRANSLATION_DOMAINS: {
  id: TranslationDomain
  label: string
  hint: string
}[] = [
  {
    id: 'cs',
    label: '计算机科学',
    hint: '面向计算机与人工智能研究：模型、系统、算法与数据集名称保留英文，代码标识符不翻译。'
  },
  {
    id: 'medical',
    label: '医学',
    hint: '面向医学与生物医学研究：疾病、药物与检验指标使用规范医学译名，基因与量表缩写保留英文。'
  },
  {
    id: 'general',
    label: '通用学术',
    hint: '面向各学科论文：使用规范学术书面语，尚无统一译名的术语保留英文原名。'
  }
]

export type GlossarySnapshot = {
  domain: TranslationDomain
  label: string
  updated_at?: string | null
  term_count: number
  pending_count: number
  interval_minutes: number
  size_bytes: number
}

export type ProviderSettingsDraft = {
  api_key: string
  clear_api_key?: boolean
  provider: string
  base_url: string
  model: string
}

export type ProviderPreset = {
  id: string
  label: string
  base_url: string
  model: string
  models: string[]
  key_hint: string
}

// OpenAI-compatible providers. Selecting one prefills the endpoint fields;
// "custom" leaves them untouched for any other compatible service.
export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-flash',
    models: ['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner'],
    key_hint: 'platform.deepseek.com 申请'
  },
  {
    id: 'siliconflow',
    label: '硅基流动 SiliconFlow',
    base_url: 'https://api.siliconflow.cn/v1',
    model: 'Qwen/Qwen2.5-7B-Instruct',
    models: [
      'Qwen/Qwen2.5-7B-Instruct',
      'Qwen/Qwen2.5-72B-Instruct',
      'deepseek-ai/DeepSeek-V3',
      'THUDM/GLM-4-9B-0414'
    ],
    key_hint: 'cloud.siliconflow.cn 申请'
  }
]


// Production (including the portable app) serves the frontend and the API from
// the same origin; only the Vite dev server needs an explicit backend URL.
const BACKEND = (import.meta.env.VITE_BACKEND_URL || (
  import.meta.env.DEV ? 'http://localhost:8000' : ''
)).replace(/\/$/, '')

export class ApiError extends Error {
  status?: number
  code?: string
}

async function apiFetch(path: string, init: RequestInit = {}, expectJson = true) {
  const res = await fetch(`${BACKEND}${path}`, init)
  if (!res.ok) {
    const text = await res.text()
    let message = text || res.statusText
    let code: string | undefined
    try {
      const payload = JSON.parse(text)
      const detail = payload?.detail
      if (typeof detail === 'string') message = detail
      else if (detail && typeof detail === 'object') {
        message = detail.message || message
        code = detail.code
      }
    } catch {
      // Plain-text errors are already useful.
    }
    const error = new ApiError(message)
    error.status = res.status
    error.code = code
    throw error
  }
  if (!expectJson) return res
  return res.json()
}

export function makeDataUrl(path?: string | null): string {
  if (!path) return ''
  return `${BACKEND}${path}`
}

export async function getSettings(): Promise<UserSettings> {
  return apiFetch('/api/settings/me')
}

export async function updateSettings(payload: Partial<UserSettings>): Promise<UserSettings> {
  return apiFetch('/api/settings/me', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

export async function updateProviderSettings(
  payload: Partial<ProviderSettingsDraft>
): Promise<UserSettings> {
  return apiFetch('/api/settings/me/providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

export async function getGlossary(domain: TranslationDomain): Promise<GlossarySnapshot> {
  return apiFetch(`/api/glossary/${domain}`)
}

export async function refreshGlossary(domain: TranslationDomain): Promise<GlossarySnapshot> {
  return apiFetch(`/api/glossary/${domain}/refresh`, { method: 'POST' })
}

export async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch('/api/upload', { method: 'POST', body: form })
}

export async function getDocumentStatus(documentId: string): Promise<DocumentStatus> {
  return apiFetch(`/api/document/${documentId}`)
}

export async function retryDocument(documentId: string): Promise<{
  document_id: string
  status: 'queued'
  resume_from: string
}> {
  return apiFetch(`/api/document/${documentId}/retry`, { method: 'POST' })
}

export async function cancelDocument(documentId: string): Promise<{ document_id: string; status: string }> {
  return apiFetch(`/api/document/${documentId}/cancel`, { method: 'POST' })
}

export async function reprocessDocument(documentId: string): Promise<{
  document_id: string
  status: 'queued'
  resume_from: string
}> {
  return apiFetch(`/api/document/${documentId}/reprocess`, { method: 'POST' })
}

export async function ensureAnnotatedPdf(documentId: string): Promise<string> {
  const payload = await apiFetch(`/api/document/${documentId}/annotated-pdf`, { method: 'POST' })
  return payload.annotated_pdf_url
}

export async function ensureMergedPdf(documentId: string): Promise<string> {
  const payload = await apiFetch(`/api/document/${documentId}/merged-pdf`, { method: 'POST' })
  return payload.merged_pdf_url
}

export async function listDocuments(): Promise<DocumentSummary[]> {
  return apiFetch('/api/documents')
}

export type DeleteDocumentResult = {
  ok: boolean
  document_id: string
  // One line per path the server removed, including any it could not remove.
  removed: string[]
}

export async function deleteDocument(documentId: string): Promise<DeleteDocumentResult> {
  return apiFetch(`/api/document/${documentId}`, { method: 'DELETE' })
}

export async function renameDocument(documentId: string, name: string): Promise<DocumentStatus> {
  return apiFetch(`/api/document/${documentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  })
}

export async function locateCounterpart(payload: {
  documentId: string
  source_side: 'original' | 'translated'
  selected_text: string
  source_page?: number
  source_page_count?: number
}): Promise<{ target_text: string; position_ratio: number; confidence: number; alignment_method: string; highlight_text: string }> {
  return apiFetch(`/api/document/${payload.documentId}/locate-counterpart`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_side: payload.source_side,
      selected_text: payload.selected_text,
      source_page: payload.source_page,
      source_page_count: payload.source_page_count
    })
  })
}

export async function listAnnotations(documentId: string): Promise<AnnotationItem[]> {
  return apiFetch(`/api/document/${documentId}/annotations`)
}

export async function createAnnotation(
  documentId: string,
  payload: { page: number; quote: string; color: string; note: string; position_ratio: number }
): Promise<AnnotationItem> {
  return apiFetch(`/api/document/${documentId}/annotations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

export async function deleteAnnotation(documentId: string, annotationId: string): Promise<void> {
  await apiFetch(`/api/document/${documentId}/annotations/${annotationId}`, { method: 'DELETE' })
}

export async function updateReadingProgress(
  documentId: string,
  page: number,
  ratio: number
): Promise<void> {
  await apiFetch(`/api/document/${documentId}/progress`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ page, ratio })
  })
}

export async function downloadNotes(documentId: string): Promise<void> {
  const res = await apiFetch(`/api/document/${documentId}/notes.md`, {}, false)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'reading-notes.md'
  link.click()
  URL.revokeObjectURL(url)
}

export async function searchLibrary(query: string): Promise<LibrarySearchHit[]> {
  const params = new URLSearchParams({ q: query })
  return apiFetch(`/api/search?${params.toString()}`)
}

export async function getDocumentBibtex(documentId: string): Promise<{ bibtex: string; filename: string }> {
  return apiFetch(`/api/document/${documentId}/bibtex`)
}

export function translatedPdfName(sourceFilename: string): string {
  const leaf = (sourceFilename || 'document.pdf').split(/[\\/]/).pop() || 'document.pdf'
  const stem = leaf.replace(/\.[^.]+$/, '') || 'document'
  return `${stem}_Chinese_ver.pdf`
}

export function annotatedPdfName(sourceFilename: string): string {
  const leaf = (sourceFilename || 'document.pdf').split(/[\\/]/).pop() || 'document.pdf'
  const stem = leaf.replace(/\.[^.]+$/, '') || 'document'
  return `${stem}_原文标注.pdf`
}

export function mergedPdfName(sourceFilename: string): string {
  const leaf = (sourceFilename || 'document.pdf').split(/[\\/]/).pop() || 'document.pdf'
  const stem = leaf.replace(/\.[^.]+$/, '') || 'document'
  return `${stem}_双语对照.pdf`
}

