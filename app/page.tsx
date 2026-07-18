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

type Product = {
  id: string;
  name: string;
  brand: string;
  category: string;
  description: string;
  match: "exact" | "similar" | "possible";
  confidence: number;
  price: string;
  retailer: string;
  retailerUrl: string;
  timestamps: number[];
  color: string;
  box: { x: number; y: number; width: number; height: number };
};

type JobResponse = {
  id?: string;
  job_id?: string;
  status?: string;
  products?: Product[];
  results?: { products?: Product[] };
};

const fixture: Product[] = [
  {
    id: "air75",
    name: "Air75 V2",
    brand: "NuPhy",
    category: "Low-profile keyboard",
    description: "75% wireless mechanical keyboard · Basalt Black",
    match: "exact",
    confidence: 98,
    price: "$119.95",
    retailer: "NuPhy",
    retailerUrl: "https://nuphy.com/products/air75-v2",
    timestamps: [4, 18, 27],
    color: "graphite",
    box: { x: 14, y: 59, width: 52, height: 18 },
  },
  {
    id: "lamp",
    name: "Type 75 Desk Lamp",
    brand: "Anglepoise",
    category: "Task lighting",
    description: "Adjustable desk lamp · Jet Black",
    match: "similar",
    confidence: 91,
    price: "$340.00",
    retailer: "Anglepoise",
    retailerUrl: "https://www.anglepoise.com/products/type-75-desk-lamp",
    timestamps: [9, 21],
    color: "sand",
    box: { x: 68, y: 17, width: 20, height: 55 },
  },
  {
    id: "headphones",
    name: "WH-1000XM5",
    brand: "Sony",
    category: "Headphones",
    description: "Wireless noise canceling headphones · Black",
    match: "exact",
    confidence: 96,
    price: "$399.99",
    retailer: "Sony",
    retailerUrl: "https://electronics.sony.com/audio/headphones/headband/p/wh1000xm5-b",
    timestamps: [14, 24],
    color: "blue",
    box: { x: 72, y: 28, width: 18, height: 31 },
  },
  {
    id: "bottle",
    name: "24 oz Standard Mouth",
    brand: "Hydro Flask",
    category: "Drinkware",
    description: "Insulated stainless steel bottle · White",
    match: "possible",
    confidence: 73,
    price: "$39.95",
    retailer: "Hydro Flask",
    retailerUrl: "https://www.hydroflask.com/24-oz-standard-mouth",
    timestamps: [7],
    color: "white",
    box: { x: 6, y: 25, width: 10, height: 34 },
  },
];

const progressCopy: Record<EventType, string> = {
  retrieving_video: "Retrieving video",
  extracting_frames: "Mapping scenes",
  analyzing_frame: "Analyzing frame",
  candidate_found: "Candidate found",
  merging_duplicates: "Merging repeat sightings",
  searching_retailers: "Checking trusted retailers",
  product_ready: "Product match ready",
  retrieval_blocked: "Upload needed",
  completed: "Analysis complete",
  failed: "Analysis stopped",
};

const demoEvents: EventType[] = [
  "retrieving_video",
  "extracting_frames",
  "analyzing_frame",
  "candidate_found",
  "merging_duplicates",
  "searching_retailers",
  "product_ready",
  "completed",
];

