"use client";

import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";

type YouTubePlayer = {
  destroy: () => void;
  getCurrentTime: () => number;
  mute: () => void;
  playVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void;
};

type YouTubeAPI = {
  Player: new (
    element: HTMLElement,
    options: {
      videoId: string;
      width: string;
      height: string;
      host?: string;
      playerVars: Record<string, string | number>;
      events: {
        onReady: () => void;
        onStateChange: (event: { data: number }) => void;
      };
    },
  ) => YouTubePlayer;
};

declare global {
  interface Window {
    YT?: YouTubeAPI;
    onYouTubeIframeAPIReady?: () => void;
  }
}

let youtubeApiPromise: Promise<YouTubeAPI> | null = null;

function loadYouTubeAPI(): Promise<YouTubeAPI> {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (youtubeApiPromise) return youtubeApiPromise;
  youtubeApiPromise = new Promise((resolve, reject) => {
    const priorReady = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      priorReady?.();
      if (window.YT?.Player) resolve(window.YT);
      else reject(new Error("YouTube playback could not initialize."));
    };
    let script = document.querySelector<HTMLScriptElement>('script[src="https://www.youtube.com/iframe_api"]');
    if (!script) {
      script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.async = true;
      document.head.appendChild(script);
    }
    script.addEventListener("error", () => reject(new Error("YouTube playback could not load.")), { once: true });
  });
  return youtubeApiPromise;
}

type EventType =
  | "retrieving_video"
  | "extracting_frames"
  | "analyzing_frame"
  | "candidate_found"
  | "merging_duplicates"
  | "searching_retailers"
  | "product_ready"
  | "retrieval_blocked"
  | "completed"
  | "failed";

type Appearance = {
  startSec: number;
  endSec?: number;
  thumbnailUrl?: string;
  boundingBox?: { x: number; y: number; width: number; height: number };
  evidence: string;
};

type ProductFinding = {
  id: string;
  name: string;
  category: string;
  matchKind: "exact" | "similar" | "possible";
  confidence: number;
  detectionConfidence?: number;
  matchConfidence?: number;
  brand?: string;
  model?: string;
  retailerName?: string;
  productUrl?: string;
  imageUrl?: string;
  price?: string;
  appearances: Appearance[];
};

type JobResponse = {
  jobId?: string;
  platform?: string;
  status?: string;
  mediaUrl?: string | null;
  findings?: ProductFinding[];
  metadata?: { width?: number; height?: number; durationSec?: number };
  error?: { message?: string } | string;
  message?: string;
  detail?: string;
};

function youtubeVideoId(value: string) {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    let candidate = "";
    if (host === "youtu.be") candidate = parsed.pathname.split("/").filter(Boolean)[0] || "";
    if (host === "youtube.com" || host === "m.youtube.com" || host === "music.youtube.com") {
      candidate = parsed.searchParams.get("v") || "";
      if (!candidate) {
        const parts = parsed.pathname.split("/").filter(Boolean);
        if (["shorts", "embed", "live"].includes(parts[0])) candidate = parts[1] || "";
      }
    }
    return /^[a-zA-Z0-9_-]{6,}$/.test(candidate) ? candidate : null;
  } catch {
    return null;
  }
}

const eventOrder: EventType[] = [
  "retrieving_video",
  "extracting_frames",
  "analyzing_frame",
  "candidate_found",
  "merging_duplicates",
  "searching_retailers",
  "product_ready",
  "completed",
];

const eventCopy: Record<EventType, string> = {
  retrieving_video: "Retrieving video",
  extracting_frames: "Mapping scenes and timestamps",
  analyzing_frame: "Looking for recognizable products",
  candidate_found: "Product candidate spotted",
  merging_duplicates: "Comparing repeat appearances",
  searching_retailers: "Searching product images and retailers",
  product_ready: "Shopping match ready",
  retrieval_blocked: "Upload needed",
  completed: "Analysis complete",
  failed: "Analysis stopped",
};

