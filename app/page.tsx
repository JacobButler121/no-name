"use client";

import { useEffect, useState } from "react";

type Product = {
  name: string;
  category: string;
  timestamp: string;
  price: string;
  confidence: number;
  query: string;
  className: string;
};

const products: Product[] = [
  { name: "NuPhy Air75 V2", category: "Low-profile keyboard", timestamp: "00:04", price: "$109", confidence: 98, query: "NuPhy Air75 V2", className: "keyboard" },
  { name: "Anglepoise Type 75", category: "Desk lamp", timestamp: "00:13", price: "$315", confidence: 94, query: "Anglepoise Type 75 desk lamp", className: "lamp" },
  { name: "Sony WH-1000XM5", category: "Noise-canceling headphones", timestamp: "00:23", price: "$399", confidence: 96, query: "Sony WH-1000XM5 headphones", className: "headphones" },
];

const steps = [
  "Opening video and mapping scenes",
  "Reading on-screen labels and logos",
  "Matching visual product candidates",
  "Searching verified retailer listings",
  "Checking availability and price",
];

export default function Home() {
  const [url, setUrl] = useState("");
  const [progress, setProgress] = useState(0);
  const [isRunning, setIsRunning] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [activeProduct, setActiveProduct] = useState(0);

  useEffect(() => {
    if (!isRunning) return;
    if (progress >= steps.length) {
      setIsRunning(false);
      setIsComplete(true);
      return;
    }
    const timer = window.setTimeout(() => setProgress((value) => value + 1), 820);
    return () => window.clearTimeout(timer);
  }, [isRunning, progress]);

  const startFinding = () => {
    setProgress(0);
    setIsComplete(false);
    setIsRunning(true);
    if (!url) setUrl("https://www.youtube.com/watch?v=desk-setup-tour");
  };

  const reset = () => {
    setProgress(0);
    setIsRunning(false);
    setIsComplete(false);
  };

  return (
    <main>
      <nav className="nav shell">
        <a className="brand" href="#top">Scene<span>Cart</span><i /></a>
        <div className="nav-center"><a href="#how">How it works</a><a href="#results">Examples</a></div>
        <button className="nav-button" onClick={() => document.getElementById("finder")?.scrollIntoView({ behavior: "smooth" })}>Find products <span>↗</span></button>
      </nav>

      <section className="hero shell" id="top">
        <div className="hero-copy">
          <p className="eyebrow"><span className="dot" /> MULTIMODAL SHOPPING INTELLIGENCE</p>
          <h1>See it.<br /><em>Shop it.</em></h1>
          <p>Paste any video link. SceneCart watches for products, searches the web, and gives you the exact moment to buy.</p>
        </div>
        <div className="hero-note"><span>01</span> From video to verified products<br />in one intelligent run.</div>
      </section>

      <section className="finder-wrap shell" id="finder">
        <div className="finder-card">
          <div className="finder-label"><span>DROP A LINK</span><small>YOUTUBE · TIKTOK · INSTAGRAM REELS</small></div>
          <div className="search-row">
            <span className="link-icon">↗</span>
            <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="Paste a video link to find every product inside…" aria-label="Video URL" />
            <button onClick={startFinding} disabled={isRunning}>{isRunning ? "Searching…" : "Find products"}<span>→</span></button>
          </div>
          <div className="source-pills"><span>▶ YouTube</span><span>♪ TikTok</span><span>◎ Instagram</span><button onClick={() => { setUrl("https://www.youtube.com/watch?v=desk-setup-tour"); startFinding(); }}>Try our desk setup demo →</button></div>
        </div>

        {(isRunning || isComplete) && (
          <section className="run-card" aria-live="polite">
            <div className="run-heading"><div><span className="run-dot" /> {isComplete ? "Search run finished" : "AI search run"}</div><span className={`run-status ${isComplete ? "done" : ""}`}>{isComplete ? "Found 3 products" : "1 running"}</span></div>
            <div className="run-body">
              <div className="run-title"><b>{isComplete ? "Products are ready to shop" : "Finding products in this video"}</b><button onClick={reset}>{isComplete ? "New search" : "Minimize"} <span>↘</span></button></div>
              <ol className="run-steps">
                {steps.map((step, index) => <li className={index < progress ? "complete" : index === progress && isRunning ? "current" : ""} key={step}><i>{index < progress ? "✓" : ""}</i>{step}</li>)}
              </ol>
              <div className="progress-bar"><span style={{ width: `${(progress / steps.length) * 100}%` }} /></div>
              <div className="run-footer"><div className="query-pills"><span className={progress > 1 ? "complete" : ""}>{progress > 1 ? "✓" : ""} brand + model</span><span className={progress > 2 ? "complete" : ""}>{progress > 2 ? "✓" : ""} visual match</span><span className={progress > 3 ? "complete" : ""}>{progress > 3 ? "✓" : ""} web listings</span></div><button onClick={reset}>↻ Reset</button></div>
            </div>
          </section>
        )}
      </section>

      <section className={`results shell ${isComplete ? "visible" : ""}`} id="results">
        <div className="results-intro"><div><p className="eyebrow"><span className="dot" /> VERIFIED RESULTS</p><h2>Found in this<br /><em>video.</em></h2></div><p>Every match is grounded in visual evidence, spoken context, and live web search—not a guess.</p></div>
        <div className="result-grid">
          <div className="video-result">
            <div className="scene-poster"><div className="scene-shade" /><span className="scene-label">DESK SETUP TOUR</span><button className="scene-play">▶</button><span className="scene-time">00:04 / 00:31</span><div className="scene-progress"><i /></div></div>
            <div className="scene-scrubber">{products.map((product, index) => <button className={activeProduct === index ? "active" : ""} onClick={() => setActiveProduct(index)} key={product.name} style={{ left: `${18 + index * 31}%` }} aria-label={`View ${product.name}`}><i /></button>)}</div>
          </div>
          <div className="matches">
            <div className="matches-heading"><span>SHOP THE MOMENT</span><span>3 MATCHES</span></div>
            {products.map((product, index) => <article className={`match ${activeProduct === index ? "active" : ""}`} key={product.name} onMouseEnter={() => setActiveProduct(index)}>
              <div className={`product-art ${product.className}`}><i /></div>
              <div className="match-copy"><span>{product.category}</span><h3>{product.name}</h3><p><b>{product.confidence}% match</b> · seen at <button onClick={() => setActiveProduct(index)}>{product.timestamp}</button></p></div>
              <div className="shop"><strong>{product.price}</strong><a href={`https://www.google.com/search?q=${encodeURIComponent(product.query)}`} target="_blank" rel="noreferrer">Shop ↗</a></div>
            </article>)}
          </div>
        </div>
      </section>

      <section className="how" id="how"><div className="shell how-grid"><div><p className="eyebrow"><span className="dot" /> HOW THE MODEL FINDS</p><h2>It watches<br />like a <em>shopper.</em></h2></div><div className="how-copy"><p>SceneCart combines the frame, the words spoken in the video, text on packaging, and live product listings to find the best match.</p><div><span>01</span><b>See</b> · recognize the object, brand and details</div><div><span>02</span><b>Search</b> · compare the web&apos;s best matches</div><div><span>03</span><b>Verify</b> · return product links with evidence</div></div></div></section>

      <footer className="shell footer"><span>SCENECART © 2026</span><span>THE SHOPPING LAYER FOR VIDEO</span><button onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>BACK TO TOP ↑</button></footer>
    </main>
  );
}
