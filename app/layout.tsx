import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Spotted — Products, right on cue.",
  description: "Paste a video. Find every product, every timestamp, and the closest trusted place to buy it.",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  openGraph: { title: "Spotted — Products, right on cue.", description: "Turn any video into a timestamped shopping list.", images: ["/og-spotted.png"] },
  twitter: { card: "summary_large_image", title: "Spotted — Products, right on cue.", description: "Turn any video into a timestamped shopping list.", images: ["/og-spotted.png"] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
