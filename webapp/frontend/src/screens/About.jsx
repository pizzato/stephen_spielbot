import { Card, Chip, Icon } from '../components.jsx'
// Generated from channels.yaml by scripts/gen_channels.py (`make channels`) — a
// GitHub Action regenerates it on merge, so edit the YAML, never this JSON.
import channels from '../channels.json'

const CHANNELS_YAML_URL = 'https://github.com/pizzato/stephen_spielbot/blob/main/channels.yaml'

// platform → icon. Anything unrecognised falls back to a generic globe.
const PLATFORM_ICON = {
  youtube: { name: 'youtube', brand: true },
  x: { name: 'x-twitter', brand: true },
  other: { name: 'globe', brand: false },
}

// External links about the project.
const LINKS = [
  {
    label: 'GitHub',
    desc: 'Source code, docs and setup',
    icon: 'github', brand: true,
    href: 'https://github.com/pizzato/stephen_spielbot',
  },
  {
    label: 'The story',
    desc: '“I will never direct a movie again” — how it was built',
    icon: 'medium', brand: true,
    href: 'https://medium.com/@pizzato/i-will-never-direct-a-movie-again-65bd4e9e6797',
  },
  {
    label: 'Watch it work',
    desc: '“I Built an AI That Makes Movies While I Sleep” — a video tour',
    icon: 'youtube', brand: true,
    href: 'https://www.youtube.com/watch?v=1XMU1_QnRa4',
  },
]

// What the pipeline does, in one line each — mirrors the README, kept short.
const PIPELINE = [
  ['feather-pointed', 'Script', 'An LLM writes a multi-scene script with visual prompts and narration'],
  ['image', 'Images', 'FLUX paints each scene’s first frame, with consistent recurring characters'],
  ['film', 'Video', 'LTX or MiniMax H3 animates every scene from its still through ComfyUI'],
  ['microphone-lines', 'Narration', 'F5-TTS speaks the script with voice cloning'],
  ['masks-theater', 'Acted scenes', 'In dialogue, mixed and silent films, MiniMax H3 performs the characters — picture and voice generated together from their portraits'],
  ['music', 'Music', 'ACE-Step or MiniMax Music 3 scores it from a mood description'],
  ['guitar', 'Music videos', 'Or the film is the song: the music model sings the story’s lyrics and the cast performs them on camera'],
  ['clapperboard', 'Assembly', 'FFmpeg cuts it all into one finished film'],
]

function LinkTile({ label, desc, icon, brand, href, soon }) {
  const inner = (
    <>
      <span className="row center between">
        <span className="row center gap-10">
          <Icon name={icon} brand={brand} style={{ fontSize: 18, color: 'var(--ink-2)' }} />
          <span className="h-title" style={{ fontSize: 16 }}>{label}</span>
        </span>
        {soon
          ? <Chip tone="neutral">Coming soon</Chip>
          : <Icon name="arrow-up-right-from-square" style={{ color: 'var(--ink-4)', fontSize: 12 }} />}
      </span>
      <p className="muted" style={{ fontSize: 13, margin: '10px 0 0' }}>{desc}</p>
    </>
  )
  if (soon || !href) {
    return <Card span={4} style={{ opacity: 0.7 }}>{inner}</Card>
  }
  return <Card span={4} link href={href}>{inner}</Card>
}

export default function About() {
  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">About</span>
          <h1 className="display-lg reveal reveal-d1">Stephen Spielbot</h1>
        </div>
      </div>

      <div className="bento">
        {/* Hero — who/what the studio is */}
        <Card span={8} padLg className="reveal reveal-d1">
          <div className="row center gap-16">
            <img src="/assets/StephenSpielbot.png" alt=""
              style={{ width: 72, height: 72, borderRadius: 18, border: '1px solid var(--line)' }} />
            <div>
              <div className="h-title">Stephen Spielbot</div>
              <div className="muted" style={{ fontSize: 13 }}>AI slop video director</div>
            </div>
          </div>
          <p className="body-1 mt-16" style={{ fontSize: 14.5, lineHeight: 1.6 }}>
            Stephen Spielbot turns a single topic into a fully produced short film — cinematic
            visuals, spoken narration or scenes acted out by its characters, and a mood-matched
            score, cut together automatically. Around the
            pipeline, the app runs the whole channel: a render queue with automation, AI-suggested
            ideas, per-scene editing, and publishing to YouTube and X with a scheduler, captions and
            community replies.
          </p>
        </Card>

        {/* Author */}
        <Card span={4} className="reveal reveal-d2" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          <span className="label-sm">Made by</span>
          <div className="h-title mt-8" style={{ fontSize: 18 }}>Luiz Pizzato</div>
          <p className="muted" style={{ fontSize: 13, margin: '8px 0 16px' }}>
            An experiment in end-to-end automated filmmaking.
          </p>
          <div className="stack gap-8">
            <a className="row center gap-10" href="https://luiz.pizzato.cc" target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--accent)', textDecoration: 'none' }}>
              <Icon name="globe" /> luiz.pizzato.cc
            </a>
            <a className="row center gap-10" href="https://github.com/pizzato" target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--accent)', textDecoration: 'none' }}>
              <Icon name="github" brand /> github.com/pizzato
            </a>
          </div>
        </Card>

        {/* How it works */}
        <Card span={12} className="reveal reveal-d2">
          <span className="label-sm">How it works</span>
          <div className="mt-16" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 16 }}>
            {PIPELINE.map(([icon, title, desc], i) => (
              <div key={i} className="row gap-12" style={{ alignItems: 'flex-start' }}>
                <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)', flex: '0 0 auto' }}>
                  <Icon name={icon} />
                </span>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700 }}>{title}</div>
                  <div className="muted" style={{ fontSize: 12.5, marginTop: 2, lineHeight: 1.5 }}>{desc}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>

        {/* Channels using the tool — sourced from channels.yaml, PR-contributed */}
        <Card span={12} className="reveal reveal-d3">
          <div className="card__head">
            <span className="label-sm">Channels using this tool</span>
            <a href={CHANNELS_YAML_URL} target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--accent)', textDecoration: 'none' }}>
              Add yours →
            </a>
          </div>
          {channels.length === 0 ? (
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No channels listed yet — <a href={CHANNELS_YAML_URL} target="_blank" rel="noopener noreferrer"
                style={{ color: 'var(--accent)' }}>be the first</a>.
            </p>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 12 }}>
              {channels.map((c) => {
                const ico = PLATFORM_ICON[c.platform] || PLATFORM_ICON.other
                return (
                  <a key={c.url} href={c.url} target="_blank" rel="noopener noreferrer"
                    className="row center gap-12"
                    style={{
                      padding: '10px 12px', borderRadius: 'var(--r-md)', textDecoration: 'none',
                      border: '1px solid var(--line)', background: 'var(--paper-2)', color: 'inherit',
                    }}>
                    <span className="stream-ico" style={{ background: 'var(--paper)', color: 'var(--ink-2)', flex: '0 0 auto' }}>
                      <Icon name={ico.name} brand={ico.brand} />
                    </span>
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: 'block', fontSize: 13.5, fontWeight: 700 }}>{c.name}</span>
                      <span className="muted" style={{ display: 'block', fontSize: 12, marginTop: 1 }}>
                        {[c.handle, c.note].filter(Boolean).join(' · ')}
                      </span>
                    </span>
                  </a>
                )
              })}
            </div>
          )}
        </Card>

        {/* Links */}
        {LINKS.map((l) => <LinkTile key={l.label} {...l} />)}
      </div>
    </div>
  )
}
