import type { ReactNode } from "react";
import {
  Fraunces,
  Geist,
  Geist_Mono,
  Inter,
  JetBrains_Mono,
} from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-inter",
  display: "swap",
});

const fraunces = Fraunces({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-fraunces",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-jetbrains",
  display: "swap",
});

// ProvenHire design-system fonts (see globals.css's `--font-sans`/`--ph-mono`).
// Inter/JetBrains stay loaded too — a handful of not-yet-migrated components
// still reference them directly, and removing them isn't in scope here.
const geistSans = Geist({
  subsets: ["latin"],
  variable: "--font-geist-sans",
  display: "swap",
});

const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-geist-mono",
  display: "swap",
});

export const metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ?? "https://proven-hire.ai",
  ),
  title: {
    default:
      "Proven Hire Job Description Interview — Practice the interview out loud",
    template: "%s · Proven Hire Job Description Interview",
  },
  description:
    "Open-source, voice-first AI mock interviews. Proven Hire Job Description Interview reads your CV and the job, researches the company, runs an adaptive voice interview, then shows you exactly what to fix. English-first, 10+ languages.",
  applicationName: "Proven Hire Job Description Interview",
  openGraph: {
    title:
      "Proven Hire Job Description Interview — Practice the interview out loud",
    description:
      "Open-source, voice-first AI mock interviews — practice out loud, then pass the real one.",
    url: "/",
    siteName: "Proven Hire Job Description Interview",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title:
      "Proven Hire Job Description Interview — Practice the interview out loud",
    description:
      "Open-source, voice-first AI mock interviews — practice out loud, then pass the real one.",
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${fraunces.variable} ${jetbrains.variable} ${geistSans.variable} ${geistMono.variable}`}
    >
      <body className="font-sans">
        <noscript>
          <style
            dangerouslySetInnerHTML={{
              __html: ".reveal{opacity:1!important;transform:none!important}",
            }}
          />
        </noscript>
        {children}
      </body>
    </html>
  );
}
