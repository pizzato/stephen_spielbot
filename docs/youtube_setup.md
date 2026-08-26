# YouTube Integration Setup

Stephen Spielbot uses the YouTube Data API v3 to fetch channel comments and upload generated videos.

## Prerequisites

- A Google account with a YouTube channel
- Python packages installed: `pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client`

---

## Step 1 — Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Give it a name (e.g. "Stephen Spielbot") → **Create**
4. Make sure the new project is selected in the top dropdown

---

## Step 2 — Enable YouTube Data API v3

1. In the left sidebar: **APIs & Services** → **Library**
2. Search for "YouTube Data API v3"
3. Click it → **Enable**

---

## Step 3 — Configure the OAuth Consent Screen

1. **APIs & Services** → **OAuth consent screen**
2. Choose **External** → **Create**
3. Fill in:
   - App name: "Stephen Spielbot"
   - User support email: your email
   - Developer contact: your email
4. Click **Save and Continue** through the **Scopes** screen (no changes needed)
5. On the **Test users** screen, click **+ Add Users**
6. **Enter your own Google account email address** → click **Add** → **Save and Continue**
7. Click **Back to Dashboard**

> **Critical:** You MUST add yourself as a test user in step 5–6, otherwise Google will show
> "Access blocked: app has not completed verification" when you try to connect. This is a
> Google requirement for apps in testing mode. Only the emails you add here can authorize the app.

---

## Step 4 — Create OAuth 2.0 Credentials

1. **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: "Stephen Spielbot Desktop" → **Create**
5. Click **Download JSON** — save this file as `client_secrets.json` at:
   `~/.config/video-generator/client_secrets.json`

---

## Step 5 — Configure in Stephen Spielbot

1. Open Stephen Spielbot and go to **Settings → Channels**
2. In the **Google API** card, verify the **Client secrets file** path shows:
   `~/.config/video-generator/client_secrets.json`
   (one OAuth app is shared by every channel you connect)

---

## Step 6 — Connect a Channel

1. In **Settings → Channels**, click **Connect channel**
2. A Google authorization window opens **on the machine running the server**
3. Sign in with **the same Google account you added as a test user in Step 3**
4. You may see a warning "Google hasn't verified this app" — click **Advanced** → **Go to Stephen Spielbot (unsafe)**
5. Grant the requested permissions
6. The channel appears in the Channels list

If you don't finish the login (for example the window opened on the wrong
machine — it always opens on the server, so connect from a browser on that
machine, not over Tailscale), the flow gives up after **5 minutes** with a
timeout error and the **Connect channel** button becomes clickable again.

Each connected channel stores its own OAuth token under
`~/.config/video-generator/` (`youtube_token_<key>.json`; the reserved "default"
channel uses the legacy `youtube_token.json`) and refreshes automatically. You can
connect **multiple channels** — each style picks which channel it publishes to in its
style card, and each channel has its own settings (privacy, category, language,
captions, engagement auto-reply, publish cadence) in the Channels list.

---

## Usage

### Fetching and Evaluating Comments

1. Go to the **Community** screen and click **Fetch & evaluate** — comments are pulled
   from every connected channel and ranked by the LLM; video requests are flagged
2. For a flagged request, click **Approve → queue** to add it to the render queue
   (optionally editing the title first)
3. Non-request comments can get AI-drafted engagement replies per channel, if enabled
   in that channel's settings

### Publishing

Finished videos land in the **publish queue** (Publishing screen). Per channel you can
publish three ways:

- **Manual** — review each finished video and publish it yourself
- **Immediate** — auto-post the moment a render finishes
- **Scheduled** — release queued videos on a per-channel cadence (e.g. N per day);
  reorder with **Manual order**, or force one out with **Publish now**

Uploads attach the script-based captions, topic tags, and the style's playlist, and
respect the channel's privacy/category/language settings. When the video's style has a
narration language set (`tts_language`), that language is stamped on the upload and its
caption track (on both YouTube and X), overriding the channel-level default.

---

## Troubleshooting

**"Access blocked: Stephen Spielbot has not completed the Google verification process"** — You need to add your Google account as a test user. Go back to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **OAuth consent screen** → **Test users** → **+ Add Users** → enter your email → Save. Then try connecting again.

**"client_secrets.json not configured"** — Check the path in Settings → Channels → Google API. Default: `~/.config/video-generator/client_secrets.json`

**"Not authenticated"** — Connect the channel in Settings → Channels.

**"Google hasn't verified this app" warning** — This is expected. Click **Advanced** → **Go to Stephen Spielbot (unsafe)** to proceed.

**Token expired errors** — Tokens auto-refresh. If a channel's refresh token dies, the app shows a "Reconnect YouTube" banner — reconnect that channel in Settings → Channels.

**Upload quota errors** — YouTube API has a daily quota (10,000 units/day). Each video upload costs ~1,600 units. If you hit the limit, wait until the quota resets at midnight Pacific Time.
