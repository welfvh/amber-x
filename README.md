# amber-x

A local X/Twitter composer with threads, images, and drafts. Runs entirely on your machine — your API keys stay in a `.env` file and never leave.

![amber-x composer](https://i.imgur.com/placeholder.png)

## Features

- **Compose** tweets with X's exact look and feel (dark/light mode, synced to system)
- **Threads** — chain multiple tweets with the "Add to thread" button
- **Images** — attach up to 4 images per tweet, shown as inline previews
- **Drafts** — save and reload drafts locally (browser localStorage)
- **Post for real** — optional API send via your own X developer credentials
- **Keyboard shortcuts** — `Cmd+Enter` to post, `Cmd+S` to save draft

## Setup

**1. Install dependencies**

```bash
npm install
```

**2. Start the server**

```bash
npm start
```

**3. Open the app**

```
open index.html
```

**4. Connect your X account**

Toggle "Post for real via X API" in the app — if no credentials are configured, the setup panel opens automatically.

You'll need an X Developer app:
1. Go to [developer.x.com/en/portal/apps/new](https://developer.x.com/en/portal/apps/new)
2. Create an app with **Read and Write** permissions
3. Enable **OAuth 1.0a** (User authentication settings)
4. Generate **Access Token and Secret** (make sure they have Read+Write)
5. Copy all four keys into the app's setup panel

Credentials are saved to a local `.env` file with `600` permissions (owner-read only).

## Without posting

The app works fully offline for drafting — no server needed, no API keys needed. Just open `index.html` and start writing. Drafts persist in localStorage.

## Architecture

```
index.html      UI — pure HTML/CSS/JS, no build step
server.js       Local API server (port 3131)
  POST /setup   Save + verify credentials → writes .env
  GET  /status  Check auth status
  POST /post    Post tweet or thread with optional images
.env            Your credentials (gitignored, never committed)
```

## License

MIT
