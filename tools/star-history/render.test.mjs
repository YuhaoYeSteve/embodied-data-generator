import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { JSDOM } from "jsdom";

const here = dirname(fileURLToPath(import.meta.url));
const upstream = process.env.STAR_HISTORY_UPSTREAM;
assert.ok(upstream, "Set STAR_HISTORY_UPSTREAM to the pinned official source checkout");

function generate(samples, callback) {
  const dir = mkdtempSync(join(tmpdir(), "official-star-history-test-"));
  try {
    const input = join(dir, "history.json");
    const output = join(dir, "output");
    writeFileSync(input, JSON.stringify({
      repository: "example/embodied-data-generator",
      sampling: "Daily UTC snapshots; no data before the first observation.",
      samples,
    }));
    const result = spawnSync(process.execPath, [
      join(here, "node_modules/tsx/dist/cli.mjs"),
      join(here, "render.mts"),
      "--upstream", resolve(upstream),
      "--history", input,
      "--output", output,
    ], {
      encoding: "utf8",
      timeout: 30_000,
      env: { PATH: process.env.PATH, TMPDIR: tmpdir() },
    });
    callback(result, output);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function readSVG(output, dark = false) {
  const source = readFileSync(join(output, dark ? "star-history-dark.svg" : "star-history.svg"), "utf8");
  const document = new JSDOM(source, { contentType: "image/svg+xml" }).window.document;
  assert.equal(document.querySelectorAll("parsererror").length, 0);
  assert.equal(document.documentElement.localName, "svg");
  const geometry = ["x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "transform", "viewBox"];
  for (const element of document.querySelectorAll("*")) {
    for (const name of geometry) {
      assert.doesNotMatch(element.getAttribute(name) || "", /NaN|Infinity|undefined/);
    }
  }
  assert.match(document.documentElement.getAttribute("style"), /font-family: xkcd/);
  assert.ok(document.querySelector("feTurbulence"), "Official xkcd filter is preserved");
  assert.match(document.querySelector("defs style").textContent, /data:.*base64/);
  assert.match(document.documentElement.textContent, /star-history\.com/);
  assert.match(document.documentElement.textContent, /Star History/);
  assert.equal(document.documentElement.style.backgroundColor, dark ? "rgb(13, 17, 23)" : "white");
  for (const element of document.querySelectorAll("[href]")) {
    assert.ok(!element.getAttribute("href") || element.getAttribute("href").startsWith("data:"), "Generated image stays self-contained");
  }
  return document;
}

test("official renderer keeps a single zero sample visible in both themes without invented history", () => {
  generate([{ date: "2026-10-07", stars: 0 }], (result, output) => {
    assert.equal(result.status, 0, result.stderr);
    for (const dark of [false, true]) {
      const document = readSVG(output, dark);
      const points = document.querySelectorAll(".chart-tooltip-dot");
      assert.equal(points.length, 1);
      assert.ok(Number(points[0].getAttribute("r")) > 0);
      assert.match(document.querySelector("desc").textContent, /0 stars on 2026-10-07/);
      assert.match(document.querySelector("desc").textContent, /1 observed sample/);
    }
  });
});

test("official renderer supports real multi-day data with a readable growth path", () => {
  generate([
    { date: "2026-10-07", stars: 0 },
    { date: "2026-10-08", stars: 3 },
    { date: "2026-10-09", stars: 2 },
  ], (result, output) => {
    assert.equal(result.status, 0, result.stderr);
    const document = readSVG(output);
    assert.match(document.querySelector(".xkcd-chart-xyline").getAttribute("d"), /^M.*[LC]/);
    assert.match(document.querySelector("desc").textContent, /2 stars on 2026-10-09/);
    assert.match(document.querySelector("desc").textContent, /3 observed samples/);
    assert.equal(document.querySelectorAll(".chart-tooltip-dot").length, 0);
  });
});

test("invalid counts fail before writing any chart", () => {
  generate([{ date: "2026-10-07", stars: -1 }], (result, output) => {
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /non-negative integer/);
    assert.equal(existsSync(join(output, "star-history.svg")), false);
  });
});
