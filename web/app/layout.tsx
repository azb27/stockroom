import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stockroom: ops agent on messy data",
  description:
    "An ops agent for a distributor's messy data. Ask about sales, stock-outs and forecasts; it drafts purchase orders a human approves. Measured on 120 ground-truth questions.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
