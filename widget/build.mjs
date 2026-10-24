// Bundles src/widget.js into one minified, self-contained script and publishes a copy where the
// Next.js app serves static files, so the dashboard's preview and the embed snippet use the same file.
import { build } from "esbuild";
import { copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "dist", "widget.js");

await build({
  entryPoints: [join(here, "src", "widget.js")],
  outfile: out,
  bundle: true,
  minify: true,
  format: "iife",
  globalName: "BizFlowChat",
  target: ["es2019"],
  legalComments: "none",
  logLevel: "info",
});

const published = join(here, "..", "frontend", "public", "widget.js");
await mkdir(dirname(published), { recursive: true });
await copyFile(out, published);
console.log(`published ${published}`);
