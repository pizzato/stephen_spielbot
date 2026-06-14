// Thin fetch wrapper around the FastAPI backend. All calls go through /api,
// which the Vite dev server proxies to the backend on :8001 (see vite.config.js),
// and which the backend serves itself in production.

async function req(method, path, body) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  // A bare fetch() rejection (TypeError) means the connection failed before any
  // response — e.g. the server closed an idle keep-alive socket just as a poll
  // reused it. These are transient, so retry idempotent GETs a couple of times
  // before surfacing the error. POSTs aren't retried (avoid double-submits).
  let res
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(`/api${path}`, opts)
      break
    } catch (e) {
      if (method !== 'GET' || attempt >= 2) throw e
      await new Promise((r) => setTimeout(r, 150 * (attempt + 1)))
    }
  }
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
  workerStatus: () => req('GET', '/workers/status'),

  // voices (reference clips F5-TTS clones). `data` is a base64 / data-URL string.
  addVoice: (name, filename, data) => req('POST', '/voices/add', { name, filename, data }),
  updateVoice: (name, fields) => req('POST', '/voices/update', { name, ...fields }),
  deleteVoice: (name) => req('POST', '/voices/delete', { name }),
  // Synthesize a short sample at a given robotic level (0..1) and return its URL.
  testVoice: (body) => req('POST', '/voices/test', body),

  generateScript: (body) => req('POST', '/script/generate', body),
  loadScript: (workDir) => req('GET', `/scripts/load?work_dir=${encodeURIComponent(workDir || '')}`),
  getScenes: (jobId) => req('GET', `/jobs/${jobId}/scenes`),
  saveScene: (jobId, sceneId, body) => req('PUT', `/jobs/${jobId}/scenes/${sceneId}`, body),
  regenPreview: (jobId, sceneId, resolution, style) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  generateAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  regenAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?force=true&resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  regenField: (jobId, sceneId, field, body) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/regenerate-field?field=${encodeURIComponent(field)}`, body),

  startGeneration: (body) => req('POST', '/jobs/generate', body),
  getProgress: (workDir) => req('GET', `/progress?work_dir=${encodeURIComponent(workDir || '')}`),
  pauseJob: (workDir) => req('POST', '/jobs/pause', { work_dir: workDir }),
  resumeJob: (workDir) => req('POST', '/jobs/resume', { work_dir: workDir }),
  retryJob: (workDir) => req('POST', '/jobs/retry', { work_dir: workDir }),
  cancelJob: (workDir) => req('POST', '/jobs/cancel', { work_dir: workDir }),
  deleteJob: (workDir) => req('POST', '/jobs/delete', { work_dir: workDir }),

  listJobs: () => req('GET', '/jobs'),
  deleteFilm: (workDir) => req('POST', '/films/delete', { work_dir: workDir }),
  loadRemix: (workDir) => req('GET', `/remix?work_dir=${encodeURIComponent(workDir || '')}`),
  applyRemix: (body) => req('POST', '/remix', body),

  getActivity: () => req('GET', '/activity'),
  getBadges: () => req('GET', '/badges'),
  markSeen: (section) => req('POST', '/badges/seen', { section }),

  getQueue: () => req('GET', '/queue'),
  getComments: () => req('GET', '/youtube/comments'),
  getSuggestions: (guidance, refresh, styleName) => {
    const p = new URLSearchParams()
    if (guidance) p.set('guidance', guidance)
    if (refresh) p.set('refresh', 'true')
    if (styleName) p.set('style_name', styleName)
    const qs = p.toString()
    return req('GET', '/youtube/suggestions' + (qs ? `?${qs}` : ''))
  },
  dismissSuggestion: (body) => req('POST', '/youtube/suggestions/dismiss', body),

  // comment actions
  fetchComments: (autoApprove) => req('POST', '/youtube/comments/fetch', { auto_approve: autoApprove ?? null }),
  approveComment: (commentId, finalTitle) => req('POST', '/youtube/comments/approve', { comment_id: commentId, final_title: finalTitle || '' }),
  rejectComment: (commentId) => req('POST', '/youtube/comments/reject', { comment_id: commentId }),
  replyComment: (commentId, text) => req('POST', '/youtube/comments/reply', { comment_id: commentId, text }),
  // community engagement drafts (issue #84)
  sendCommunityReply: (commentId, text) => req('POST', '/youtube/comments/community/send', { comment_id: commentId, text }),
  dismissCommunityReply: (commentId) => req('POST', '/youtube/comments/community/dismiss', { comment_id: commentId }),

  // queue management
  queueMove: (id, direction) => req('POST', '/queue/move', { id, direction }),
  queueRemove: (id) => req('POST', '/queue/remove', { id }),
  queueAbandon: (id) => req('POST', '/queue/abandon', { id }),
  queueRetryReply: (id) => req('POST', '/queue/retry-reply', { id }),
  queueAdd: (title, nScenes, prompt, resolution, styleName) => req('POST', '/queue/add', { title, n_scenes: nScenes || 0, prompt: prompt || '', resolution: resolution || '', style_name: styleName || '' }),
  queueUpdate: (id, fields) => req('POST', '/queue/update', { id, ...fields }),
  queueStart: (id) => req('POST', '/queue/start', { id }),
  queueFromJob: (body) => req('POST', '/queue/from-job', body),

  // automation steps
  autoFetch: () => req('POST', '/automation/fetch'),
  autoStart: () => req('POST', '/automation/start'),
  autoPost: () => req('POST', '/automation/post'),
  autoTick: () => req('POST', '/automation/tick'),

  ytAnalytics: (channel) => req('GET', '/youtube/analytics' + (channel ? `?channel=${encodeURIComponent(channel)}` : '')),
  // multi-channel management (issue #22) — channels live in Settings → YouTube
  ytChannels: () => req('GET', '/youtube/channels'),
  ytAuthStart: () => req('POST', '/youtube/auth/start'),
  ytAuthPoll: () => req('POST', '/youtube/auth/poll'),
  ytDisconnect: (channel) => req('POST', '/youtube/disconnect', { channel: channel || '' }),
  ytChannelSettings: (id, fields) => req('POST', '/youtube/channels/settings', { id, ...fields }),
  ytPostOptions: () => req('GET', '/youtube/post/options'),
  ytPostPrefill: (workDir) => req('GET', `/youtube/post/prefill?work_dir=${encodeURIComponent(workDir || '')}`),
  ytDescribe: (body) => req('POST', '/youtube/describe', body),
  ytCover: (body) => req('POST', '/youtube/cover', body),
  ytCoverStatus: (taskId) => req('GET', `/youtube/cover/status?task_id=${encodeURIComponent(taskId)}`),
  ytThumbnail: (body) => req('POST', '/youtube/thumbnail', body),
  ytPost: (body) => req('POST', '/youtube/post', body),
  ytPostStatus: (taskId) => req('GET', `/youtube/post/status?task_id=${encodeURIComponent(taskId)}`),

  // film scene editor (post-render)
  filmScenes: (workDir) => req('GET', `/films/scenes?work_dir=${encodeURIComponent(workDir || '')}`),
  deleteFilmScene: (workDir, sceneId) => req('POST', '/films/scenes/delete', { work_dir: workDir, scene_id: sceneId }),
  reorderFilmScenes: (workDir, order) => req('POST', '/films/scenes/reorder', { work_dir: workDir, order }),
  rerenderFilmScene: (workDir, sceneId, component) => req('POST', `/films/scenes/${sceneId}/rerender`, { work_dir: workDir, component }),
  reassembleFilm: (workDir) => req('POST', '/films/reassemble', { work_dir: workDir }),
  filmTaskStatus: (taskId) => req('GET', `/films/task?task_id=${encodeURIComponent(taskId)}`),
  filmTasksForWorkDir: (workDir) => req('GET', `/films/tasks?work_dir=${encodeURIComponent(workDir || '')}`),

  // engagement prediction (issue #50); per-channel models (issue #22)
  engagementStatus: (channel) => req('GET', '/engagement/status' + (channel ? `?channel=${encodeURIComponent(channel)}` : '')),
  engagementBuild: (channel) => req('POST', '/engagement/build', { channel: channel || '' }),
  engagementBuildStatus: (taskId) => req('GET', `/engagement/build/status?task_id=${encodeURIComponent(taskId)}`),
  engagementPredict: (body) => req('POST', '/engagement/predict', body),
  engagementBestTimes: (body) => req('POST', '/engagement/best-times', body),
}

// Build a URL the <img>/<video> tags can load straight from the backend.
export const fileUrl = (p) => (p ? `/api/file?path=${encodeURIComponent(p)}` : '')
