# Spotted

Spotted turns public YouTube, TikTok, and Instagram videos into timestamped shopping findings. It retrieves a video, samples every five seconds, uses OpenAI vision to identify and track products, preserves timestamps from near-identical scenes without analyzing them twice, and performs image-informed web search plus multi-image verification before showing a shopping link.

Spotted never substitutes canned product results. If a social platform blocks retrieval, the interface offers a real video-upload fallback.

## What runs where

- The `app/` frontend is a Sites-compatible vinext application.
- The `processor/` service runs on the demo laptop because video download and `ffmpeg` cannot run inside the hosted Sites worker.
- Same-origin frontend routes under `/api/jobs` proxy to the processor at `SPOTTED_PROCESSOR_URL`.

## Local setup

Requirements: Node.js 22+, Python 3.11+, `ffmpeg`, and `ffprobe`. The processor setup installs a compatible `yt-dlp` release and its YouTube challenge solver inside the project environment.

```bash
npm install
npm run setup:processor
cp .env.example .env.local
```

Add the team OpenAI key to `.env.local`:

```text
OPENAI_API_KEY=your-key-here
```

The default model split keeps routine frame scanning on `gpt-5.6-luna` and
uses `gpt-5.6-terra` only for one image-first search and verification pass per
unique tracked object. Both values can be overridden in `.env.local`.

For true reverse-image candidate discovery, add a SerpApi key and configure the
short-lived crop relay deployed with the Sites frontend:

```text
SERPAPI_API_KEY=your-serpapi-key
SPOTTED_IMAGE_RELAY_URL=https://your-spotted-site.example/api/lens-crops
SPOTTED_RELAY_TOKEN=use-the-same-secret-configured-in-sites
```

Spotted sends one tight crop per unique product to the relay, runs Google Lens
against its temporary HTTPS URL, and deletes the crop as soon as Lens returns.
Lens only proposes candidates; OpenAI compares the video crops with retailer images,
rejects visual contradictions, and decides whether a result is exact or similar.
Without these optional values, the OpenAI image-informed web-search path remains
fully functional.

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