function formatTime(seconds: number) {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

type AppearanceGroup = {
  first: Appearance;
  lastSec: number;
  count: number;
};

function groupAppearances(
  appearances: Appearance[],
  maximumGapSec = 2.25,
): AppearanceGroup[] {
  const ordered = [...appearances].sort((left, right) => left.startSec - right.startSec);
  const groups: AppearanceGroup[] = [];
  for (const appearance of ordered) {
    const endSec = Math.max(appearance.startSec, appearance.endSec ?? appearance.startSec);
    const current = groups.at(-1);
    if (current && appearance.startSec - current.lastSec <= maximumGapSec) {
      current.lastSec = Math.max(current.lastSec, endSec);
      current.count += 1;
    } else {
      groups.push({ first: appearance, lastSec: endSec, count: 1 });
    }
  }
  return groups;
}

// One-second frame sampling means the closest observation should be no more
// than about half a second from playback. A small allowance keeps overlays
// stable across YouTube's 500ms player updates without carrying a tag into a
// visibly different moment.
const APPEARANCE_MATCH_WINDOW_SEC = 0.75;

function appearanceGroupLabel(group: AppearanceGroup) {
  const start = formatTime(group.first.startSec);
  return group.lastSec > group.first.startSec + 1
    ? `${start}–${formatTime(group.lastSec)}`
    : start;
}

function productPercent(confidence: number) {
  const normalized = confidence <= 1 ? confidence * 100 : confidence;
  return Math.round(Math.min(100, Math.max(0, normalized)));
}

function isFinding(value: unknown): value is ProductFinding {
  if (!value || typeof value !== "object") return false;
  const product = value as Partial<ProductFinding>;
  return typeof product.id === "string" && typeof product.name === "string" && Array.isArray(product.appearances);
}

function responseMessage(data: JobResponse, fallback: string) {
  if (data.detail) return data.detail;
  if (data.message) return data.message;
  if (typeof data.error === "string") return data.error;
  if (data.error?.message) return data.error.message;
  return fallback;
}

async function responseJson(response: Response): Promise<JobResponse> {
  try {
    return (await response.json()) as JobResponse;
  } catch {
    return {};
  }
}

function withSurfaceTransition(update: () => void): Promise<void> {
  update();
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return reduceMotion
    ? Promise.resolve()
    : new Promise((resolve) => window.setTimeout(resolve, 480));
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [focus, setFocus] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [platform, setPlatform] = useState("video");
  const [status, setStatus] = useState<"idle" | "starting" | "running" | "complete" | "blocked" | "error">("idle");
  const [eventType, setEventType] = useState<EventType>("retrieving_video");
  const [eventHistory, setEventHistory] = useState<EventType[]>([]);
  const [products, setProducts] = useState<ProductFinding[]>([]);
  const [activeId, setActiveId] = useState("");
  const [activeAppearance, setActiveAppearance] = useState<Appearance | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [error, setError] = useState("");
  const [mobileResults, setMobileResults] = useState(false);
  const [videoReady, setVideoReady] = useState(false);
  const [mediaAvailable, setMediaAvailable] = useState(false);
  const [videoDimensions, setVideoDimensions] = useState({ width: 16, height: 9 });
  const [videoRect, setVideoRect] = useState({ left: 0, top: 0, width: 0, height: 0 });
  const streamRef = useRef<EventSource | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const videoStageRef = useRef<HTMLDivElement>(null);
  const youtubeHostRef = useRef<HTMLDivElement>(null);
  const youtubePlayerRef = useRef<YouTubePlayer | null>(null);
  const pendingSeekRef = useRef<number | null>(null);
  const surfaceHandoffRef = useRef<Promise<void>>(Promise.resolve());
  const fileRef = useRef<HTMLInputElement>(null);

  const pastedYoutubeId = youtubeVideoId(url);
  const youtubeId = youtubeVideoId(sourceUrl);
  const mainProducts = products.filter((product) => product.matchKind !== "possible" && Boolean(product.productUrl));
  const possibleProducts = products.filter((product) => product.matchKind === "possible" && Boolean(product.productUrl));
  const linkedProducts = [...mainProducts, ...possibleProducts];
  const taggedProducts = products.filter((product) => product.appearances.length > 0);
  const groupedMoments = taggedProducts.flatMap((product) =>
    groupAppearances(product.appearances).map((group) => ({ product, group })),
  );
  const activeProduct = taggedProducts.find((product) => product.id === activeId) ?? linkedProducts[0] ?? taggedProducts[0];
  const progressIndex = eventOrder.indexOf(eventType);
  const progress = status === "complete" ? 100 : Math.max(6, ((Math.max(0, progressIndex) + 1) / eventOrder.length) * 100);

  useEffect(() => () => streamRef.current?.close(), []);

  useEffect(() => {
    if (!pastedYoutubeId) return;
    // Warm the player API while the user is still in the composer so the
    // submitted video can replace the search surface immediately.
    void loadYouTubeAPI().catch(() => undefined);
  }, [pastedYoutubeId]);

  useEffect(() => {
    const stage = videoStageRef.current;
    if (!stage) return;
    const updateRect = () => {
      const stageWidth = stage.clientWidth;
      const stageHeight = stage.clientHeight;
      if (!stageWidth || !stageHeight) return;
      const sourceRatio = videoDimensions.width / videoDimensions.height || 16 / 9;
      const stageRatio = stageWidth / stageHeight;
      if (stageRatio > sourceRatio) {
        const width = stageHeight * sourceRatio;
        setVideoRect({ left: (stageWidth - width) / 2, top: 0, width, height: stageHeight });
      } else {
        const height = stageWidth / sourceRatio;
        setVideoRect({ left: 0, top: (stageHeight - height) / 2, width: stageWidth, height });
      }
    };
    updateRect();
    const observer = new ResizeObserver(updateRect);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [status, videoDimensions.width, videoDimensions.height]);

  useEffect(() => {
    if (!youtubeId) return;
    let cancelled = false;
    let poll: number | undefined;
    const host = youtubeHostRef.current;
    void loadYouTubeAPI().then((api) => {
      if (cancelled || !host) return;
      const mount = document.createElement("div");
      host.replaceChildren(mount);
      const player = new api.Player(mount, {
        videoId: youtubeId,
        width: "100%",
        height: "100%",
        host: "https://www.youtube-nocookie.com",
        playerVars: {
          autoplay: 1,
          playsinline: 1,
          rel: 0,
          origin: window.location.origin,
        },
        events: {
          onReady: () => {
            if (cancelled) return;
            youtubePlayerRef.current = player;
            setVideoReady(true);
            // Muted autoplay is permitted by modern browsers and gives the user
            // immediate playback while the backend analyzes the same source.
            player.mute();
            player.playVideo();
            if (pendingSeekRef.current !== null) {
              player.seekTo(pendingSeekRef.current, true);
              player.playVideo();
              pendingSeekRef.current = null;
            }
          },
          onStateChange: () => undefined,
        },
      });
      youtubePlayerRef.current = player;
      poll = window.setInterval(() => {
        try {
          const time = player.getCurrentTime();
          if (!Number.isFinite(time)) return;
          setCurrentTime(time);
          setActiveAppearance((current) => current && Math.abs(time - current.startSec) > APPEARANCE_MATCH_WINDOW_SEC ? null : current);
        } catch { /* the player may be between states */ }
      }, 500);
    }).catch(() => {
      if (!cancelled) setVideoReady(false);
    });
    return () => {
      cancelled = true;
      if (poll !== undefined) window.clearInterval(poll);
      try { youtubePlayerRef.current?.destroy(); } catch { /* already removed */ }
      youtubePlayerRef.current = null;
      host?.replaceChildren();
    };
  }, [youtubeId]);

  function recordEvent(type: EventType) {
    setEventType(type);
    setEventHistory((history) => history.includes(type) ? history : [...history, type]);
  }

  function addFinding(product: ProductFinding) {
    setProducts((current) => {
      const matchIndex = current.findIndex((item) => item.id === product.id);
      if (matchIndex < 0) return [...current, product];
      const next = [...current];
      next[matchIndex] = product;
      return next;
    });
    setActiveId((current) => current || (product.productUrl ? product.id : ""));
  }

  async function fetchJob(id: string) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { cache: "no-store" });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "Could not load the completed findings."));
      const findings = Array.isArray(data.findings) ? data.findings.filter(isFinding) : [];
      if (data.metadata?.width && data.metadata?.height) {
        setVideoDimensions({ width: data.metadata.width, height: data.metadata.height });
      }
      setMediaAvailable(Boolean(data.mediaUrl));
      setProducts(findings);
      setActiveId((current) => current || findings.find((item) => item.productUrl)?.id || "");
      setStatus(data.status === "failed" ? "error" : "complete");
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not load findings.");
    }
  }

  function handleServerEvent(type: EventType, raw: string, id: string) {
    let payload: Record<string, unknown> = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch { /* named event can have no JSON body */ }
    const eventMessage = typeof payload.message === "string" ? payload.message : "";
    recordEvent(type);
    if (type === "extracting_frames") {
      setMediaAvailable(true);
      const width = Number(payload.width);
      const height = Number(payload.height);
      if (width > 0 && height > 0) setVideoDimensions({ width, height });
    }
    if (type === "product_ready" && isFinding(payload)) addFinding(payload);
    if (type === "retrieval_blocked") {
      streamRef.current?.close();
      void surfaceHandoffRef.current.then(() => withSurfaceTransition(() => {
        setStatus("blocked");
        setError(eventMessage || "This platform did not provide the video. Upload the file to continue.");
      }));
    } else if (type === "failed") {
      streamRef.current?.close();
      void surfaceHandoffRef.current.then(() => withSurfaceTransition(() => {
        setStatus("error");
        setError(eventMessage || "The processor could not finish this video.");
      }));
    } else if (type === "completed") {
      setStatus("complete");
      streamRef.current?.close();
      void fetchJob(id);
    } else {
      setStatus("running");
    }
  }

  function connectEvents(id: string) {
    streamRef.current?.close();
    const stream = new EventSource(`/api/jobs/${encodeURIComponent(id)}/events`);
    streamRef.current = stream;
    (Object.keys(eventCopy) as EventType[]).forEach((type) => {
      stream.addEventListener(type, (message) => handleServerEvent(type, (message as MessageEvent).data, id));
    });
    stream.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data) as { type?: EventType };
        if (payload.type && payload.type in eventCopy) handleServerEvent(payload.type, message.data, id);
      } catch { /* named events are handled above */ }
    };
  }

  function startJob(data: JobResponse) {
    if (!data.jobId) throw new Error("The processor did not return a job ID.");
    setJobId(data.jobId);
    setPlatform(data.platform || "video");
    setMediaAvailable(Boolean(data.mediaUrl));
    setStatus("running");
    connectEvents(data.jobId);
  }

  async function submitUrl(event: FormEvent) {
    event.preventDefault();
    const videoUrl = url.trim();
    if (!videoUrl) return;
    try { new URL(videoUrl); } catch { setError("Paste a complete YouTube, TikTok, or Instagram URL."); setStatus("error"); return; }
    const handoff = withSurfaceTransition(() => {
      setStatus("starting");
      setEventType("retrieving_video");
      setEventHistory(["retrieving_video"]);
      setProducts([]);
      setActiveId("");
      setActiveAppearance(null);
      setError("");
      setMobileResults(false);
      setVideoReady(false);
      setMediaAvailable(false);
      setVideoDimensions({ width: 16, height: 9 });
      pendingSeekRef.current = null;
      setSourceUrl(videoUrl);
    });
    surfaceHandoffRef.current = handoff;
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: videoUrl, focus: focus.trim() || undefined }),
        // Job creation should return immediately. If the local processor has
        // stopped, fail visibly instead of leaving the progress UI at its first
        // (13%) step forever.
        signal: AbortSignal.timeout(20_000),
      });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "The video processor is unavailable."));
      startJob(data);
    } catch (reason) {
      await handoff;
      await withSurfaceTransition(() => {
        setStatus("error");
        setError(
          reason instanceof DOMException && reason.name === "TimeoutError"
            ? "The local video processor did not respond. Restart Spotted and try again."
            : reason instanceof Error
              ? reason.message
              : "Could not start this video.",
        );
      });
    }
  }

  async function uploadVideo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    if (focus.trim()) form.append("focus", focus.trim());
    const handoff = withSurfaceTransition(() => {
      setStatus("starting");
      setEventType("retrieving_video");
      setEventHistory(["retrieving_video"]);
      setProducts([]);
      setActiveId("");
      setActiveAppearance(null);
      setError("");
      setPlatform("upload");
      setSourceUrl("");
      setVideoReady(false);
      setMediaAvailable(false);
      setVideoDimensions({ width: 16, height: 9 });
      pendingSeekRef.current = null;
    });
    surfaceHandoffRef.current = handoff;
    try {
      const response = await fetch("/api/jobs/upload", { method: "POST", body: form });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "The upload could not be started."));
      startJob(data);
    } catch (reason) {
      await handoff;
      await withSurfaceTransition(() => {
        setStatus("error");
        setError(reason instanceof Error ? reason.message : "Could not upload this video.");
      });
    } finally {
      event.target.value = "";
    }
  }

  async function newSearch() {
    streamRef.current?.close();
    const id = jobId;
    withSurfaceTransition(() => {
      setJobId(null);
      setStatus("idle");
      setProducts([]);
      setEventHistory([]);
      setActiveId("");
      setActiveAppearance(null);
      setCurrentTime(0);
      setError("");
      setVideoReady(false);
      setMediaAvailable(false);
      setVideoDimensions({ width: 16, height: 9 });
      pendingSeekRef.current = null;
      setSourceUrl("");
    });
    if (id) {
      try { await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE", keepalive: true }); } catch { /* jobs also expire */ }
    }
  }

  function seek(product: ProductFinding, appearance: Appearance) {
    setActiveId(product.id);
    setActiveAppearance(appearance);
    setCurrentTime(appearance.startSec);
    setMobileResults(false);
    if (youtubeId) {
      const player = youtubePlayerRef.current;
      if (player) {
        player.seekTo(appearance.startSec, true);
        player.playVideo();
      } else {
        pendingSeekRef.current = appearance.startSec;
      }
      return;
    }
    if (videoRef.current) {
      const duration = videoRef.current.duration;
      const target = Number.isFinite(duration)
        ? Math.min(appearance.startSec, Math.max(0, duration - 0.05))
        : appearance.startSec;
      videoRef.current.currentTime = target;
      void videoRef.current.play().catch(() => undefined);
    }
  }

  const workspaceVisible = status === "starting" || status === "running" || status === "complete";
  const nearbyTag = taggedProducts
    .flatMap((product) => product.appearances.map((appearance) => ({ product, appearance })))
    .filter(({ appearance }) => Math.abs(currentTime - appearance.startSec) <= APPEARANCE_MATCH_WINDOW_SEC)
    .sort((left, right) => Math.abs(currentTime - left.appearance.startSec) - Math.abs(currentTime - right.appearance.startSec))[0];
  const selectedAppearanceIsCurrent = Boolean(
    activeAppearance && Math.abs(currentTime - activeAppearance.startSec) <= APPEARANCE_MATCH_WINDOW_SEC,
  );
  const currentAppearance = selectedAppearanceIsCurrent
    ? activeAppearance
    : nearbyTag?.appearance;
  const currentTagProduct = selectedAppearanceIsCurrent && activeProduct ? activeProduct : nearbyTag?.product;
  const currentBox = currentAppearance?.boundingBox;

  return (
    <main className={`app-shell ${workspaceVisible ? "has-workspace" : "is-landing"}`}>
      <header className="topbar">
        <button className="logo logo-button hero-logo" type="button" onClick={() => void newSearch()} aria-label="Spotted home"><span>Spotted</span><i aria-hidden="true" /></button>
        {workspaceVisible && <button className="header-action" onClick={() => void newSearch()}>New search <span>＋</span></button>}
      </header>

      <div className="experience-stage">
        <section className="intro" id="top">
          <h1>Spot it. Buy it.</h1>
        </section>

        <form className="composer" onSubmit={submitUrl}>
        <div className="composer-fields">
          <div className="prompt-field prompt-field-url">
            <button className="prompt-plus" type="button" onClick={() => fileRef.current?.click()} aria-label="Upload a video">＋</button>
            <div className="prompt-copy">
              <label htmlFor="composer">Video link</label>
              <input id="composer" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="Paste a YouTube, TikTok, or Instagram link" inputMode="url" disabled={status === "starting"} />
            </div>
          </div>
          <span className="composer-divider" aria-hidden="true" />
          <div className="prompt-field prompt-field-focus">
            <span className="prompt-spark" aria-hidden="true">✦</span>
            <div className="prompt-copy">
              <label htmlFor="search-focus">What should Spotted look for?</label>
              <input
                id="search-focus"
                value={focus}
                onChange={(event) => setFocus(event.target.value)}
                placeholder="Everything — or watches, shoes, tools…"
                disabled={status === "starting"}
                maxLength={500}
              />
            </div>
            <span className="prompt-mode">{focus.trim() ? "Focused" : "Everything"}</span>
          </div>
          <button className="composer-submit" type="submit" disabled={!url.trim() || status === "starting"} aria-label={status === "starting" ? "Starting product scan" : "Find products"}>
            <span className="composer-submit-label">{status === "starting" ? "Starting" : "Find products"}</span>
            <span aria-hidden="true">→</span>
          </button>
        </div>
        <div className="composer-meta">
          <div className="platforms"><span>YouTube</span><i /><span>TikTok</span><i /><span>Instagram</span></div>
          <div className="composer-links"><button type="button" onClick={() => fileRef.current?.click()}>Upload a video instead</button></div>
        </div>
        <input ref={fileRef} className="file-input" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" onChange={uploadVideo} />
        </form>

        {(status === "error" || status === "blocked") && (
          <div className="notice" role="alert">
            <div><strong>{status === "blocked" ? "The platform blocked this link" : "The scan couldn’t start"}</strong><p>{error}</p></div>
            <button onClick={() => fileRef.current?.click()}>Upload video <span>↑</span></button>
          </div>
        )}

        {workspaceVisible && (
          <section className={`workspace ${status === "running" || status === "starting" ? "is-running" : ""}`} aria-label="Video findings workspace">
          <div className="video-column">
            <div className="panel-heading">
              <div><span className="step-number">01</span><div><strong>Video</strong><small>{jobId ? `${platform} · Job ${jobId.slice(0, 8)}` : "Creating secure session"}</small></div></div>
              <span className="source-badge">{focus.trim() ? "Focused analysis" : "Live analysis"}</span>
            </div>
            <div ref={videoStageRef} className={`video-stage real-video-stage ${videoReady ? "is-playable" : "is-loading"} ${youtubeId ? "has-youtube" : ""}`}>
              {youtubeId ? (
                <div
                  ref={youtubeHostRef}
                  className="youtube-player"
                  aria-label="YouTube video player"
                  style={{ backgroundImage: `url(https://i.ytimg.com/vi/${youtubeId}/hqdefault.jpg)` }}
                />
              ) : jobId && mediaAvailable ? (
                <video ref={videoRef} controls playsInline preload="metadata" src={`/api/jobs/${encodeURIComponent(jobId)}/media`} onCanPlay={(event) => { setVideoReady(true); if (event.currentTarget.videoWidth && event.currentTarget.videoHeight) setVideoDimensions({ width: event.currentTarget.videoWidth, height: event.currentTarget.videoHeight }); }} onTimeUpdate={(event) => { setCurrentTime(event.currentTarget.currentTime); if (activeAppearance && Math.abs(event.currentTarget.currentTime - activeAppearance.startSec) > APPEARANCE_MATCH_WINDOW_SEC) setActiveAppearance(null); }} />
              ) : null}
              {!youtubeId && <div className="video-loading" aria-hidden={videoReady}>
                <div className="scan-orbit"><span>AI</span><i /><i /><i /></div>
                <strong>Preparing playback</strong>
                <small>The first frames will appear here.</small>
              </div>}
              {youtubeId && !videoReady && <div className="youtube-loading-badge"><i /> Opening playback</div>}
              {currentBox && videoReady && currentTagProduct && <div className="detection-layer" style={videoRect}><div className="detection-box" style={{ left: `${currentBox.x * 100}%`, top: `${currentBox.y * 100}%`, width: `${currentBox.width * 100}%`, height: `${currentBox.height * 100}%` }}><span>{currentTagProduct.name} · {productPercent(currentTagProduct.matchConfidence ?? currentTagProduct.detectionConfidence ?? currentTagProduct.confidence)}%</span></div></div>}
            </div>
            <div className="moments">
              <div><span className="moment-count">{taggedProducts.length.toString().padStart(2, "0")}</span><span>Tagged products<br />repeat sightings grouped</span></div>
              <div className="moment-list">{groupedMoments.map(({ product, group }) => <button key={`${product.id}-${group.first.startSec}`} className={activeId === product.id && currentTime >= group.first.startSec - 0.75 && currentTime <= group.lastSec + 0.75 ? "active" : ""} title={group.count > 1 ? `${group.count} nearby sampled appearances grouped` : group.first.evidence} onClick={() => seek(product, group.first)}><span>{appearanceGroupLabel(group)}</span>{product.brand || product.name}</button>)}</div>
            </div>
          </div>

          <div className={`results-column ${mobileResults ? "mobile-open" : ""}`}>
            <div className="panel-heading">
              <div><span className="step-number">02</span><div><strong>Findings</strong><small>{status === "complete" ? `${mainProducts.length} verified · ${possibleProducts.length} possible` : eventCopy[eventType]}</small></div></div>
              {status === "complete" && <span className="complete-badge"><i />Complete</span>}
            </div>
            {status !== "complete" && linkedProducts.length === 0 ? (
              <div className="processing" aria-live="polite">
                <div className="scan-orbit"><span>{Math.round(progress)}%</span><i /><i /><i /></div>
                <h2>{eventCopy[eventType]}</h2>
                <p>Verified matches will appear here as the model recognizes, groups, and checks them.</p>
                <div className="processing-bar"><span style={{ width: `${progress}%` }} /></div>
                <ol>{eventOrder.slice(0, -1).map((item, index) => <li className={eventHistory.includes(item) ? "done" : item === eventType ? "active" : ""} key={item}><i>{eventHistory.includes(item) ? "✓" : index + 1}</i>{eventCopy[item]}</li>)}</ol>
              </div>
            ) : (
              <div className="findings-scroll">
                <div className="results-summary"><div><strong>{mainProducts.length.toString().padStart(2, "0")}</strong><span>Verified shopping<br />matches</span></div><p><i />Exact match <b>{mainProducts.filter((product) => product.matchKind === "exact").length}</b></p></div>
                {linkedProducts.length === 0 && <div className="no-findings"><h2>No shopping matches found</h2><p>Spotted detected objects, but no retailer candidate passed the minimum visual and page checks.</p></div>}
                <div className="product-list">{mainProducts.map((product, index) => <ProductCard key={product.id} product={product} index={index} active={activeId === product.id} onSelect={() => setActiveId(product.id)} onTime={(appearance) => seek(product, appearance)} />)}</div>
                {possibleProducts.length > 0 && <section className="possible-section" aria-label="Possible matches"><div className="possible-title"><span>Possible matches</span><small>Visually plausible · not verified as exact</small></div><div className="product-list">{possibleProducts.map((product, index) => <ProductCard key={product.id} product={product} index={mainProducts.length + index} active={activeId === product.id} onSelect={() => setActiveId(product.id)} onTime={(appearance) => seek(product, appearance)} />)}</div></section>}
              </div>
            )}
          </div>
          </section>
        )}
      </div>

      {workspaceVisible && <button className="mobile-toggle" onClick={() => setMobileResults((value) => !value)}>{mobileResults ? "Show video" : `Show ${linkedProducts.length} matches`} <span>↗</span></button>}
      <footer><div className="logo footer-logo"><span>Spotted</span><i aria-hidden="true" /></div><span>Built for the OpenAI hackathon · 2026</span></footer>
    </main>
  );
}

