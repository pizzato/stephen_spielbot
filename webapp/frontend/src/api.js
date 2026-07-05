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
  // Image engines (model bundles) + automated model download onto the workers.
  listEngines: () => req('GET', '/models/engines'),
  installEngine: (engine) => req('POST', '/models/install', { engine }),
  installEngineStatus: (taskId) => req('GET', `/models/install/status?task_id=${encodeURIComponent(taskId)}`),
  // TTS narration models (per-style voice engine) — reuses the install-status machinery.
  listTtsEngines: () => req('GET', '/models/tts-engines'),
  installTtsEngine: (engine) => req('POST', '/models/tts-install', { engine }),
  // Start/stop/restart one host's worker containers over SSH (action: start|stop|restart).
  controlWorker: (host, action) => req('POST', '/workers/control', { host, action }),

  // settings backup / restore (issue #106). Backup downloads straight from the
  // browser via an <a download> hitting backupUrl(); restore POSTs the zip as
  // a base64 data-URL (same shape as voice uploads).
  backupUrl: (scope) => `/api/settings/backup?scope=${encodeURIComponent(scope || 'full')}`,
  restoreSettings: (data) => req('POST', '/settings/restore', { data }),

  // UI-worker reservation (issue #98): heartbeat marks "the UI is in use" so the
  // render holds a worker idle for covers/previews; uiWorker polls that state.
  uiHeartbeat: () => req('POST', '/ui/heartbeat'),
  uiWorker: () => req('GET', '/ui/worker'),

  // voices (reference clips F5-TTS clones). `data` is a base64 / data-URL string.
  addVoice: (name, filename, data) => req('POST', '/voices/add', { name, filename, data }),
  updateVoice: (name, fields) => req('POST', '/voices/update', { name, ...fields }),
  deleteVoice: (name) => req('POST', '/voices/delete', { name }),
  // Synthesize a short sample at a given robotic level (0..1) and return its URL.
  testVoice: (body) => req('POST', '/voices/test', body),
  // Character reference images (global character library). `data` is a base64 / data-URL.
  setCharacterImage: (charId, filename, data) => req('POST', '/characters/image', { char_id: charId, filename, data }),
  clearCharacterImage: (charId) => req('POST', '/characters/image/clear', { char_id: charId }),
  generateCharacterPortrait: (charId, extraPrompt) => req('POST', '/characters/portrait', { char_id: charId, extra_prompt: extraPrompt || '' }),

  // Script generation is several Claude calls (tens of seconds). Holding one long
  // POST open meant any blip on that connection surfaced as a "NetworkError" even
  // though the script was created — so kick it off, then poll the status (short
  // GETs, which req() already retries on transient failures) until it's ready.
  generateScript: async (body) => {
    const { task_id } = await req('POST', '/script/generate', body)
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Script generation failed.')
    }
  },
  // Improve the Create brief's title or direction in place (issue #88).
  improveBrief: (field, title, direction, styleName) =>
    req('POST', '/create/improve', { field, title, direction, style_name: styleName || '' }),
  loadScript: (workDir) => req('GET', `/scripts/load?work_dir=${encodeURIComponent(workDir || '')}`),
  // Copy an existing script into a fresh work dir to render again, leaving the
  // original render intact. Returns the same payload as loadScript.
  duplicateScript: (workDir, title) => req('POST', '/scripts/duplicate', { work_dir: workDir, title: title || '' }),
  getScenes: (jobId) => req('GET', `/jobs/${jobId}/scenes`),
  saveScene: (jobId, sceneId, body) => req('PUT', `/jobs/${jobId}/scenes/${sceneId}`, body),
  regenPreview: (jobId, sceneId, resolution, style) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  generateAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  regenAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?force=true&resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  selectPreview: (jobId, sceneId, versionId) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview-select`, { version_id: versionId }),
  // Masked image edit (FLUX inpaint): `mask` is a base64 PNG data-URL where white
  // marks the region to change; `prompt` describes the change; `denoise` is the
  // edit strength (0.3–1.0, lower keeps more of the original).
  inpaintScene: (jobId, sceneId, mask, prompt, denoise) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/inpaint`, { mask, prompt, denoise }),
  regenField: (jobId, sceneId, field, body) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/regenerate-field?field=${encodeURIComponent(field)}`, body),

  startGeneration: (body) => req('POST', '/jobs/generate', body),
  getProgress: (workDir) => req('GET', `/progress?work_dir=${encodeURIComponent(workDir || '')}`),
  pauseJob: (workDir) => req('POST', '/jobs/pause', { work_dir: workDir }),
  resumeJob: (workDir) => req('POST', '/jobs/resume', { work_dir: workDir }),
  retryJob: (workDir) => req('POST', '/jobs/retry', { work_dir: workDir }),
  cancelJob: (workDir) => req('POST', '/jobs/cancel', { work_dir: workDir }),
  deleteJob: (workDir) => req('POST', '/jobs/delete', { work_dir: workDir }),
  markJobSeen: (workDir) => req('POST', '/jobs/seen', { work_dir: workDir }),

  listJobs: () => req('GET', '/jobs'),
  deleteFilm: (workDir) => req('POST', '/films/delete', { work_dir: workDir }),
  loadRemix: (workDir) => req('GET', `/remix?work_dir=${encodeURIComponent(workDir || '')}`),
  applyRemix: (body) => req('POST', '/remix', body),
  regenNarrator: (body) => req('POST', '/remix/narrator', body),
  upscaleRemixVideo: (body) => req('POST', '/remix/upscale', body),
  regenMusic: (body) => req('POST', '/remix/music', body),
  selectMusic: (workDir, versionId) => req('POST', '/remix/music-select', { work_dir: workDir, version_id: versionId }),
  selectRemixVideo: (workDir, versionId) => req('POST', '/remix/video-select', { work_dir: workDir, version_id: versionId }),

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
  // Discarded ideas: list them, bring one back, or forget it for good.
  getDiscarded: (styleName) => req('GET', '/youtube/suggestions/discarded' + (styleName ? `?style_name=${encodeURIComponent(styleName)}` : '')),
  reviveSuggestion: (body) => req('POST', '/youtube/suggestions/revive', body),
  forgetSuggestion: (body) => req('POST', '/youtube/suggestions/forget', body),
  // Empty the declined ("not accepted") ideas list (Settings → Automation).
  resetDeclinedSuggestions: () => req('POST', '/youtube/suggestions/discarded/reset'),

  // comment actions
  fetchComments: (autoApprove) => req('POST', '/youtube/comments/fetch', { auto_approve: autoApprove ?? null }),
  approveComment: (commentId, finalTitle) => req('POST', '/youtube/comments/approve', { comment_id: commentId, final_title: finalTitle || '' }),
  rejectComment: (commentId) => req('POST', '/youtube/comments/reject', { comment_id: commentId }),
  replyComment: (commentId, text) => req('POST', '/youtube/comments/reply', { comment_id: commentId, text }),
  // LLM-draft a reply to any comment (issue #88): manual composer + draft regenerate.
  draftCommentReply: (commentId) => req('POST', '/youtube/comments/draft-reply', { comment_id: commentId }),
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
  queueApprove: (id, approved = true) => req('POST', '/queue/approve', { id, approved }),
  queueStart: (id) => req('POST', '/queue/start', { id }),
  queueFromJob: (body) => req('POST', '/queue/from-job', body),

  // automation steps
  autoFetch: () => req('POST', '/automation/fetch'),
  autoStart: () => req('POST', '/automation/start'),
  autoPost: () => req('POST', '/automation/post'),
  autoTick: () => req('POST', '/automation/tick'),

  // publish scheduler — a publish queue decoupled from rendering. Finished
  // videos are released to YouTube/X on each channel/account's own cadence.
  publishQueue: () => req('GET', '/publish/queue'),
  publishScan: () => req('POST', '/publish/scan'),
  publishRemove: (id, platform) => req('POST', '/publish/remove', { id, platform: platform || '' }),
  publishNow: (id) => req('POST', '/publish/now', { id }),
  publishMove: (id, direction) => req('POST', '/publish/move', { id, direction }),
  publishApprove: (workDir, approved = true) => req('POST', '/publish/approve', { work_dir: workDir, approved }),

  ytAnalytics: (channel, refresh) => {
    const p = new URLSearchParams()
    if (channel) p.set('channel', channel)
    if (refresh) p.set('refresh', 'true')
    const qs = p.toString()
    return req('GET', '/youtube/analytics' + (qs ? `?${qs}` : ''))
  },
  // multi-channel management (issue #22) — channels live in Settings → YouTube
  ytChannels: () => req('GET', '/youtube/channels'),
  ytAuthStart: () => req('POST', '/youtube/auth/start'),
  ytAuthPoll: () => req('POST', '/youtube/auth/poll'),
  ytDisconnect: (channel) => req('POST', '/youtube/disconnect', { channel: channel || '' }),
  ytChannelSettings: (id, fields) => req('POST', '/youtube/channels/settings', { id, ...fields }),
  ytPostOptions: () => req('GET', '/youtube/post/options'),
  ytPostPrefill: (workDir) => req('GET', `/youtube/post/prefill?work_dir=${encodeURIComponent(workDir || '')}`),
  ytDescribe: (body) => req('POST', '/youtube/describe', body),
  ytPostTitle: (workDir, title) => req('POST', '/youtube/post/title', { work_dir: workDir, title: title || '' }),
  // Persist edited cover title + description back to the script.
  ytPostSave: (body) => req('POST', '/youtube/post/save', body),
  ytCover: (body) => req('POST', '/youtube/cover', body),
  ytCoverStatus: (taskId) => req('GET', `/youtube/cover/status?task_id=${encodeURIComponent(taskId)}`),
  // Cover image edit (mask + prompt) + version history (mirrors scene previews).
  coverHistory: (workDir) => req('GET', `/youtube/cover/history?work_dir=${encodeURIComponent(workDir || '')}`),
  coverInpaint: (workDir, mask, prompt, denoise) => req('POST', '/youtube/cover/inpaint', { work_dir: workDir, mask, prompt, denoise }),
  coverSelect: (workDir, versionId) => req('POST', '/youtube/cover/select', { work_dir: workDir, version_id: versionId }),
  ytThumbnail: (body) => req('POST', '/youtube/thumbnail', body),
  ytPost: (body) => req('POST', '/youtube/post', body),
  ytPostStatus: (taskId) => req('GET', `/youtube/post/status?task_id=${encodeURIComponent(taskId)}`),

  // X (Twitter) — mirrors the YouTube account/auth/post methods (issue #107).
  xAccounts: () => req('GET', '/x/accounts'),
  xAuthStart: () => req('POST', '/x/auth/start'),
  xAuthPoll: () => req('POST', '/x/auth/poll'),
  xImportTokens: (accessToken, refreshToken) => req('POST', '/x/auth/import', { access_token: accessToken, refresh_token: refreshToken || '' }),
  xImportKeys: (keys) => req('POST', '/x/auth/import-keys', keys),
  xDisconnect: (account) => req('POST', '/x/disconnect', { channel: account || '' }),
  xAccountSettings: (id, fields) => req('POST', '/x/accounts/settings', { id, ...fields }),
  xPost: (body) => req('POST', '/x/post', body),
  xPostStatus: (taskId) => req('GET', `/x/post/status?task_id=${encodeURIComponent(taskId)}`),
  xAnalytics: (account, refresh) => {
    const p = new URLSearchParams()
    if (account) p.set('account', account)
    if (refresh) p.set('refresh', 'true')
    const qs = p.toString()
    return req('GET', '/x/analytics' + (qs ? `?${qs}` : ''))
  },
  // X mentions ("comments") — mirror the YouTube comment actions.
  xComments: () => req('GET', '/x/comments'),
  xFetchComments: (autoApprove) => req('POST', '/x/comments/fetch', { auto_approve: autoApprove ?? null }),
  xApproveComment: (commentId, finalTitle) => req('POST', '/x/comments/approve', { comment_id: commentId, final_title: finalTitle || '' }),
  xRejectComment: (commentId) => req('POST', '/x/comments/reject', { comment_id: commentId }),
  xReplyComment: (commentId, text) => req('POST', '/x/comments/reply', { comment_id: commentId, text }),
  xSendCommunityReply: (commentId, text) => req('POST', '/x/comments/community/send', { comment_id: commentId, text }),
  xDismissCommunityReply: (commentId) => req('POST', '/x/comments/community/dismiss', { comment_id: commentId }),

  // film scene editor (post-render)
  filmScenes: (workDir) => req('GET', `/films/scenes?work_dir=${encodeURIComponent(workDir || '')}`),
  deleteFilmScene: (workDir, sceneId) => req('POST', '/films/scenes/delete', { work_dir: workDir, scene_id: sceneId }),
  reorderFilmScenes: (workDir, order) => req('POST', '/films/scenes/reorder', { work_dir: workDir, order }),
  rerenderFilmScene: (workDir, sceneId, component) => req('POST', `/films/scenes/${sceneId}/rerender`, { work_dir: workDir, component }),
  selectFilmPreview: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/preview-select`, { work_dir: workDir, version_id: versionId }),
  selectFilmVideo: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/video-select`, { work_dir: workDir, version_id: versionId }),
  inpaintFilmScene: (workDir, sceneId, mask, prompt, denoise) => req('POST', `/films/scenes/${sceneId}/inpaint`, { work_dir: workDir, mask, prompt, denoise }),
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
