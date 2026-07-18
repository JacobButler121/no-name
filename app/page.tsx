"use client";

import { ChangeEvent, useEffect, useMemo, useState } from "react";

type Product = {
  id: string;
  name: string;
  category: string;
  price: string;
  time: number;
  end: number;
  confidence: number;
  color: string;
  detail: string;
};

const products: Product[] = [
  {
    id: "keyboard",
    name: "NuPhy Air75 V2",
    category: "Low-profile keyboard",
    price: "$109",
    time: 4,
    end: 13,
    confidence: 98,
    color: "#f46a22",
    detail: "Name spoken + matching key layout",
  },
  {
    id: "lamp",
    name: "Anglepoise Type 75",
    category: "Desk lamp",
    price: "$315",
    time: 13,
    end: 23,
    confidence: 94,
    color: "#f3c35e",
    detail: "Distinctive silhouette + visible finish",
  },
  {
    id: "headphones",
    name: "Sony WH-1000XM5",
    category: "Noise-canceling headphones",
    price: "$399",
    time: 23,
    end: 31,
    confidence: 96,
    color: "#536fff",
    detail: "Brand name spoken + matching headband",
  },
];

const formatTime = (seconds: number) => `0:${String(Math.floor(seconds)).padStart(2, "0")}`;

export default function Home() {
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisReady, setAnalysisReady] = useState(true);
  const [activeId, setActiveId] = useState("keyboard");
  const [currentTime, setCurrentTime] = useState(7);
  const [isPlaying, setIsPlaying] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);

  useEffect(() => {
    if (!isPlaying || isAnalyzing) return;
    const timer = window.setInterval(() => {
      setCurrentTime((time) => (time >= 31 ? 0 : Number((time + 0.25).toFixed(2))));
    }, 250);
    return () => window.clearInterval(timer);
  }, [isPlaying, isAnalyzing]);

  const visibleProducts = useMemo(
    () => products.filter((product) => currentTime >= product.time && currentTime < product.end),
    [currentTime],
  );

  useEffect(() => {
    if (visibleProducts.length) setActiveId(visibleProducts[0].id);
  }, [visibleProducts]);

  const startAnalysis = () => {
    setIsAnalyzing(true);
    setAnalysisReady(false);
    setIsPlaying(false);
    window.setTimeout(() => {
      setIsAnalyzing(false);
      setAnalysisReady(true);
      setCurrentTime(4);
      setActiveId("keyboard");
    }, 2600);
  };

  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setFileName(file.name);
    startAnalysis();
  };

  const jumpTo = (product: Product) => {
    setCurrentTime(product.time + 0.3);
    setActiveId(product.id);
    setIsPlaying(true);
  };

  const activeProduct = products.find((product) => product.id === activeId) ?? products[0];

  return (
    <main>
      <section className="hero">
        <nav className="nav shell">
          <a className="brand" href="#top" aria-label="SceneCart home">
            Scene<span>Cart</span><i />
          </a>
          <div className="nav-links">
            <a href="#how">How it works</a>
            <a href="#studio">Creator studio</a>
          </div>
          <button className="nav-cta" onClick={startAnalysis}>Try the demo <span>↗</span></button>
        </nav>

        <div className="shell hero-copy" id="top">
          <p className="eyebrow"><span className="pulse" /> Video intelligence for commerce</p>
          <h1>Every product.<br /><em>Right on cue.</em></h1>
          <p className="hero-description">SceneCart turns a creator video into a verified storefront—identifying the products viewers actually see, exactly when they see them.</p>
          <div className="hero-actions">
            <label className="primary-button">
              Upload a video <span>↑</span>
              <input type="file" accept="video/*" onChange={handleFile} />
            </label>
            <button className="text-button" onClick={startAnalysis}>Watch it work <span>↘</span></button>
          </div>
          <p className="microcopy">{fileName ? `Selected: ${fileName}` : "No setup. No tagging. Just upload."}</p>
        </div>
        <div className="orb orb-one" />
        <div className="orb orb-two" />
      </section>

      <section className="studio shell" id="studio" aria-label="SceneCart creator studio demo">
        <div className="studio-topline">
          <div><span className="status-dot" /> ANALYSIS COMPLETE <strong>•</strong> 3 PRODUCT MOMENTS</div>
          <div className="project-name">SCENECART / DESK SETUP TOUR</div>
        </div>

        <div className="player-grid">
          <div className="video-panel">
            <div className="video-frame">
              <div className="scene-image" />
              <div className="scanline scanline-one" />
              <div className="scanline scanline-two" />
              <div className="scene-timestamp">{formatTime(currentTime)} / 0:31</div>
              {isAnalyzing && (
                <div className="analysis-overlay">
                  <div className="scanner" />
                  <p>Reading the scene</p>
                  <span>Detecting products, labels, and mentions</span>
                </div>
              )}
              {analysisReady && visibleProducts.map((product) => (
                <button
                  className={`tag tag-${product.id}`}
                  key={product.id}
                  onClick={() => jumpTo(product)}
                  style={{ "--tag-color": product.color } as React.CSSProperties}
                >
                  <b>+</b> {product.name}
                </button>
              ))}
              <button className="play-button" onClick={() => setIsPlaying(!isPlaying)} aria-label={isPlaying ? "Pause demo" : "Play demo"}>
                {isPlaying ? "Ⅱ" : "▶"}
              </button>
              <div className="progress-track"><span style={{ width: `${(currentTime / 31) * 100}%` }} /></div>
            </div>

            <div className="timeline" aria-label="Detected product timeline">
              <div className="timeline-times"><span>0:00</span><span>0:10</span><span>0:20</span><span>0:31</span></div>
              <div className="timeline-line">
                {products.map((product) => (
                  <button
                    key={product.id}
                    className={`moment ${activeId === product.id ? "selected" : ""}`}
                    style={{ left: `${(product.time / 31) * 100}%`, "--moment-color": product.color } as React.CSSProperties}
                    onClick={() => jumpTo(product)}
                    aria-label={`Jump to ${product.name}`}
                  />
                ))}
                <span className="scrubber" style={{ left: `${(currentTime / 31) * 100}%` }} />
              </div>
            </div>
          </div>

          <aside className="product-panel">
            <div className="panel-heading"><span>SHOP THIS SCENE</span><span className="live-pill">LIVE</span></div>
            <div className="product-list">
              {products.map((product) => (
                <button
                  className={`product-card ${activeId === product.id ? "active" : ""}`}
                  key={product.id}
                  onClick={() => jumpTo(product)}
                >
                  <span className={`product-art ${product.id}`}><i /></span>
                  <span className="product-copy">
                    <small>{product.category}</small>
                    <strong>{product.name}</strong>
                    <em>{product.price}</em>
                  </span>
                  <span className="card-arrow">↗</span>
                </button>
              ))}
            </div>
            <div className="evidence">
              <span className="evidence-label">WHY WE&apos;RE SURE</span>
              <p><b>{activeProduct.confidence}% confidence</b> · {activeProduct.detail}</p>
            </div>
          </aside>
        </div>
      </section>

      <section className="proof shell" id="how">
        <p className="eyebrow">Built for the people making culture</p>
        <div className="proof-grid">
          <h2>Your audience is already asking.<br /><em>Give them an answer.</em></h2>
          <div className="proof-copy">
            <p>From product review to purchase-ready in minutes. SceneCart combines visual recognition, spoken context, and your approved catalog to make every recommendation actionable.</p>
            <div className="stat-row"><div><strong>03</strong><span>signals per match</span></div><div><strong>96%</strong><span>verified confidence</span></div><div><strong>31s</strong><span>to first storefront</span></div></div>
          </div>
        </div>
      </section>

      <footer className="footer shell"><span>© 2026 SCENECART</span><span>MADE FOR THE CREATOR ECONOMY</span><button onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>BACK TO TOP ↑</button></footer>
    </main>
  );
}
