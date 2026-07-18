"use client";

import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";

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
  findings?: ProductFinding[];
  error?: { message?: string } | string;
  message?: string;
  detail?: string;
};

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
  searching_retailers: "Searching trusted retailers",
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

export default function Home() {
  const [url, setUrl] = useState("");
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
  const streamRef = useRef<EventSource | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const activeProduct = products.find((product) => product.id === activeId) ?? products[0];
  const mainProducts = products.filter((product) => product.matchKind !== "possible");
  const possibleProducts = products.filter((product) => product.matchKind === "possible");
  const progressIndex = eventOrder.indexOf(eventType);
  const progress = status === "complete" ? 100 : Math.max(6, ((Math.max(0, progressIndex) + 1) / eventOrder.length) * 100);

  useEffect(() => () => streamRef.current?.close(), []);

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
    setActiveId((current) => current || product.id);
  }

  async function fetchJob(id: string) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { cache: "no-store" });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "Could not load the completed findings."));
      const findings = Array.isArray(data.findings) ? data.findings.filter(isFinding) : [];
      setProducts(findings);
      setActiveId((current) => current || findings[0]?.id || "");
      setStatus(data.status === "failed" ? "error" : "complete");
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not load findings.");
    }
  }

  function handleServerEvent(type: EventType, raw: string, id: string) {
    let payload: Record<string, unknown> = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch { /* named event can have no JSON body */ }
    recordEvent(type);
    if (type === "product_ready" && isFinding(payload)) addFinding(payload);
    if (type === "retrieval_blocked") {
      setStatus("blocked");
      setError(typeof payload.message === "string" ? payload.message : "This platform did not provide the video. Upload the file to continue.");
      streamRef.current?.close();
    } else if (type === "failed") {
      setStatus("error");
      setError(typeof payload.message === "string" ? payload.message : "The processor could not finish this video.");
      streamRef.current?.close();
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
    setStatus("running");
    connectEvents(data.jobId);
  }

  async function submitUrl(event: FormEvent) {
    event.preventDefault();
    const videoUrl = url.trim();
    if (!videoUrl) return;
    try { new URL(videoUrl); } catch { setError("Paste a complete YouTube, TikTok, or Instagram URL."); setStatus("error"); return; }
    setStatus("starting");
    setEventType("retrieving_video");
    setEventHistory(["retrieving_video"]);
    setProducts([]);
    setActiveId("");
    setActiveAppearance(null);
    setError("");
    setMobileResults(false);
    setVideoReady(false);
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: videoUrl }),
      });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "The video processor is unavailable."));
      startJob(data);
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not start this video.");
    }
  }

  async function uploadVideo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setStatus("starting");
    setEventType("retrieving_video");
    setEventHistory(["retrieving_video"]);
    setProducts([]);
    setActiveId("");
    setActiveAppearance(null);
    setError("");
    setPlatform("upload");
    setVideoReady(false);
    try {
      const response = await fetch("/api/jobs/upload", { method: "POST", body: form });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(responseMessage(data, "The upload could not be started."));
      startJob(data);
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not upload this video.");
    } finally {
      event.target.value = "";
    }
  }

  async function newSearch() {
    streamRef.current?.close();
    const id = jobId;
    setJobId(null);
    setStatus("idle");
    setProducts([]);
    setEventHistory([]);
    setActiveId("");
    setActiveAppearance(null);
    setCurrentTime(0);
    setError("");
    setVideoReady(false);
    if (id) {
      try { await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE", keepalive: true }); } catch { /* jobs also expire */ }
    }
  }

  function seek(product: ProductFinding, appearance: Appearance) {
    setActiveId(product.id);
    setActiveAppearance(appearance);
    setCurrentTime(appearance.startSec);
    setMobileResults(false);
    if (videoRef.current) {
      videoRef.current.currentTime = appearance.startSec;
      void videoRef.current.play().catch(() => undefined);
    }
  }

  const workspaceVisible = status === "starting" || status === "running" || status === "complete";
  const currentBox = activeAppearance?.boundingBox || activeProduct?.appearances.find((appearance) => currentTime >= appearance.startSec && currentTime <= (appearance.endSec ?? appearance.startSec + 3))?.boundingBox;

  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="logo logo-button" type="button" onClick={() => void newSearch()} aria-label="Spotted home"><span className="logo-mark">S</span>Spotted</button>
        <div className="topbar-center"><span className="live-dot" />AI product discovery</div>
        <button className="header-action" onClick={() => void newSearch()}>New search <span>＋</span></button>
      </header>

      <section className="intro" id="top">
        <div><p className="kicker">Shop what you watch</p><h1>Spot it in a video.<br /><em>Find it online.</em></h1></div>
        <p className="intro-note">Spotted studies the scenes, identifies what matters, and finds the closest products you can actually buy.</p>
      </section>

      <form className="composer" onSubmit={submitUrl}>
        <label htmlFor="composer">Drop a public video link</label>
        <div className="composer-row">
          <span className="composer-icon">↗</span>
          <input id="composer" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="YouTube, TikTok, or Instagram URL" inputMode="url" disabled={status === "starting"} />
          <button type="submit" disabled={!url.trim() || status === "starting"}>{status === "starting" ? "Starting" : "Find products"}<span>→</span></button>
        </div>
        <div className="composer-meta">
          <div className="platforms"><span>YouTube</span><i /><span>TikTok</span><i /><span>Instagram</span></div>
          <div className="composer-links"><button type="button" onClick={() => fileRef.current?.click()}>Upload a video instead</button></div>
        </div>
        <input ref={fileRef} className="file-input" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" onChange={uploadVideo} />
      </form>

      {(status === "error" || status === "blocked") && (
        <div className="notice" role="alert">
          <div><strong>{status === "blocked" ? "This link needs a handoff" : "The scan couldn’t start"}</strong><p>{error}</p></div>
          <button onClick={() => fileRef.current?.click()}>Upload video <span>↑</span></button>
        </div>
      )}

      {status === "idle" && (
        <section className="honest-empty" aria-label="How Spotted works">
          <article><span>01</span><h2>Understands scenes</h2><p>Samples key moments and reads visual details, labels, and context.</p></article>
          <article><span>02</span><h2>Resolves repeats</h2><p>One product card keeps every timestamp where the same item appears.</p></article>
          <article><span>03</span><h2>Searches with evidence</h2><p>Exact matches stay separate from alternatives and possible finds.</p></article>
        </section>
      )}

      {workspaceVisible && (
        <section className={`workspace ${status === "running" || status === "starting" ? "is-running" : ""}`} aria-label="Video findings workspace">
          <div className="video-column">
            <div className="panel-heading">
              <div><span className="step-number">01</span><div><strong>Video</strong><small>{jobId ? `${platform} · Job ${jobId.slice(0, 8)}` : "Creating secure session"}</small></div></div>
              <span className="source-badge">Live analysis</span>
            </div>
            <div className="video-stage real-video-stage">
              {jobId && <video ref={videoRef} controls playsInline preload="metadata" src={`/api/jobs/${encodeURIComponent(jobId)}/media`} onCanPlay={() => setVideoReady(true)} onTimeUpdate={(event) => { setCurrentTime(event.currentTarget.currentTime); if (activeAppearance && Math.abs(event.currentTarget.currentTime - activeAppearance.startSec) > 4) setActiveAppearance(null); }} />}
              {!videoReady && <div className="video-loading"><div className="scan-orbit"><span>AI</span><i /><i /><i /></div><strong>Preparing playback</strong><small>The first frames will appear here.</small></div>}
              {currentBox && videoReady && activeProduct && <div className="detection-box" style={{ left: `${currentBox.x * 100}%`, top: `${currentBox.y * 100}%`, width: `${currentBox.width * 100}%`, height: `${currentBox.height * 100}%` }}><span>{activeProduct.name} · {productPercent(activeProduct.confidence)}%</span></div>}
            </div>
            <div className="moments">
              <div><span className="moment-count">{products.reduce((sum, product) => sum + product.appearances.length, 0).toString().padStart(2, "0")}</span><span>Product moments<br />across {products.length || "—"} unique finds</span></div>
              <div className="moment-list">{products.flatMap((product) => product.appearances.map((appearance, index) => <button key={`${product.id}-${appearance.startSec}-${index}`} className={activeId === product.id && currentTime === appearance.startSec ? "active" : ""} onClick={() => seek(product, appearance)}><span>{formatTime(appearance.startSec)}</span>{product.brand || product.name}</button>))}</div>
            </div>
          </div>

          <div className={`results-column ${mobileResults ? "mobile-open" : ""}`}>
            <div className="panel-heading">
              <div><span className="step-number">02</span><div><strong>Findings</strong><small>{status === "complete" ? `${products.length} unique products` : eventCopy[eventType]}</small></div></div>
              {status === "complete" && <span className="complete-badge"><i />Complete</span>}
            </div>
            {status !== "complete" && products.length === 0 ? (
              <div className="processing" aria-live="polite">
                <div className="scan-orbit"><span>{Math.round(progress)}%</span><i /><i /><i /></div>
                <h2>{eventCopy[eventType]}</h2>
                <p>Verified matches will appear here as the model recognizes, groups, and checks them.</p>
                <div className="processing-bar"><span style={{ width: `${progress}%` }} /></div>
                <ol>{eventOrder.slice(0, -1).map((item, index) => <li className={eventHistory.includes(item) ? "done" : item === eventType ? "active" : ""} key={item}><i>{eventHistory.includes(item) ? "✓" : index + 1}</i>{eventCopy[item]}</li>)}</ol>
              </div>
            ) : (
              <div className="findings-scroll">
                <div className="results-summary"><div><strong>{mainProducts.length.toString().padStart(2, "0")}</strong><span>High-confidence<br />findings</span></div><p><i />Exact match <b>{mainProducts.filter((product) => product.matchKind === "exact").length}</b></p></div>
                {products.length === 0 && <div className="no-findings"><h2>No confident matches</h2><p>Spotted finished the video without inventing an identification it could not support.</p></div>}
                <div className="product-list">{mainProducts.map((product, index) => <ProductCard key={product.id} product={product} index={index} active={activeId === product.id} onSelect={() => setActiveId(product.id)} onTime={(appearance) => seek(product, appearance)} />)}</div>
                {possibleProducts.length > 0 && <div className="possible-section"><div className="possible-title"><span>Possible finds</span><small>Lower confidence · review suggested</small></div>{possibleProducts.map((product, index) => <ProductCard key={product.id} product={product} index={mainProducts.length + index} active={activeId === product.id} onSelect={() => setActiveId(product.id)} onTime={(appearance) => seek(product, appearance)} />)}</div>}
              </div>
            )}
          </div>
        </section>
      )}

      {workspaceVisible && <button className="mobile-toggle" onClick={() => setMobileResults((value) => !value)}>{mobileResults ? "Show video" : `Show ${products.length} findings`} <span>↗</span></button>}
      <footer><div className="logo footer-logo"><span className="logo-mark">S</span>Spotted</div><p>Products, right on cue.</p><span>Built for the OpenAI hackathon · 2026</span></footer>
    </main>
  );
}

function ProductCard({ product, index, active, onSelect, onTime }: { product: ProductFinding; index: number; active: boolean; onSelect: () => void; onTime: (appearance: Appearance) => void }) {
  const [imageFailed, setImageFailed] = useState(false);
  const image = product.imageUrl || product.appearances[0]?.thumbnailUrl;
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
        <div className="product-meta"><span className={`match-label ${product.matchKind}`}>{product.matchKind}</span><span>{productPercent(product.confidence)}% confidence</span></div>
        <p>{product.brand || product.category}</p><h3>{product.name}</h3><small>{product.category}{product.model ? ` · ${product.model}` : ""}</small>
        <div className="timestamps"><span>Seen at</span>{product.appearances.map((appearance, index) => <button key={`${appearance.startSec}-${index}`} title={appearance.evidence} onClick={() => onTime(appearance)}>{formatTime(appearance.startSec)}</button>)}</div>
      </div>
      <div className="product-shop">{product.price && <strong>{product.price}</strong>}<small>{product.retailerName ? `at ${product.retailerName}` : "Match verified"}</small>{product.productUrl && <a href={product.productUrl} target="_blank" rel="noopener noreferrer">View product <span>↗</span></a>}</div>
    </article>
  );
}
