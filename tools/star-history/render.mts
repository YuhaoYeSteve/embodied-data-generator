/**
 * Render sanitized local samples with the unmodified official Star History renderer.
 * The external source checkout is pinned; this adapter never requests GitHub data.
 * See THIRD_PARTY_NOTICES.md for the renderer and embedded font licenses.
 */
import { execFileSync } from "node:child_process";
import { lstatSync, mkdirSync, readFileSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { JSDOM } from "jsdom";

const UPSTREAM_COMMIT = "608d0e39be441665c01cd6ac1fd01ca5ec1eaf39";
const SVG_NS = "http://www.w3.org/2000/svg";

// Daily samples are UTC dates; keep D3 tick labels independent of the host timezone.
process.env.TZ = "UTC";

type Sample = { date: string; stars: number };
type History = { repository: string; samples: Sample[] };

function readHistory(filename: string): History {
  const history = JSON.parse(readFileSync(filename, "utf8"));
  if (typeof history.repository !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(history.repository)) {
    throw new Error("History repository must use owner/name format");
  }
  if (!Array.isArray(history.samples) || history.samples.length === 0) {
    throw new Error("History must contain at least one observed sample");
  }
  let previous = "";
  for (const sample of history.samples) {
    if (!sample || typeof sample.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(sample.date)) {
      throw new Error("Sample date must use YYYY-MM-DD format");
    }
    const date = new Date(`${sample.date}T00:00:00.000Z`);
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== sample.date || sample.date <= previous) {
      throw new Error("Sample dates must be valid, unique, and in ascending order");
    }
    if (!Number.isSafeInteger(sample.stars) || sample.stars < 0) {
      throw new Error("Sample stars must be a non-negative integer");
    }
    previous = sample.date;
  }
  return history;
}

function prepareUpstream(upstream: string): void {
  const revision = execFileSync("git", ["-C", upstream, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  if (revision !== UPSTREAM_COMMIT) {
    throw new Error(`Official Star History checkout must be pinned to ${UPSTREAM_COMMIT}`);
  }
  execFileSync("git", ["-C", upstream, "diff", "--quiet", "HEAD", "--", "shared/packages", "backend/utils.ts"]);
  const dependencies = realpathSync(join(dirname(fileURLToPath(import.meta.url)), "node_modules"));
  const link = join(upstream, "node_modules");
  if (lstatSync(link, { throwIfNoEntry: false })) {
    if (realpathSync(link) !== dependencies) {
      throw new Error("Upstream node_modules already exists; use a clean scratch checkout");
    }
  } else {
    // Only the disposable upstream checkout receives this dependency-resolution link.
    symlinkSync(dependencies, link, "dir");
  }
}

async function main(): Promise<void> {
  const { values } = parseArgs({
    options: {
      upstream: { type: "string" },
      history: { type: "string" },
      output: { type: "string" },
    },
    strict: true,
    allowPositionals: false,
  });
  if (!values.upstream || !values.history || !values.output) {
    throw new Error("Usage: render.mts --upstream DIR --history FILE --output DIR");
  }
  const upstream = resolve(values.upstream);
  const history = readHistory(resolve(values.history));
  prepareUpstream(upstream);
  const { default: XYChart } = await import(pathToFileURL(join(upstream, "shared/packages/xy-chart.tsx")).href);
  const { fixJsdomSvgCasing } = await import(pathToFileURL(join(upstream, "backend/utils.ts")).href);
  const last = history.samples.at(-1)!;
  const outputs = new Map<string, string>();

  for (const theme of ["light", "dark"] as const) {
    const dom = new JSDOM("<!DOCTYPE html><body></body>");
    const document = dom.window.document;
    const svg = document.createElementNS(SVG_NS, "svg");
    document.body.append(svg);
    svg.setAttribute("width", "1000");
    svg.setAttribute("xmlns", SVG_NS);
    XYChart(svg, {
      title: "Star History",
      xLabel: "Date",
      yLabel: "GitHub Stars",
      data: {
        datasets: [{
          label: history.repository,
          logo: "",
          data: history.samples.map(sample => ({ x: new Date(`${sample.date}T00:00:00.000Z`), y: sample.stars })),
        }],
      },
      // The official dot option makes a single observed point visible, including zero.
      showDots: history.samples.length === 1,
      transparent: false,
      theme,
    }, {
      envType: "node",
      xTickLabelType: "Date",
      chartWidth: 1000,
      legendPosition: "top-left",
      dotSize: 1,
    });
    const title = document.createElementNS(SVG_NS, "title");
    title.setAttribute("id", "chart-title");
    title.textContent = `Star History — ${history.repository}`;
    const description = document.createElementNS(SVG_NS, "desc");
    description.setAttribute("id", "chart-description");
    description.textContent = `Daily snapshots from GitHub API, UTC. ${history.samples.length} observed sample${history.samples.length === 1 ? "" : "s"}. Latest: ${last.stars} stars on ${last.date}. No earlier history is inferred. Rendered with official Star History ${UPSTREAM_COMMIT}.`;
    svg.prepend(title, description);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-labelledby", "chart-title chart-description");
    const source = fixJsdomSvgCasing(svg.outerHTML);
    // Inspect geometry rather than arbitrary base64 font/image bytes: the
    // official watermark contains the literal letters "NaN" in valid PNG data.
    const geometry = ["x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "transform", "viewBox"];
    for (const element of [svg, ...svg.querySelectorAll("*")]) {
      for (const name of geometry) {
        if (/NaN|Infinity|undefined/.test(element.getAttribute(name) || "")) {
          throw new Error(`Official renderer produced invalid SVG geometry: ${name}`);
        }
      }
    }
    outputs.set(theme === "dark" ? "star-history-dark.svg" : "star-history.svg", `${source}\n`);
    dom.window.close();
  }
  const output = resolve(values.output);
  mkdirSync(output, { recursive: true });
  for (const [filename, source] of outputs) {
    writeFileSync(join(output, filename), source);
  }
  console.log(`Rendered official Star History in light and dark themes from ${history.samples.length} observed sample(s).`);
}

main().catch(error => {
  console.error(`Star History rendering failed: ${error.message}`);
  process.exitCode = 1;
});
