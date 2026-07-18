# Spotted

Spotted turns public YouTube, TikTok, and Instagram videos into timestamped shopping findings. It retrieves a video, extracts representative frames, uses OpenAI vision to identify products, groups repeated appearances, searches the live web for defensible matches, and streams the results into a synchronized video workspace.

Spotted never substitutes canned product results. If a social platform blocks retrieval, the interface offers a real video-upload fallback.

## What runs where

- The `app/` frontend is a Sites-compatible vinext application.
- The `processor/` service runs on the demo laptop because video download and `ffmpeg` cannot run inside the hosted Sites worker.
- Same-origin frontend routes under `/api/jobs` proxy to the processor at `SPOTTED_PROCESSOR_URL`.

## Local setup

Requirements: Node.js 22+, Python 3.11+, `ffmpeg`, `ffprobe`, and `yt-dlp`.

```bash
npm install
npm run setup:processor
cp .env.example .env.local
```

Add the team OpenAI key to `.env.local`:

```text
OPENAI_API_KEY=your-key-here
```

Then start the complete application:

```bash
npm run demo
```

The frontend prints its local URL. The processor health endpoint is available at `http://127.0.0.1:8000/health`.

## Verification

```bash
npm run processor:test
npm run build
npm test
```

Processor tests generate their own short video and do not download social-media content or call OpenAI.

## Hackathon demo

Use one prepared public link from each platform and keep the corresponding MP4 available for upload fallback. For a public judging URL, expose the local frontend through a secure tunnel; the frontend continues to reach the processor through its same-origin proxy.

The current public-link adapters use best-effort retrieval. A production launch should replace that path with creator-owned uploads or approved platform access.
