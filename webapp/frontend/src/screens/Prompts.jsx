import { useState, useEffect, useRef } from 'react'
import { Card, Button, Banner, Chip, Icon } from '../components.jsx'
import { api } from '../api.js'

// The raw instructions sent to the LLM / image models behind every generation.
// Opened from Settings → Infrastructure, deliberately off the sidebar: a bad
// edit here degrades every future video, so each prompt is read-only until
// explicitly unlocked and saving asks for confirmation.
//
// The app's own prompts.yaml stays the baseline and is never written to — edits
// are stored as an override, so "Revert to original" always has somewhere to go.

const FIELD_LABEL = {
  system: 'System — the role and rules the model is given',
  user: 'User — the request itself, filled in per video',
  value: 'Value',
}

// ${...} tokens the app fills in at generation time. Dropping one from a prompt
// silently starves the model of that content, so they're surfaced explicitly.
const missingTokens = (field, text) =>
  (field.placeholders || []).filter((p) => !(text || '').includes('${' + p + '}'))

function Placeholders({ names, missing }) {
  if (!names.length) return null
  return (
    <div className="row center gap-10 row--wrap" style={{ marginTop: 8, fontSize: 11.5 }}>
      <span className="muted">Fills in:</span>
      {names.map((p) => (
        <code key={p} style={{
          fontSize: 11, padding: '1px 6px', borderRadius: 4,
          background: missing.includes(p) ? 'var(--danger-soft)' : 'var(--surface-2)',
          color: missing.includes(p) ? 'var(--danger)' : 'var(--ink-2)',
          textDecoration: missing.includes(p) ? 'line-through' : 'none',
        }}>{'${' + p + '}'}</code>
      ))}
    </div>
  )
}

function PromptCard({ prompt, expanded, onToggle, onSaved, onError }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({})
  const [busy, setBusy] = useState(false)

  // Collapsing (or a reload landing new text) drops any half-typed edit.
  useEffect(() => { if (!expanded) { setEditing(false); setDraft({}) } }, [expanded])

  const startEdit = () => {
    setDraft(Object.fromEntries(prompt.fields.map((f) => [f.key, f.value])))
    setEditing(true)
  }
  const textOf = (f) => (editing ? (draft[f.key] ?? f.value) : f.value)
  const changed = editing && prompt.fields.some((f) => draft[f.key] !== f.value)
  const missing = prompt.fields.flatMap((f) => missingTokens(f, textOf(f)))
  const blank = editing && prompt.fields.some((f) => !(draft[f.key] || '').trim())

  const save = async () => {
    const warn = missing.length
      ? `\n\nThis prompt no longer uses ${missing.map((p) => '${' + p + '}').join(', ')} — the app will stop passing that content to the model.`
      : ''
    if (!window.confirm(`Save “${prompt.name}”?\n\nThis changes how every future generation is written.${warn}`)) return
    setBusy(true)
    try {
      const r = await api.savePrompt(prompt.name, draft)
      setEditing(false)
      onSaved(r.prompts)
    } catch (e) { onError(e.message) } finally { setBusy(false) }
  }

  const revert = async () => {
    if (!window.confirm(`Revert “${prompt.name}” to the original prompt? Your edits to it are discarded.`)) return
    setBusy(true)
    try {
      const r = await api.resetPrompt(prompt.name)
      setEditing(false)
      onSaved(r.prompts)
    } catch (e) { onError(e.message) } finally { setBusy(false) }
  }

  return (
    <Card span={12} className="reveal">
      <div className="row center between" style={{ cursor: 'pointer' }} onClick={onToggle}>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row center gap-10">
            <code style={{ fontSize: 13, fontWeight: 700 }}>{prompt.name}</code>
            {prompt.modified && <Chip tone="warn" dot>Edited</Chip>}
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{prompt.description}</div>
        </div>
        <Icon name={expanded ? 'chevron-up' : 'chevron-down'} />
      </div>

      {expanded && (
        <div style={{ marginTop: 16 }}>
          {prompt.fields.map((f) => {
            const text = textOf(f)
            return (
              <div key={f.key} style={{ marginBottom: 16 }}>
                <div className="label-sm" style={{ marginBottom: 6 }}>{FIELD_LABEL[f.key] || f.key}</div>
                <textarea
                  className="textarea"
                  rows={Math.min(24, Math.max(4, (text || '').split('\n').length + 1))}
                  readOnly={!editing}
                  value={text}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  style={{
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                    fontSize: 12, lineHeight: 1.5,
                    opacity: editing ? 1 : 0.75,
                    cursor: editing ? 'text' : 'default',
                  }}
                />
                <Placeholders names={f.placeholders || []} missing={missingTokens(f, text)} />
              </div>
            )
          })}

          {editing && missing.length > 0 && (
            <Banner tone="warn">
              Removed {missing.map((p) => '${' + p + '}').join(', ')} — the app fills those in at generation time,
              so the model will no longer receive that content.
            </Banner>
          )}

          <div className="row center gap-10 row--wrap mt-16">
            {!editing && <Button icon="pen" onClick={startEdit} disabled={busy}>Edit prompt</Button>}
            {editing && (
              <>
                <Button variant="primary" icon="check" onClick={save} disabled={busy || !changed || blank}>
                  {busy ? 'Saving…' : 'Save'}
                </Button>
                <Button onClick={() => { setEditing(false); setDraft({}) }} disabled={busy}>Cancel</Button>
                {blank && <span className="muted" style={{ fontSize: 11.5 }}>A prompt cannot be empty — use Revert to restore the original.</span>}
              </>
            )}
            <div className="grow" />
            {prompt.modified && (
              <Button variant="danger" icon="rotate-left" onClick={revert} disabled={busy}>Revert to original</Button>
            )}
          </div>
        </div>
      )}
    </Card>
  )
}

