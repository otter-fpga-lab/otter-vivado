import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const defaultNodeModules = path.resolve(
  rootDir,
  "..",
  "otter-rtl-coding",
  "node_modules",
);

function resolveNodeModules() {
  const candidates = [];
  if (process.env.NODE_PATH) {
    for (const part of process.env.NODE_PATH.split(path.delimiter)) {
      if (part.trim()) candidates.push(path.resolve(part.trim()));
    }
  }
  candidates.push(path.join(rootDir, "node_modules"));
  candidates.push(defaultNodeModules);
  for (const dir of candidates) {
    if (
      fs.existsSync(path.join(dir, "esbuild")) &&
      fs.existsSync(path.join(dir, "@modelcontextprotocol", "ext-apps"))
    ) {
      return dir;
    }
  }
  throw new Error(
    `Cannot locate esbuild and @modelcontextprotocol/ext-apps. Searched: ${candidates.join(", ")}`,
  );
}

const nodeModulesDir = resolveNodeModules();
const { default: esbuild } = await import(
  `file:///${path.join(nodeModulesDir, "esbuild", "lib", "main.js").replace(/\\/g, "/")}`
);

const enLocale = path.join(nodeModulesDir, "zod", "v4", "locales", "en.js");
const slimZodLocales = {
  name: "slim-zod-locales",
  setup(build) {
    build.onResolve({ filter: /^\.\/.*\.js$/ }, (args) => {
      if (
        args.importer &&
        args.importer.replace(/\\/g, "/").endsWith("/zod/v4/locales/index.js")
      ) {
        if (
          args.path !== "./en.js" &&
          args.path !== "./zh-CN.js" &&
          args.path !== "./index.js"
        ) {
          return { path: enLocale };
        }
      }
      return undefined;
    });
  },
};

async function buildConsoleHtml() {
  const uiDir = path.join(rootDir, "ui", "console");
  const outFile = path.join(rootDir, "src", "vivado_mcp", "web", "console.html");

  const rawCss = fs.readFileSync(path.join(uiDir, "styles.css"), "utf8");
  const cssResult = await esbuild.transform(rawCss, {
    loader: "css",
    minify: true,
    target: ["es2022"],
  });
  const safeCss = cssResult.code.trim().replace(/<\/style/gi, "<\\/style");

  const jsResult = await esbuild.build({
    entryPoints: [path.join(uiDir, "main.mjs")],
    nodePaths: [nodeModulesDir],
    plugins: [slimZodLocales],
    bundle: true,
    write: false,
    format: "iife",
    platform: "browser",
    target: ["es2022"],
    minify: true,
    treeShaking: true,
    legalComments: "none",
    charset: "utf8",
  });

  if (!jsResult.outputFiles || jsResult.outputFiles.length !== 1) {
    throw new Error("Expected exactly 1 esbuild output file");
  }
  const safeJs = jsResult.outputFiles[0].text
    .trim()
    .replace(/<\/script/gi, "<\\/script");

  const template = fs.readFileSync(path.join(uiDir, "template.html"), "utf8");
  const html = template
    .replace("<!-- __INLINE_CSS__ -->", () => `<style>${safeCss}</style>`)
    .replace("<!-- __INLINE_JS__ -->", () => `<script>${safeJs}</script>`);

  const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/i);
  if (!scriptMatch) {
    throw new Error("Missing inline <script> in built console.html");
  }
  new vm.Script(scriptMatch[1], { filename: "console.inline.js" });

  if (/https?:\/\//i.test(template) || /https?:\/\//i.test(rawCss)) {
    throw new Error("External URL detected in console template or CSS");
  }

  const nonScriptHtml = html.replace(/<script>[\s\S]*?<\/script>/gi, "");
  if (/https?:\/\//i.test(nonScriptHtml)) {
    throw new Error("External URL detected outside inline <script> in console.html");
  }
  if (/<(?:script|link|img|iframe|source|video|audio)\b[^>]*(?:src|href)\s*=/i.test(nonScriptHtml)) {
    throw new Error("External resource tag detected in console.html");
  }

  const byteLen = Buffer.byteLength(html, "utf8");
  if (byteLen >= 500 * 1024) {
    throw new Error(`Bundle exceeds 500 KB budget: ${byteLen} bytes`);
  }

  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.writeFileSync(outFile, html, "utf8");
  process.stdout.write(
    `Built ${path.relative(rootDir, outFile)} (${(byteLen / 1024).toFixed(1)} KB / ${byteLen} bytes)\n`,
  );
}

buildConsoleHtml().catch((err) => {
  process.stderr.write(`build_console_app failed: ${err?.stack || err}\n`);
  process.exitCode = 1;
});
