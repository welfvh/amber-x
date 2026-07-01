# amber-x → Content Studio — Brief & Roadmap

**Source:** voice memo `260701_0922.mp3` (2026-07-01 09:22, Sony recorder), dictated by Welf to Claude Code.
Transcript: `~/dev/cc-transcribe/sessions/2026-07-01_sd_latest/260701_0922.deepgram.txt`.
This doc is the source-of-truth spec for expanding the existing `amber-x` (x-vibepoastry) app into a full X content-creation studio. Vision text is preserved verbatim below; do not edit the verbatim block.

---

## Vision (verbatim, lightly de-mistranscribed)

> Okay, Claude. Here's exactly what I need. First, dedicate a sub-agent to figuring out what's going on with the X API access — I'm pretty sure there are still credits on it, and recent conversations touching the X API surely contain leads to restore it without me having to do anything.
>
> For the app, the vision is this. First, I need a **feed** — basically a Twitter feed, the closest exact clone you can make. Unblock Twitter, open it in Chrome, take the exact elements, make the closest possible clone in design — turn that into a **design system with reusable components** throughout the app.
>
> Second, a feed that is primarily **my own tweets** — my best tweets, sorted by how well they worked. And a **"quote tweet on steroids"** — a machine for me to come up with new ideas, write new posts, draft them, refine them, and post them. Connected to the **Claude API**. I should not see any AI suggestions other than what I write myself, until I've written a draft.
>
> A **second feed pulled from my Are.na**. And a feed of **relevant stuff to react to** around e-ink, paper-like display, Amber computing — accounts I've engaged with and am likely to engage with. Real curation work — maybe ongoing scripts that identify tweets around certain **keywords** and auto-populate a custom feed.
>
> Separate it into tabs: **all**, **my viral tweets**, **my recent tweets**, and **replies I still need to reply to** (what would otherwise live in notifications, in its own feed). Cast the net wide, and give me a way in the app to **edit the keywords / selection algorithm**.
>
> Then the **draft editor** — copy the Twitter interface, desktop + tablet (Daylight 600×800 / 800×600) + mobile. Get artifacts for the different Twitter views. A perfect clone of the design. Attach pictures, make threads — a whole Twitter drafting suite.
>
> In the drafting thing, a button: **"chat about this with Claude."** MVP: a pre-populated link that starts a new Claude chat with context attached — the tweet link, verbatim tweet content, any thread tweets, like/retweet counts, plus my draft, plus a "how can this be made better?" prompt. Advanced: a tweet-length reflection + 3 edited variations to swipe through and select/edit.
>
> The idea: an app where I can distraction-free have the perfect seeding ground for creating the most authentic, creative, and viral content I possibly can.
>
> Also **stats** — analytics view: how many views, average engagement across last 10/20/50/100 tweets, how many tweets in last 7/30/90 days, replies vs. normal tweets.
>
> And an **AI-suggested-drafts** tab. Get to the architecture of the perfect tweet, get to the core ideas of my work, combine them. Use the Obsidian second-brain, use the Are.na API for inspiration. Have a sub-agent research good tweet architecture, another research the best ideas for my work, another combine those into ~500 drafts — or prompts, or questions (what are unanswered questions from my audience?). Learn what drives engagement — e.g. the **flicker / paper-like-screen health** topic drives engagement, so make more content about the phenomenology of e-ink screens.

---

## Structured spec

**0. X API access (step zero, sub-agent).** Restore authenticated X API access (read + write) via the VPS. See recovery findings appended by the recovery sub-agent.

**1. Twitter-clone design system.** Reusable components matching X's look, used across the app. Responsive: desktop, Daylight tablet (600×800 & 800×600), mobile.

**2. Multi-feed system (tabs):**
- All
- My tweets — sorted by performance (best/viral first)
- My recent tweets
- My viral tweets
- Replies-to-do (owed replies, from notifications)
- Are.na feed (from Welf's Are.na)
- Topical/curated — keyword-matched around e-ink / paper-display / Amber computing / engaged accounts; ongoing auto-populate
- AI-suggested drafts (see #6)

**3. Keyword/algorithm editor.** In-app control to edit the keywords + selection logic driving the curated feed.

**4. Drafting suite.** Twitter-clone editor: images, threads. Rule: no AI suggestions except what Welf writes himself, until a draft exists.

**5. "Chat about this with Claude" bridge.**
- MVP: pre-populated deep-link → new Claude chat with context (tweet link, verbatim content, thread, like/RT counts, draft, "how to improve?" prompt).
- Advanced: inline tweet-length reflection + 3 variations to swipe/select/edit.

**6. AI draft-generation pipeline (multi-sub-agent).** Agent A: perfect-tweet architecture. Agent B: core ideas of Welf's work (source: Obsidian vault + Are.na API). Agent C: combine → ~500 drafts / prompts / audience-questions. Feedback loop: learn from engagement (flicker/e-ink topic is a proven driver).

**7. Analytics view.** Views; avg engagement over last 10/20/50/100 tweets; tweet counts over 7/30/90 days; replies vs. normal ratio.

---

## What already exists (foundation)

- `server.js` (:3131) — draft CRUD, media, posting (dedup + status locks), scheduling, queue, tweet delete; feed/stats **proxy endpoints** (`/tweets`, `/activity`, `/stats`, `/status`) to the VPS; assets; jam→`/cc-x-jam` bridge.
- `index.html` — Twitter-styled UI, tabs: Compose / Feed / Schedule / Assets; theme, char-ring, threads, image attach.
- `vps/main.py` (Hetzner `162.55.60.42:8142`) — FastAPI + tweepy; posts/schedules/feeds/stats. **WIP (uncommitted): OAuth 2.0 PKCE for bookmarks.**
- `mcp-server/` — MCP facade (Poke).
- SQLite: `drafts`, `media`, `assets`, `tweets_cache`.
- Content strategy in sibling `~/dev/x-business/`: `CONTENT_PRINCIPLES.md`, `DONT_DO.md`, `PHRASE_BANK.md`, 1,253 categorized bookmarks.

## Gap → phased roadmap

- **Phase 0 — X API access** *(gated on recovery sub-agent)*: confirm/restore read+write; verify `/tweets`, `/activity`, `/stats` return live data.
- **Phase 1 — Multi-feed UI + Are.na feed** *(X-independent, build now)*: refactor single Feed tab → feed system with sub-tabs; add Are.na feed source (Are.na v2 API); scaffold feed component in the design system.
- **Phase 2 — X-read feeds**: wire My/Recent/Viral/Replies to VPS (needs Phase 0); performance sorting; cache in `tweets_cache`.
- **Phase 3 — Topical curation**: keyword store + editor UI; ongoing populate job (local script or VPS cron); dedup.
- **Phase 4 — AI draft pipeline**: multi-sub-agent generator (Obsidian + Are.na → drafts/prompts/questions) → AI-suggested-drafts tab.
- **Phase 5 — Drafting suite + Claude bridge polish**: "chat with Claude" deep-link MVP then inline variations; media/threads parity with X.
- **Phase 6 — Analytics view**: engagement windows, cadence, replies ratio.
- **Phase 7 — Responsive**: Daylight 600×800 / 800×600 + mobile breakpoints.

**Invariants:** posts to real `@_welf` — dedup/locks stay. Build on branch, never disturb the running :3131 server or the uncommitted VPS OAuth WIP. No AI suggestions surface before Welf writes his own draft (brief rule).