export default function Prompts({ go, leaveGuardRef }) {
  const [prompts, setPrompts] = useState(null)
  const [overridePath, setOverridePath] = useState('')
  const [expanded, setExpanded] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getPrompts()
      .then((r) => { setPrompts(r.prompts); setOverridePath(r.override_path) })
      .catch((e) => { setError(e.message); setPrompts([]) })
  }, [])

  // An expanded card may hold unsaved text; warn before navigating away. A ref
  // read inside the guard, so the guard identity stays stable.
  const expandedRef = useRef('')
  expandedRef.current = expanded
  useEffect(() => {
    if (leaveGuardRef) leaveGuardRef.current = () => !expandedRef.current ||
      window.confirm('Any unsaved prompt edits will be lost. Leave?')
    return () => { if (leaveGuardRef) leaveGuardRef.current = null }
  }, [leaveGuardRef])

  const editedCount = (prompts || []).filter((p) => p.modified).length

  const revertAll = async () => {
    if (!window.confirm(`Revert all ${editedCount} edited prompt${editedCount === 1 ? '' : 's'} to the originals? Your edits are discarded.`)) return
    setBusy(true)
    try {
      const r = await api.resetPrompt()
      setPrompts(r.prompts)
      setExpanded('')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Settings · Advanced</span>
          <h1 className="display-md reveal reveal-d1">Prompts</h1>
        </div>
        <div className="row center gap-10">
          {editedCount > 0 && (
            <Button variant="danger" icon="rotate-left" onClick={revertAll} disabled={busy}>
              Revert all to original
            </Button>
          )}
          <Button icon="arrow-left" onClick={() => go('settings')}>Back to settings</Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>

      <Banner tone="warn">
        These are the raw instructions sent to the language and image models for every film — scripts, narration,
        image prompts, descriptions, tags and replies. Editing them changes all future generations. Each prompt is
        read-only until you choose <strong>Edit prompt</strong>.
      </Banner>

      <div className="field__hint" style={{ margin: '12px 0 20px' }}>
        The app's built-in prompts are never overwritten, so <strong>Revert to original</strong> always restores the
        shipped wording — and prompts you haven't edited keep improving with app updates.
        {editedCount > 0 && <> Your edits are stored in <code>{overridePath}</code> and are included in a full settings backup.</>}
      </div>

      {prompts === null
        ? <div className="muted">Loading…</div>
        : (
          <div className="bento">
            {prompts.map((p) => (
              <PromptCard
                key={p.name}
                prompt={p}
                expanded={expanded === p.name}
                onToggle={() => setExpanded((cur) => (cur === p.name ? '' : p.name))}
                onSaved={(next) => { setPrompts(next); setError('') }}
                onError={setError}
              />
            ))}
          </div>
        )}
    </div>
  )
}
