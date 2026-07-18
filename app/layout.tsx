import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const forwardedHost = requestHeaders.get("x-forwarded-host")?.split(",")[0].trim();
  const requestHost = forwardedHost || requestHeaders.get("host") || "localhost:3000";
  const safeHost = /^[a-z0-9.-]+(?::\d+)?$/i.test(requestHost)
    ? requestHost
    : "localhost:3000";
  const forwardedProtocol = requestHeaders.get("x-forwarded-proto")?.split(",")[0].trim();
  const protocol = forwardedProtocol === "http" || safeHost.startsWith("localhost")
    ? "http"
    : "https";
  const socialImage = `${protocol}://${safeHost}/og-spotted-v2.png`;

  return {
    title: "Spotted — Spot it. Buy it.",
    description: "Paste a video link, spot products at exact moments, and find the closest trusted place to buy them.",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "Spotted — Spot it. Buy it.",
      description: "Turn any video into a timestamped shopping list.",
      images: [socialImage],
    },
    twitter: {
      card: "summary_large_image",
      title: "Spotted — Spot it. Buy it.",
      description: "Turn any video into a timestamped shopping list.",
      images: [socialImage],
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
