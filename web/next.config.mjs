/** Static export: FastAPI serves web/out, so the demo is one container and one URL (ADR 0007). */
const nextConfig = {
  output: "export",
  images: { unoptimized: true },
  reactStrictMode: true,
  // `next dev` talks to a local `stockroom-web` on :7860
  ...(process.env.NODE_ENV === "development"
    ? { async rewrites() { return [{ source: "/api/:path*", destination: "http://127.0.0.1:7860/api/:path*" }]; } }
    : {}),
};
export default nextConfig;
