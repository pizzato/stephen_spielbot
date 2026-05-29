// Thin fetch wrapper around the FastAPI backend. All calls go through /api,
// which the Vite dev server proxies to the backend on :8001 (see vite.config.js),
// and which the backend serves itself in production.

async function req(method, path, body) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`/api${path}`, opts)
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { detail: text } }
  if (!res.ok) {
    const msg = data?.detail || `${res.status} ${res.statusText}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

export const api = {
  getConfig: () => req('GET', '/config'),
  saveConfig: (config) => req('POST', '/config', { config }),

  generateScript: (body) => req('POST', '/script/generate', body),
  getScenes: (jobId) => req('GET', `/jobs/${jobId}/scenes`),
  saveScene: (jobId, sceneId, body) => req('PUT', `/jobs/${jobId}/scenes/${sceneId}`, body),
  regenPreview: (jobId, sceneId, resolution, style) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),

  startGeneration: (body) => req('POST', '/jobs/generate', body),
  getProgress: (workDir) => req('GET', `/progress?work_dir=${encodeURIComponent(workDir || '')}`),
  pauseJob: (workDir) => req('POST', '/jobs/pause', { work_dir: workDir }),
  resumeJob: (workDir) => req('POST', '/jobs/resume', { work_dir: workDir }),
  retryJob: (workDir) => req('POST', '/jobs/retry', { work_dir: workDir }),
  cancelJob: (workDir) => req('POST', '/jobs/cancel', { work_dir: workDir }),

  listJobs: () => req('GET', '/jobs'),
  loadRemix: (workDir) => req('GET', `/remix?work_dir=${encodeURIComponent(workDir || '')}`),
  applyRemix: (body) => req('POST', '/remix', body),

  getBadges: () => req('GET', '/badges'),
  markSeen: (section) => req('POST', '/badges/seen', { section }),

  getQueue: () => req('GET', '/queue'),
  getComments: () => req('GET', '/youtube/comments'),
  getSuggestions: () => req('GET', '/youtube/suggestions'),

  ytAuthStatus: () => req('GET', '/youtube/auth'),
  ytAuthStart: () => req('POST', '/youtube/auth/start'),
  ytAuthPoll: () => req('POST', '/youtube/auth/poll'),
  ytDisconnect: () => req('POST', '/youtube/disconnect'),
  ytPostOptions: () => req('GET', '/youtube/post/options'),
  ytPostPrefill: (workDir) => req('GET', `/youtube/post/prefill?work_dir=${encodeURIComponent(workDir || '')}`),
  ytDescribe: (body) => req('POST', '/youtube/describe', body),
  ytCover: (body) => req('POST', '/youtube/cover', body),
  ytPost: (body) => req('POST', '/youtube/post', body),
}

// Build a URL the <img>/<video> tags can load straight from the backend.
export const fileUrl = (p) => (p ? `/api/file?path=${encodeURIComponent(p)}` : '')
