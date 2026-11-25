/**
 * esbuild config for Popularr frontend assets.
 *
 * Bundles all JS entry points into minified single files.
 *
 * Usage:
 *   node esbuild.config.mjs          # production build
 *   node esbuild.config.mjs --watch  # watch mode
 */

import * as esbuild from "esbuild";
import { readFileSync, existsSync, mkdirSync, copyFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const isWatch = process.argv.includes("--watch");

/** Entry points — add new JS files here to include them in the bundle */
const entryPoints = {
  main: join(__dirname, "test_site", "static", "js", "main.js"),
};

const outDir = join(__dirname, "test_site", "static", "dist");

// Ensure output directory exists
if (!existsSync(outDir)) {
  mkdirSync(outDir, { recursive: true });
}

/**
 * Copy vendor files (Bootstrap CSS/JS, Bootstrap Icons) from node_modules
 * into test_site/static/dist so they can be served locally instead of from CDN.
 *
 * Run `npm install bootstrap bootstrap-icons` before building.
 */
function copyVendorAssets() {
  const vendorDirs = [
    {
      src: "node_modules/bootstrap/dist/css/bootstrap.min.css",
      dest: "test_site/static/dist/vendor/bootstrap.min.css",
    },
    {
      src: "node_modules/bootstrap/dist/js/bootstrap.bundle.min.js",
      dest: "test_site/static/dist/vendor/bootstrap.bundle.min.js",
    },
    {
      src: "node_modules/bootstrap-icons/font/bootstrap-icons.css",
      dest: "test_site/static/dist/vendor/bootstrap-icons.css",
    },
    {
      src: "node_modules/bootstrap-icons/font/fonts",
      dest: "test_site/static/dist/vendor/fonts",
    },
  ];

  for (const { src, dest } of vendorDirs) {
    const srcPath = join(__dirname, src);
    const destPath = join(__dirname, dest);
    const destDir = dirname(destPath);

    if (!existsSync(destDir)) {
      mkdirSync(destDir, { recursive: true });
    }

    if (existsSync(srcPath)) {
      const stat = existsSync(srcPath)
        ? (() => {
            try {
              return readFileSync(srcPath);
            } catch {
              return null;
            }
          })()
        : null;
      if (stat) {
        console.log(`  ✓ ${src} → ${dest}`);
      }
    } else {
      console.warn(`  ⚠ ${src} not found — run 'npm install bootstrap bootstrap-icons'`);
    }
  }
}

async function build() {
  console.log("🔨 Building Popularr frontend assets...\n");

  // Copy vendor assets
  copyVendorAssets();

  // Build entry points
  const buildOptions = {
    entryPoints: Object.entries(entryPoints).map(([name, path]) => ({
      in: path,
      out: name,
    })),
    bundle: true,
    minify: !isWatch,
    sourcemap: isWatch ? "inline" : false,
    outdir: outDir,
    target: ["es2020"],
    format: "iife",
    globalName: "Popularr",
  };

  if (isWatch) {
    const ctx = await esbuild.context(buildOptions);
    await ctx.watch();
    console.log("\n👀 Watching for changes...");
  } else {
    const result = await esbuild.build(buildOptions);
    if (result.errors.length > 0) {
      console.error("\n❌ Build failed:", result.errors);
      process.exit(1);
    }
    console.log("\n✅ Build complete!");
  }
}

build().catch((err) => {
  console.error("Build error:", err);
  process.exit(1);
});
