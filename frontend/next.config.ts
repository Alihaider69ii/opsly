import type { NextConfig } from "next";

// Headers every dashboard response carries. They only restrict what the browser will do with the
// dashboard; none of them can break a page that works today:
//  - the dashboard is never meant to be framed (frame-ancestors / X-Frame-Options stop clickjacking),
//  - nosniff stops a browser from running an upload as a script,
//  - the referrer policy keeps full URLs (the Shopify callback carries a one-time code) from being
//    sent to other sites.
// A script-src CSP and form-action are deliberately NOT set here: Next.js injects inline scripts that
// need per-request nonces, and form-action would block the redirect to Shopify's authorize page.
const baseSecurityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
  },
];

// HSTS tells browsers to use https only. It is meaningless (and ignored) over http, so it is sent in
// production builds, which are served over https behind the reverse proxy.
const productionOnlyHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
];

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // Do not advertise the framework.
  poweredByHeader: false,
  async headers() {
    const headers =
      process.env.NODE_ENV === "production"
        ? [...baseSecurityHeaders, ...productionOnlyHeaders]
        : baseSecurityHeaders;
    return [{ source: "/:path*", headers }];
  },
  // The Shopify OAuth callback's query carries a single-use code and signature; keep it out of the
  // development server's request log (production does not log requests).
  logging: { incomingRequests: { ignore: [/\/integrations\/shopify\/callback/] } },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
