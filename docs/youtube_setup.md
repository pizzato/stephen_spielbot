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

1. Open Stephen Spielbot and go to the **⚙️ Config** tab
2. In the **YouTube Integration** section, verify the path shows:
   `~/.config/video-generator/client_secrets.json`
3. Configure your preferences:
   - **Auto-approve** comment requests (confidence ≥ 70%)
   - **Auto-post** videos when generation completes
   - Default **Privacy** setting for uploads (start with "private" to verify uploads look correct)
   - Default **Category**

---

## Step 6 — Connect Your Account

1. Go to the **📺 YouTube** tab
2. Click **Connect YouTube**
3. Your browser will open a Google authorization page
4. Sign in with **the same Google account you added as a test user in Step 3**
5. You may see a warning "Google hasn't verified this app" — click **Advanced** → **Go to Stephen Spielbot (unsafe)**
6. Grant the requested permissions
7. The tab will show **Connected to "[Your Channel Name]"**

The OAuth token is saved at `~/.config/video-generator/youtube_token.json` and will be automatically refreshed. You only need to connect once.

---

## Usage

### Fetching and Evaluating Comments

1. In the **📺 YouTube** tab, click **Fetch Comments** to load recent channel comments
2. Click **Evaluate All with AI** — the LLM will analyze each comment and flag video requests (highlighted in green)
3. Pending requests appear in the **Pending Requests** list with suggested titles
4. Enter the row number, optionally override the title, and click **Approve**
5. Click **Launch in Create tab →** to open the Create tab with the title pre-filled

### Posting a Video

1. After generation completes, go to the **📤 Post** tab (or it auto-navigates if auto-post is on)
2. Review the auto-filled fields: video file, title, description, cover image
3. Edit as needed — click **↺ Regenerate Description** or **↺ Regenerate Cover Image** if desired
4. Set **Privacy** (recommend starting with "private" to verify)
5. Click **Post to YouTube**
6. A YouTube link appears when the upload completes

### Auto-Post

Enable **Auto-post when generation completes** in Config. When a video finishes generating, the app will automatically navigate to the Post tab and begin uploading with the default settings.

---

## Troubleshooting

**"Access blocked: Stephen Spielbot has not completed the Google verification process"** — You need to add your Google account as a test user. Go back to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **OAuth consent screen** → **Test users** → **+ Add Users** → enter your email → Save. Then try connecting again.

**"client_secrets.json not configured"** — Check the path in Config tab → YouTube Integration section. Default: `~/.config/video-generator/client_secrets.json`

**"Not authenticated"** — Click Connect YouTube in the YouTube tab.

**"Google hasn't verified this app" warning** — This is expected. Click **Advanced** → **Go to Stephen Spielbot (unsafe)** to proceed.

**Token expired errors** — The token auto-refreshes. If refresh fails, click Disconnect then Connect YouTube again.

**Upload quota errors** — YouTube API has a daily quota (10,000 units/day). Each video upload costs ~1,600 units. If you hit the limit, wait until the quota resets at midnight Pacific Time.
