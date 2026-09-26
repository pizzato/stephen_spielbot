# X (Twitter) Integration Setup

Stephen Spielbot can post finished videos to X, reply to mentions, and pull
basic analytics. Posting works on the free API tier; **reading** (mentions,
replies, analytics) requires a paid tier — see [Limits](#limits).

## Step 1 — Create an X developer app

1. Go to the [X Developer Portal](https://developer.x.com/) and sign up
   (the free tier is enough to start)
2. Create a **Project** and an **App** inside it
3. In the app's **User authentication settings**, click **Set up**:
   - App permissions: **Read and write**
   - Type of App: **Native App** (public client with PKCE)
   - Callback URI: `http://127.0.0.1:8723/callback` — must match **exactly**;
     the app runs a one-shot local listener on that port during connect
   - Website URL: anything valid (e.g. your channel URL)

## Step 2 — Enter the client credentials

1. Copy the app's **OAuth 2.0 Client ID**
2. In Stephen Spielbot: **Settings → Channels → X** → paste it into **Client ID**
3. **Client Secret** is only needed if you created a *confidential* client —
   leave it blank for a Native App

## Step 3 — Connect an account

1. Save settings, then click **Connect X account** under **Settings → Channels → X**
2. A browser window opens on x.com — authorize the app while logged in to the
   account you want to post from
3. The local callback catches the redirect and stores the token at
   `~/.config/video-generator/x_token_<account>.json`

Repeat for extra accounts, then map each **style** to its X account in the
style's settings. After a reconnect, re-check that mapping — accounts are
matched explicitly, and a stale mapping stops auto-posting.

**Alternative (no browser):** the portal's **Keys and tokens** tab gives OAuth
1.0a user keys (API key/secret + Access token/secret). Entering those instead
also works and uses the classic v1.1 media upload.

## News search

News monitoring can reuse the account connected above. Configure its search account
in **Settings → Channels → X**, alongside the app credentials and publishing
accounts. OAuth 2.0 tokens refresh automatically using the saved refresh token;
OAuth 1.0a accounts use their existing keys. Client ID and Client Secret alone are not
enough: complete **Connect X account** before searching.

Select an account under **News search account** to use it for search. With
**Automatic** selected, a saved standalone **X search bearer token (optional)**
(or `X_BEARER_TOKEN`) is used first; otherwise, the sole connected account is used
automatically. If several accounts are connected,
choose one for search. This choice does not change any style's publishing destination.
The bearer field remains available as an optional alternative; you do not need to
paste an expiring OAuth 2.0 access token there.

The X app needs recent-search endpoint access with either method. Enable **News
monitoring** for each chosen style, then open **AI ideas → News**. See the
[monitor settings](manual/settings.md#news-monitoring) and
[News tab](manual/ideas.md#news-monitor) for topic queries, source review and unattended
creation.

## Limits

- **Free tier**: posting tweets and media works; mention fetching, reply
  threads, and analytics return errors — the app degrades gracefully but the
  Community features need a paid tier.
- **Video length**: the public API cannot post videos longer than 2:20 (even
  for Premium accounts). Longer films are posted as a text tweet linking to
  the YouTube upload instead.
- **Captions**: for OAuth 1.0a accounts the script's SRT is attached as a
  soft (closed-caption) track; OAuth 2.0 accounts skip this — it's
  best-effort either way. A style with
  [burned-in subtitles](manual/settings.md#cover-first-frame) sidesteps the
  limitation entirely: the captions are part of the picture.