function ProductCard({ product, index, active, onSelect, onTime }: { product: ProductFinding; index: number; active: boolean; onSelect: () => void; onTime: (appearance: Appearance) => void }) {
  const [imageFailed, setImageFailed] = useState(false);
  const image = product.imageUrl || product.appearances[0]?.thumbnailUrl;
  const appearanceGroups = groupAppearances(product.appearances);
  return (
    <article className={`product-card ${active ? "active" : ""}`} onMouseEnter={onSelect}>
      <button className="product-visual" onClick={onSelect} aria-label={`Show ${product.name} in video`}>
        {image && !imageFailed ? (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element -- product image hosts are discovered at runtime. */}
            <img src={image} alt="" onError={() => setImageFailed(true)} />
          </>
        ) : <span className="product-fallback">{(product.category || product.name).charAt(0)}</span>}
        <small>{String(index + 1).padStart(2, "0")}</small>
      </button>
      <div className="product-copy">
        <div className="product-meta"><span className={`match-label ${product.matchKind}`}>{product.matchKind}</span><span>{productPercent(product.matchConfidence ?? product.confidence)}% match confidence</span></div>
        <p>{product.brand || product.category}</p><h3>{product.name}</h3><small>{product.category}{product.model ? ` · ${product.model}` : ""}</small>
        <div className="timestamps"><span>Seen</span>{appearanceGroups.map((group) => <button key={group.first.startSec} title={group.count > 1 ? `${group.count} nearby sampled appearances grouped` : group.first.evidence} onClick={() => onTime(group.first)}>{appearanceGroupLabel(group)}</button>)}</div>
      </div>
      <div className="product-shop">{product.price && <strong>{product.price}</strong>}<small>{product.retailerName ? `at ${product.retailerName}` : product.matchKind === "possible" ? "Candidate retailer" : "Verified retailer"}</small>{product.productUrl && <a href={product.productUrl} target="_blank" rel="noopener noreferrer">View product <span>↗</span></a>}</div>
    </article>
  );
}
