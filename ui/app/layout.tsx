import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Helm",
  description: "Helm trading terminal control panel",
  referrer: "no-referrer",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
