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

  // The LLM/image prompts behind every generation. Edits are stored as an
  // override; resetPrompt(name) reverts one, resetPrompt() reverts them all.
  getPrompts: () => req('GET', '/prompts'),
  savePrompt: (name, fields) => req('POST', '/prompts', { name, fields }),
  resetPrompt: (name) => req('POST', '/prompts/reset', { name: name || null }),

  // UI-worker reservation (issue #98): heartbeat marks "the UI is in use" so the
  // render holds a worker idle for covers/previews; uiWorker polls that state.
  uiHeartbeat: () => req('POST', '/ui/heartbeat'),
  uiWorker: () => req('GET', '/ui/worker'),

  // voices (reference clips F5-TTS clones). `data` is a base64 / data-URL string.
  addVoice: (name, filename, data, meta = {}) => req('POST', '/voices/add', { name, filename, data, ...meta }),
  updateVoice: (name, fields) => req('POST', '/voices/update', { name, ...fields }),
  deleteVoice: (name) => req('POST', '/voices/delete', { name }),
  // Synthesize a short sample at a given robotic level (0..1) and return its URL.
  testVoice: (body) => req('POST', '/voices/test', body),
  // Measure a voice's natural cadence (words/minute) by timing a fixed passage.
  calibrateVoice: (voice, engine) => req('POST', '/voices/calibrate', { voice: voice || '', engine: engine || '' }),
  // Target length (minutes) → word budget + scene plan for a style/narrator.
  lengthEstimate: (styleName, minutes, voice) => req('GET',
    `/script/length-estimate?style_name=${encodeURIComponent(styleName || '')}` +
    `&minutes=${encodeURIComponent(minutes || 0)}&voice=${encodeURIComponent(voice || '')}`),
  // Short read-aloud script for the record-a-voice screen (issue #192).
  voiceReadingScript: (language, fresh = false) => req('POST', '/voices/reading-script', { language, fresh }),
  // Character reference images (global character library). `data` is a base64 / data-URL.
  setCharacterImage: (charId, filename, data) => req('POST', '/characters/image', { char_id: charId, filename, data }),
  clearCharacterImage: (charId) => req('POST', '/characters/image/clear', { char_id: charId }),
  selectCharacterImage: (charId, versionId) => req('POST', '/characters/image/select', { char_id: charId, version_id: versionId }),
  deleteCharacterImage: (charId, versionId) => req('POST', '/characters/image/delete', { char_id: charId, version_id: versionId }),
  generateCharacterPortrait: (charId, extraPrompt) => req('POST', '/characters/portrait', { char_id: charId, extra_prompt: extraPrompt || '' }),

  // Per-script characters (main-character consistency). Job-scoped: they live in
  // the script's own work dir, separate from the global catalogue above. Each
  // mutating call returns the fresh { characters } list. `data` is base64/data-URL.
  scriptCharacters: (jobId) => req('GET', `/jobs/${jobId}/characters`),
  addScriptCharacter: (jobId, body) => req('POST', `/jobs/${jobId}/characters`, body),
  updateScriptCharacter: (jobId, charId, body) => req('PUT', `/jobs/${jobId}/characters/${charId}`, body),
  deleteScriptCharacter: (jobId, charId) => req('DELETE', `/jobs/${jobId}/characters/${charId}`),
  setScriptCharacterImage: (jobId, charId, filename, data) => req('POST', `/jobs/${jobId}/characters/${charId}/image`, { filename, data }),
  clearScriptCharacterImage: (jobId, charId) => req('POST', `/jobs/${jobId}/characters/${charId}/image/clear`),
  selectScriptCharacterImage: (jobId, charId, versionId) => req('POST', `/jobs/${jobId}/characters/${charId}/image/select`, { version_id: versionId }),
  deleteScriptCharacterImage: (jobId, charId, versionId) => req('POST', `/jobs/${jobId}/characters/${charId}/image/delete`, { version_id: versionId }),
  generateScriptCharacterPortrait: (jobId, charId, extraPrompt) => req('POST', `/jobs/${jobId}/characters/${charId}/portrait`, { extra_prompt: extraPrompt || '' }),
  promoteScriptCharacter: (jobId, charId) => req('POST', `/jobs/${jobId}/characters/${charId}/promote`),

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
  // Story-first mode: phase 1 drafts + judges the prose story (shown for review
  // in Create), phase 2 divides the (possibly edited) story into scenes. Both
  // long-running, so same kick-off + poll pattern as generateScript.
  generateStory: async (body) => {
    const { task_id } = await req('POST', '/script/story/generate', body)
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Story generation failed.')
    }
  },
  // Music-video flow: write the song first, then generate its audio (polls
  // the shared script-task status until the track is rendered).
  songDraft: (body) => req('POST', '/song/draft', body),
  songGenerate: async (body) => {
    const { task_id } = await req('POST', '/song/generate', body)
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Song generation failed.')
    }
  },
  // "Sing this as [voice]": re-voice the generated song with seed-vc.
  songConvert: async (workDir, voice) => {
    const { task_id } = await req('POST', '/song/convert', { work_dir: workDir, voice })
    for (;;) {
      await new Promise((r) => setTimeout(r, 3000))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Voice conversion failed.')
    }
  },
  divideStory: async (body) => {
    const { task_id } = await req('POST', '/script/story/divide', body)
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Story division failed.')
    }
  },
  // Retell the prose story at a new scene count (rewrites every chapter).
  // Long-running (one LLM call per chapter), so same kick-off + poll pattern.
  redraftStory: async (jobId, body) => {
    const { task_id } = await req('POST', `/jobs/${jobId}/story/redraft`, body)
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      // the endpoint spreads the story into the response; its own "status"
      // field is clobbered by the task status, so drop it
      if (s.status === 'done') { delete s.status; return s }
      if (s.status === 'error') throw new Error(s.error || 'Story redraft failed.')
    }
  },
  getStory: (jobId) => req('GET', `/jobs/${jobId}/story`),
  // A song film's song — the caption and tagged lyrics the music model sings.
  getSong: (jobId) => req('GET', `/jobs/${jobId}/song`),
  saveSong: (jobId, caption, lyrics) => req('PUT', `/jobs/${jobId}/song`, { caption, lyrics }),
  // Persist edited chapter texts so a story review can be resumed later.
  saveStory: (jobId, chapters) => req('PUT', `/jobs/${jobId}/story`, { chapters }),
  // Script critic: post-generation QC that can rewrite, delete, and reorder
  // scenes. One pass, or loop until it proposes no more edits (converged).
  listScriptVersions: (jobId) => req('GET', `/jobs/${jobId}/script-versions`),
  restoreScriptVersion: (jobId, file) => req('POST', `/jobs/${jobId}/script-versions/restore`, { file }),
  runCritic: async (jobId, opts = {}) => {
    const { task_id } = await req('POST', `/jobs/${jobId}/critic`,
      { passes: opts.passes || 1, until_converged: !!opts.untilConverged })
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const s = await req('GET', `/script/generate/status?task_id=${encodeURIComponent(task_id)}`)
      if (s.status === 'done') return s
      if (s.status === 'error') throw new Error(s.error || 'Critic run failed.')
    }
  },
  // Improve the Create brief's title or direction in place (issue #88).
  improveBrief: (field, title, direction, styleName, instruction) =>
    req('POST', '/create/improve', { field, title, direction, style_name: styleName || '', instruction: instruction || '' }),
  loadScript: (workDir) => req('GET', `/scripts/load?work_dir=${encodeURIComponent(workDir || '')}`),
  // Performance films: every scene with its references already resolved into
  // numbered <Picture N>/<Audio N> slots (see /api/scripts/performance).
  loadPerformanceScript: (workDir) => req('GET', `/scripts/performance?work_dir=${encodeURIComponent(workDir || '')}`),
  // Per-script visuals: locations and wardrobe, the reference images that pin
  // where a scene happens and what people wear.
  // Catalogue assets: locations and wardrobe reusable across films.
  listAssets: () => req('GET', '/assets'),
  saveAssets: (assets) => req('POST', '/assets', { assets }),
  generateAssetImage: (assetId, styleName, extraPrompt) => req('POST', '/assets/image', { asset_id: assetId, style_name: styleName || '', extra_prompt: extraPrompt || '' }),
  uploadAssetImage: (assetId, filename, data) => req('POST', '/assets/upload', { asset_id: assetId, filename, data }),
  listVisuals: (jobId) => req('GET', `/jobs/${jobId}/visuals`),
  addVisual: (jobId, body) => req('POST', `/jobs/${jobId}/visuals`, body),
  updateVisual: (jobId, id, body) => req('PUT', `/jobs/${jobId}/visuals/${id}`, body),
  deleteVisual: (jobId, id) => req('DELETE', `/jobs/${jobId}/visuals/${id}`),
  generateVisualImage: (jobId, id, extraPrompt) => req('POST', `/jobs/${jobId}/visuals/${id}/image`, { extra_prompt: extraPrompt || '' }),
  visualFromUrl: (jobId, visualId, url) => req('POST', `/jobs/${jobId}/visuals/${visualId}/from-url`, { url }),
  uploadVisualImage: (jobId, id, filename, data) => req('POST', `/jobs/${jobId}/visuals/${id}/upload`, { filename, data }),
  // Copy an existing script into a fresh work dir to render again, leaving the
  // original render intact. Returns the same payload as loadScript.
  duplicateScript: (workDir, title) => req('POST', '/scripts/duplicate', { work_dir: workDir, title: title || '' }),
  getScenes: (jobId) => req('GET', `/jobs/${jobId}/scenes`),
  saveScene: (jobId, sceneId, body) => req('PUT', `/jobs/${jobId}/scenes/${sceneId}`, body),
  // Scene structure (issue #193). Scene ids are renumbered to 1..N on every
  // structural change, so each call returns the fresh { scenes } list to
  // replace local state wholesale.
  addScene: (jobId, afterSceneId) => req('POST', `/jobs/${jobId}/scenes/add`, { after_scene_id: afterSceneId || 0 }),
  deleteScene: (jobId, sceneId) => req('DELETE', `/jobs/${jobId}/scenes/${sceneId}`),
  reorderScenes: (jobId, order) => req('POST', `/jobs/${jobId}/scenes/reorder`, { order }),
  removeScenePreview: (jobId, sceneId) => req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview-remove`),
  regenPreview: (jobId, sceneId, resolution, style, instruction) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}&instruction=${encodeURIComponent(instruction || '')}`),
  generateAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  regenAllPreviews: (jobId, resolution, style) =>
    req('POST', `/jobs/${jobId}/previews?force=true&resolution=${encodeURIComponent(resolution || '')}&style=${encodeURIComponent(style || '')}`),
  selectPreview: (jobId, sceneId, versionId) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview-select`, { version_id: versionId }),
  deletePreview: (jobId, sceneId, versionId) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/preview-delete`, { version_id: versionId }),
  // Masked image edit (FLUX inpaint): `mask` is a base64 PNG data-URL where white
  // marks the region to change; `prompt` describes the change; `denoise` is the
  // edit strength (0.3–1.0, lower keeps more of the original).
  inpaintScene: (jobId, sceneId, mask, prompt, denoise) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/inpaint`, { mask, prompt, denoise }),
  regenField: (jobId, sceneId, field, body) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/regenerate-field?field=${encodeURIComponent(field)}`, body),
  // Acted scenes regenerate WHOLE (one coherent take): setting, dialogue,
  // beats, camera, sound — the prompt reassembles server-side.
  regenActedScene: (jobId, sceneId, instruction) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/regenerate-acted`, { instruction: instruction || '' }),
  // Switching a scene's type converts the content (same theme, other shape)
  // and stashes the version being left, so switching back restores it.
  convertSceneMode: (jobId, sceneId, mode) =>
    req('POST', `/jobs/${jobId}/scenes/${sceneId}/convert-mode`, { mode }),

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
  firstFrameCover: (body) => req('POST', '/remix/first-frame-cover', body),
  saveCoverPhrase: (workDir, phrase) => req('POST', '/films/cover-phrase', { work_dir: workDir, phrase }),
  listFonts: () => req('GET', '/fonts'),
  localizeFilm: (body) => req('POST', '/remix/localize', body),
  localizeScripts: (workDir) => req('GET', `/remix/localize/scripts?work_dir=${encodeURIComponent(workDir)}`),
  saveLocalizeScript: (body) => req('POST', '/remix/localize/script', body),
  localizeMetadata: (body) => req('POST', '/remix/localize/metadata', body),
  buildLocalizeAudio: (body) => req('POST', '/remix/localize/audio', body),
  listLocalizeLanguages: () => api.listTtsEngines().then((r) =>
    (r.engines || []).find((e) => e.key === 'chatterbox-multilingual')?.languages || {}),
  regenMusic: (body) => req('POST', '/remix/music', body),
  selectMusic: (workDir, versionId) => req('POST', '/remix/music-select', { work_dir: workDir, version_id: versionId }),
  selectRemixVideo: (workDir, versionId) => req('POST', '/remix/video-select', { work_dir: workDir, version_id: versionId }),
  deleteRemixVideo: (workDir, versionId) => req('POST', '/remix/video-delete', { work_dir: workDir, version_id: versionId }),

  getActivity: ({ limit } = {}) => {
    const q = limit ? `?limit=${encodeURIComponent(limit)}` : ''
    return req('GET', `/activity${q}`)
  },
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
  // Accepted ideas: list them and mark one as acted upon (queued/created).
  getAccepted: (styleName) => req('GET', '/youtube/suggestions/accepted' + (styleName ? `?style_name=${encodeURIComponent(styleName)}` : '')),
  actSuggestion: (body) => req('POST', '/youtube/suggestions/accepted/act', body),
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
  draftCommentReply: (commentId, instruction) => req('POST', '/youtube/comments/draft-reply', { comment_id: commentId, instruction: instruction || '' }),
  // community engagement drafts (issue #84)
  sendCommunityReply: (commentId, text) => req('POST', '/youtube/comments/community/send', { comment_id: commentId, text }),
  dismissCommunityReply: (commentId) => req('POST', '/youtube/comments/community/dismiss', { comment_id: commentId }),

  // queue management
  queueMove: (id, direction) => req('POST', '/queue/move', { id, direction }),
  queueRemove: (id) => req('POST', '/queue/remove', { id }),
  queueAbandon: (id) => req('POST', '/queue/abandon', { id }),
  queueRetryReply: (id) => req('POST', '/queue/retry-reply', { id }),
  queueAdd: (title, minutes, prompt, resolution, styleName) => req('POST', '/queue/add', { title, minutes: minutes || 0, prompt: prompt || '', resolution: resolution || '', style_name: styleName || '' }),
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
  publishClock: () => req('GET', '/publish/clock'),
  publishClockReset: (platform, key, nextAt = 0) => req('POST', '/publish/clock', { platform, key, next_at: nextAt }),

  ytAnalytics: (channel, refresh) => {
    const p = new URLSearchParams()
    if (channel) p.set('channel', channel)
    if (refresh) p.set('refresh', 'true')
    const qs = p.toString()
    return req('GET', '/youtube/analytics' + (qs ? `?${qs}` : ''))
  },
  // multi-channel management (issue #22) — channels live in Settings → YouTube
  ytChannels: () => req('GET', '/youtube/channels'),
  ytPlaylists: (channel) => req('GET', '/youtube/playlists' + (channel ? `?channel=${encodeURIComponent(channel)}` : '')),
  ytAuthStart: () => req('POST', '/youtube/auth/start'),
  ytAuthPoll: () => req('POST', '/youtube/auth/poll'),
  ytDisconnect: (channel) => req('POST', '/youtube/disconnect', { channel: channel || '' }),
  ytChannelSettings: (id, fields) => req('POST', '/youtube/channels/settings', { id, ...fields }),
  ytPostOptions: () => req('GET', '/youtube/post/options'),
  ytPostPrefill: (workDir) => req('GET', `/youtube/post/prefill?work_dir=${encodeURIComponent(workDir || '')}`),
  ytDescribe: (body) => req('POST', '/youtube/describe', body),
  ytPostTitle: (workDir, title, instruction) => req('POST', '/youtube/post/title', { work_dir: workDir, title: title || '', instruction: instruction || '' }),
  // Persist edited cover title + description back to the script.
  ytPostSave: (body) => req('POST', '/youtube/post/save', body),
  ytCover: (body) => req('POST', '/youtube/cover', body),
  ytCoverStatus: (taskId) => req('GET', `/youtube/cover/status?task_id=${encodeURIComponent(taskId)}`),
  // Cover image edit (mask + prompt) + version history (mirrors scene previews).
  coverHistory: (workDir) => req('GET', `/youtube/cover/history?work_dir=${encodeURIComponent(workDir || '')}`),
  coverInpaint: (workDir, mask, prompt, denoise) => req('POST', '/youtube/cover/inpaint', { work_dir: workDir, mask, prompt, denoise }),
  coverSelect: (workDir, versionId) => req('POST', '/youtube/cover/select', { work_dir: workDir, version_id: versionId }),
  coverRetext: (workDir) => req('POST', '/youtube/cover/retext', { work_dir: workDir }),
  coverDelete: (workDir, versionId) => req('POST', '/youtube/cover/delete', { work_dir: workDir, version_id: versionId }),
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
  addFilmScene: (workDir, afterSceneId) => req('POST', '/films/scenes/add', { work_dir: workDir, after_scene_id: afterSceneId || 0 }),
  deleteFilmScene: (workDir, sceneId) => req('POST', '/films/scenes/delete', { work_dir: workDir, scene_id: sceneId }),
  reorderFilmScenes: (workDir, order) => req('POST', '/films/scenes/reorder', { work_dir: workDir, order }),
  rerenderFilmScene: (workDir, sceneId, component, instruction) => req('POST', `/films/scenes/${sceneId}/rerender`, { work_dir: workDir, component, instruction: instruction || '' }),
  selectFilmPreview: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/preview-select`, { work_dir: workDir, version_id: versionId }),
  selectFilmVideo: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/video-select`, { work_dir: workDir, version_id: versionId }),
  deleteFilmPreview: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/preview-delete`, { work_dir: workDir, version_id: versionId }),
  deleteFilmVideo: (workDir, sceneId, versionId) => req('POST', `/films/scenes/${sceneId}/video-delete`, { work_dir: workDir, version_id: versionId }),
  inpaintFilmScene: (workDir, sceneId, mask, prompt, denoise) => req('POST', `/films/scenes/${sceneId}/inpaint`, { work_dir: workDir, mask, prompt, denoise }),
  trimFilmScene: (workDir, sceneId, endSeconds) => req('POST', `/films/scenes/${sceneId}/trim`, { work_dir: workDir, end_seconds: endSeconds }),
  continueFilmScene: (workDir, sceneId, { seconds, direction, lines }) =>
    req('POST', `/films/scenes/${sceneId}/continue`, { work_dir: workDir, seconds, direction, lines }),
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
