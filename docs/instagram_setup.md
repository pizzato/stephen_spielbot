# Instagram setup

Stephen Spielbot can publish a finished film as an **Instagram Reel**, directly from
the local video file. Multiple accounts are supported. Instagram publishing is manual:
it does not join the YouTube/X scheduler, style routing, analytics, or comment tools.

This integration uses **Instagram API with Facebook Login**, pinned to Graph API
`v22.0`, with a Facebook Page access token. It does not accept Instagram passwords
or Instagram Login tokens. No public video URL or incoming OAuth callback is needed.

## 1. Prepare the account and Meta app

1. Use an Instagram **Business or Creator** account and link it to a Facebook Page
   you manage. Personal Instagram accounts cannot publish through this API.
2. Create/configure an app in [Meta for Developers](https://developers.facebook.com/)
   for Instagram API with Facebook Login.
3. Authorize your account with `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, and `pages_read_engagement`. Business Manager configurations
   may also require `business_management`.
4. For your own account while developing, configure the app roles/test access in
   Meta's dashboard. Connecting accounts outside those roles requires the applicable
   App Review and Advanced Access; follow the requirements shown for your app.

See Meta's [Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
for the permissions and token flow.

## 2. Obtain a Page access token

Using your app's authorized **User Access Token** in Meta's Graph API Explorer,
request:

```text
GET /me/accounts?fields=id,name,access_token,instagram_business_account
```

Choose the Page linked to the Instagram account you want. Copy **that Page's
`access_token`**, not the User Access Token. Meta documents this response in
[Get Access Tokens of Pages You Manage](https://www.postman.com/meta/instagram/request/0vuw3vk/get-access-tokens-of-pages-you-manage).

Use a long-lived token obtained through Meta's supported token flow for ongoing use.
Token expiry/revocation depends on the app and authorization; Spielbot does not
automatically refresh Facebook tokens. Reconnect with a new Page token when needed.

## 3. Connect in Spielbot

Open **Settings → Channels → Instagram accounts**, paste the **Facebook Page access
token**, and click **Connect Instagram**. Spielbot looks up the linked Instagram
username and verifies access to the publishing-limit endpoint before saving.

Each token is stored separately as
`~/.config/video-generator/instagram_token_<instagram-id>.json`, readable only by
your OS user. Account-list responses never contain tokens. A full Settings backup
includes these credentials. Reconnecting the same account replaces its token;
**Disconnect** removes the local credential, without deleting any Instagram posts
or revoking the app in Meta's dashboard.

## 4. Publish a Reel

1. Open **Publishing → Publish a film** and choose the film and final-cut version.
2. Select **Instagram Reels** under **Publish to**, then choose the account.
3. Review the separate **Instagram caption** (up to 2,200 characters). It starts
   from the film description or title; **Use description as caption** copies your
   current edits. Hashtags are published exactly as typed.
4. Choose whether to **also share the Reel to the Instagram feed**.
5. Click **Publish**, check the destination, then **Confirm**.

Instagram posts are public immediately; the YouTube privacy control applies only to
YouTube. Uploading, processing, and publishing progress appears in the form. A
successful post adds a **View on Instagram** link when Meta returns a permalink and
an Instagram badge to the film in **Films**. The upload result survives a restart.

Selecting Instagram together with YouTube/X uses the same selected final cut.
Instagram's own action does not remove pending YouTube/X queue entries. Instagram
posts are shown on the film and its Publish form; the **Published** queue-history
tab and automatic scheduling remain YouTube/X only.

### Video requirements

- MP4 or MOV, up to 1 GB; between 3 seconds and 15 minutes.
- H.264 or HEVC at 23–60 fps; at most 1920 pixels wide.
- AAC audio when present, no more than 48 kHz and two channels.
- Portrait **9:16** is recommended. Spielbot checks the requirements above before
  uploading but does not resize or transcode the chosen cut. Meta performs final
  validation, including bitrate and encoding constraints; see
  [Meta's Reels specifications](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md).

Instagram does not receive a separate SRT track or custom cover in this integration.
Use [burned-in subtitles](manual/settings.md#cover-first-frame) when you want captions
in the video. Instagram uploads do not set an AI disclosure label or add C2PA
credentials; include an appropriate disclosure in the caption when needed.

## Troubleshooting

- **Unlinked account:** verify the token belongs to the Facebook Page linked to
  your professional Instagram account. A personal-account or User token will not work.
- **Missing permission:** check the app's granted publishing permissions and role/
  review access, then reconnect. Being able to read the profile alone is insufficient.
- **Expired or revoked token:** generate a replacement Page token and reconnect.
- **Processing fails:** check the final cut against Meta's Reel specifications.
  Processing is checked for up to ten minutes; a timeout does not publish anything.
- **Already published or uncertain:** Spielbot blocks another post of the same film
  to the same account. If the publish response was lost or the server stopped while
  publishing, check the Instagram profile before taking any further action.

Publication records live in each film's `instagram_publish_<instagram-id>.json`.
An interrupted upload before the publish request can be retried. An uncertain
publish result is deliberately not retried automatically. If you have independently
confirmed no Reel was published, stop any active upload and back up/remove that
film/account's record to permit a new attempt. Never remove a successful or uncertain
record merely to clear an error without checking the account first.
