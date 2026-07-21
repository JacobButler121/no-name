import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Spotted Image Relay",
  description: "Short-lived signed image transport for Spotted product search.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