function formatTime(seconds: number) {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function normalizeProducts(data: JobResponse): Product[] {
  const products = data.products ?? data.results?.products ?? [];
  return products.map((product, index) => ({
    ...product,
    id: product.id ?? `product-${index}`,
    brand: product.brand ?? "Unconfirmed brand",
    description: product.description ?? product.category ?? "Product match",
    match: product.match ?? "similar",
    confidence: product.confidence ?? 0,
    price: product.price ?? "View price",
    retailer: product.retailer ?? "Retailer",
    retailerUrl: product.retailerUrl ?? "#",
    timestamps: product.timestamps ?? [],
    color: product.color ?? "graphite",
    box: product.box ?? { x: 18, y: 22, width: 35, height: 35 },
  }));
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "running" | "complete" | "blocked" | "error">("idle");
  const [eventType, setEventType] = useState<EventType>("retrieving_video");
  const [eventIndex, setEventIndex] = useState(0);
  const [products, setProducts] = useState<Product[]>(fixture);
  const [activeId, setActiveId] = useState(fixture[0].id);
  const [currentTime, setCurrentTime] = useState(4);
  const [error, setError] = useState("");
  const [isFixture, setIsFixture] = useState(true);
  const [mobileResults, setMobileResults] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const activeProduct = products.find((product) => product.id === activeId) ?? products[0];

  useEffect(() => {
    if (status !== "running" || !isFixture) return;
    if (eventIndex >= demoEvents.length - 1) {
      setStatus("complete");
      setEventType("completed");
      return;
    }
    const timer = window.setTimeout(() => {
      const next = eventIndex + 1;
      setEventIndex(next);
      setEventType(demoEvents[next]);
      if (next >= 3) setProducts(fixture.slice(0, Math.min(next - 2, fixture.length)));
    }, 620);
    return () => window.clearTimeout(timer);
  }, [eventIndex, isFixture, status]);

  useEffect(() => {
    if (!jobId || isFixture || status !== "running") return;
    const stream = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    stream.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data) as { type: EventType; product?: Product; message?: string };
        setEventType(payload.type);
        if (payload.type === "product_ready" && payload.product) {
          setProducts((current) => [...current.filter((item) => item.id !== payload.product?.id), payload.product!]);
        }
        if (payload.type === "retrieval_blocked") {
          setStatus("blocked");
          setError(payload.message ?? "This platform blocked retrieval. Upload the video to keep going.");
          stream.close();
        }
        if (payload.type === "failed") {
          setStatus("error");
          setError(payload.message ?? "The processor could not finish this video.");
          stream.close();
        }
        if (payload.type === "completed") {
          stream.close();
          void fetchJob(jobId);
        }
      } catch {
        // Ignore malformed heartbeat messages from a tunnel or proxy.
      }
    };
    stream.onerror = () => stream.close();
    return () => stream.close();
  }, [isFixture, jobId, status]);

  async function fetchJob(id: string) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("Could not load the completed findings.");
      const data = (await response.json()) as JobResponse;
      const nextProducts = normalizeProducts(data);
      setProducts(nextProducts);
      setActiveId(nextProducts[0]?.id ?? "");
      setStatus("complete");
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not load findings.");
    }
  }

  async function submitUrl(event: FormEvent) {
    event.preventDefault();
    if (!url.trim()) return;
    setStatus("running");
    setEventType("retrieving_video");
    setProducts([]);
    setError("");
    setIsFixture(false);
    setMobileResults(false);
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: url.trim() }),
      });
      const data = (await response.json()) as JobResponse & { message?: string };
      if (!response.ok) throw new Error(data.message ?? "The video processor is unavailable.");
      const id = data.id ?? data.job_id;
      if (!id) throw new Error("The processor did not return a job ID.");
      setJobId(id);
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not start this video.");
    }
  }

  function runDemo() {
    setUrl("https://youtube.com/watch?v=desk-setup-tour");
    setJobId(null);
    setStatus("running");
    setEventType("retrieving_video");
    setEventIndex(0);
    setProducts([]);
    setError("");
    setIsFixture(true);
    setMobileResults(false);
  }

  async function uploadVideo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("video", file);
    setStatus("running");
    setEventType("retrieving_video");
    setProducts([]);
    setError("");
    setIsFixture(false);
    try {
      const response = await fetch("/api/jobs/upload", { method: "POST", body: form });
      const data = (await response.json()) as JobResponse & { message?: string };
      if (!response.ok) throw new Error(data.message ?? "The upload could not be started.");
      const id = data.id ?? data.job_id;
      if (!id) throw new Error("The processor did not return a job ID.");
      setJobId(id);
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof Error ? reason.message : "Could not upload this video.");
    }
  }

  function seek(product: Product, timestamp: number) {
    setActiveId(product.id);
    setCurrentTime(timestamp);
    setMobileResults(false);
    if (videoRef.current) {
      videoRef.current.currentTime = timestamp;
      void videoRef.current.play().catch(() => undefined);
    }
  }

  const mainProducts = products.filter((product) => product.match !== "possible");
  const possibleProducts = products.filter((product) => product.match === "possible");
  const progress = Math.max(7, ((demoEvents.indexOf(eventType) + 1) / demoEvents.length) * 100);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="logo" href="#top" aria-label="Spotted home"><span className="logo-mark">S</span>Spotted</a>
        <div className="topbar-center"><span className="live-dot" />AI video shopping</div>
        <button className="header-action" onClick={() => document.getElementById("composer")?.focus()}>New search <span>↗</span></button>
      </header>

      <section className="intro" id="top">
        <div>
          <p className="kicker">See it. Find it.</p>
          <h1>Turn any video<br />into a <em>shopping list.</em></h1>
        </div>
        <p className="intro-note">Paste a public video. Spotted finds the products, remembers every appearance, and checks the web for the closest buyable match.</p>
      </section>

      <form className="composer" onSubmit={submitUrl}>
        <label htmlFor="composer">Video link</label>
        <div className="composer-row">
          <span className="composer-icon">↗</span>
          <input id="composer" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="Paste a YouTube, TikTok, or Instagram link" inputMode="url" />
          <button type="submit" disabled={!url.trim() || status === "running"}>{status === "running" && !isFixture ? "Analyzing" : "Find products"}<span>→</span></button>
        </div>
        <div className="composer-meta">
          <div className="platforms"><span>YouTube</span><i /> <span>TikTok</span><i /> <span>Instagram</span></div>
          <div className="composer-links"><button type="button" onClick={runDemo}>Run the 30-second demo</button><button type="button" onClick={() => fileRef.current?.click()}>or upload a video</button></div>
        </div>
        <input ref={fileRef} className="file-input" type="file" accept="video/mp4,video/quicktime,video/webm" onChange={uploadVideo} />
      </form>

      {(status === "error" || status === "blocked") && (
        <div className="notice" role="alert"><div><strong>{status === "blocked" ? "This link needs a handoff" : "Couldn’t reach the processor"}</strong><p>{error}</p></div><button onClick={() => fileRef.current?.click()}>Upload video <span>↑</span></button></div>
      )}

      <section className={`workspace ${status === "running" ? "is-running" : ""}`} aria-label="Video findings workspace">
        <div className="video-column">
          <div className="panel-heading">
            <div><span className="step-number">01</span><div><strong>Video</strong><small>{isFixture ? "Desk setup tour · 0:31" : jobId ? `Job ${jobId.slice(0, 8)}` : "Ready for a link"}</small></div></div>
            <span className="source-badge">{isFixture ? "Demo preview" : "Live analysis"}</span>
          </div>

          <div className="video-stage">
            {!isFixture && jobId ? (
              <video ref={videoRef} controls src={`/api/jobs/${encodeURIComponent(jobId)}/media`} onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} />
            ) : (
              <div className="desk-scene" role="img" aria-label="Stylized desk setup video preview">
                <div className="scene-window"><i /><i /><i /><span /></div>
                <div className="scene-lamp"><i /><i /></div>
                <div className="scene-monitor"><span>SPOTTED</span></div>
                <div className="scene-keyboard" />
                <div className="scene-headphones" />
                <div className="scene-bottle" />
                <div className="scene-desk" />
                <div className="scene-light" />
              </div>
            )}
            {activeProduct && status !== "running" && <div className="detection-box" style={{ left: `${activeProduct.box.x}%`, top: `${activeProduct.box.y}%`, width: `${activeProduct.box.width}%`, height: `${activeProduct.box.height}%` }}><span>{activeProduct.brand} · {Math.round(activeProduct.confidence)}%</span></div>}
            <div className="video-topline"><span>SCENE 04</span><span>1080P</span></div>
            {isFixture && <div className="video-controls"><button aria-label="Play demo">▶</button><div className="video-track"><span style={{ width: `${(currentTime / 31) * 100}%` }} />{fixture.map((product) => product.timestamps.map((time) => <i key={`${product.id}-${time}`} style={{ left: `${(time / 31) * 100}%` }} />))}</div><time>{formatTime(currentTime)} / 0:31</time></div>}
          </div>

          <div className="moments">
            <div><span className="moment-count">{products.reduce((sum, product) => sum + product.timestamps.length, 0).toString().padStart(2, "0")}</span><span>Product moments<br />across {products.length || "—"} unique finds</span></div>
            <div className="moment-list">{products.flatMap((product) => product.timestamps.map((time) => <button key={`${product.id}-${time}`} className={activeId === product.id && currentTime === time ? "active" : ""} onClick={() => seek(product, time)}><span>{formatTime(time)}</span>{product.brand}</button>))}</div>
          </div>
        </div>

        <div className={`results-column ${mobileResults ? "mobile-open" : ""}`}>
          <div className="panel-heading">
            <div><span className="step-number">02</span><div><strong>Findings</strong><small>{status === "running" ? progressCopy[eventType] : `${products.length} unique products`}</small></div></div>
            {status === "complete" && <span className="complete-badge"><i />Complete</span>}
          </div>

          {status === "running" ? (
            <div className="processing" aria-live="polite">
              <div className="scan-orbit"><span>{Math.round(progress)}%</span><i /><i /><i /></div>
              <h2>{progressCopy[eventType]}</h2>
              <p>Spotted is reading visual evidence, merging repeat appearances, and checking trustworthy product pages.</p>
              <div className="processing-bar"><span style={{ width: `${progress}%` }} /></div>
              <ol>{demoEvents.slice(0, -1).map((item, index) => <li className={index < demoEvents.indexOf(eventType) ? "done" : index === demoEvents.indexOf(eventType) ? "active" : ""} key={item}><i>{index < demoEvents.indexOf(eventType) ? "✓" : index + 1}</i>{progressCopy[item]}</li>)}</ol>
            </div>
          ) : (
            <div className="findings-scroll">
              <div className="results-summary"><div><strong>{mainProducts.length.toString().padStart(2, "0")}</strong><span>High-confidence<br />findings</span></div><p><i />Exact match <b>{mainProducts.filter((p) => p.match === "exact").length}</b></p></div>
              <div className="product-list">
                {mainProducts.map((product, index) => <ProductCard key={product.id} product={product} index={index} active={activeId === product.id} onSelect={() => seek(product, product.timestamps[0] ?? 0)} onTime={(time) => seek(product, time)} />)}
              </div>
              {possibleProducts.length > 0 && <div className="possible-section"><div className="possible-title"><span>Possible finds</span><small>Lower confidence · review suggested</small></div>{possibleProducts.map((product, index) => <ProductCard key={product.id} product={product} index={mainProducts.length + index} active={activeId === product.id} onSelect={() => seek(product, product.timestamps[0] ?? 0)} onTime={(time) => seek(product, time)} />)}</div>}
            </div>
          )}
        </div>
      </section>

      <button className="mobile-toggle" onClick={() => setMobileResults((value) => !value)}>{mobileResults ? "Show video" : `Show ${products.length} findings`} <span>↗</span></button>

      <footer><div className="logo footer-logo"><span className="logo-mark">S</span>Spotted</div><p>Products, right on cue.</p><span>Built for the OpenAI hackathon · 2026</span></footer>
    </main>
  );
}

function ProductCard({ product, index, active, onSelect, onTime }: { product: Product; index: number; active: boolean; onSelect: () => void; onTime: (time: number) => void }) {
  return (
    <article className={`product-card ${active ? "active" : ""}`} onMouseEnter={onSelect}>
      <button className={`product-visual ${product.color}`} onClick={onSelect} aria-label={`Show ${product.brand} ${product.name} in video`}><span className={`product-shape shape-${product.id}`} /><small>{(index + 1).toString().padStart(2, "0")}</small></button>
      <div className="product-copy">
        <div className="product-meta"><span className={`match-label ${product.match}`}>{product.match}</span><span>{product.confidence}% confidence</span></div>
        <p>{product.brand}</p><h3>{product.name}</h3><small>{product.description}</small>
        <div className="timestamps"><span>Seen at</span>{product.timestamps.map((time) => <button key={time} onClick={() => onTime(time)}>{formatTime(time)}</button>)}</div>
      </div>
      <div className="product-shop"><strong>{product.price}</strong><small>at {product.retailer}</small><a href={product.retailerUrl} target="_blank" rel="noreferrer">View product <span>↗</span></a></div>
    </article>
  );
}
