/**
 * Unit tests for the static build's Unicode lookup (static/unicode-static.js).
 *
 * This module is a JavaScript port of the query logic in unicode_data.py, so the
 * useful question is not "does it run" but "does it agree with the Python side".
 * The expectations below are deliberately the same ones tests/api_smoke.py makes
 * against the server, so both builds are held to one set of facts.
 *
 * The index is served over a throwaway HTTP server rather than read from disk,
 * because Node's fetch does not accept file:// URLs and the point is to exercise
 * the real fetch path anyway.
 *
 * Run:
 *     python App/tools/build_static.py            # produces _site
 *     node App/tests/unicode_static_test.mjs
 */

import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import * as unicode from "../static/unicode-static.js";

const CANDIDATES = [
  new URL("../../_site/static/unicode-index.json", import.meta.url),
  new URL("../resources/unicode-index.json", import.meta.url),
];

let passed = 0;
const failures = [];

function check(ok, name, detail = "") {
  if (ok) {
    passed += 1;
    console.log(`[ok] ${name}${detail ? ` - ${detail}` : ""}`);
  } else {
    failures.push(name);
    console.log(`[XX] ${name}${detail ? ` - ${detail}` : ""}`);
  }
}

function equal(actual, expected, name) {
  check(actual === expected, name, actual === expected ? String(actual) : `expected ${expected}, got ${actual}`);
}

console.log("=".repeat(68));
console.log("Font Builder Studio - static Unicode lookup tests");
console.log("=".repeat(68));

const indexPath = CANDIDATES.find((candidate) => existsSync(candidate));
if (!indexPath) {
  console.log("\nNo unicode index found. Build the static site first:");
  console.log("  python App/tools/build_static.py");
  process.exit(0);
}

// --- pure helpers, no index needed ----------------------------------------
console.log("\n--- parseCodePoint (matches the Python side) ---");
equal(unicode.parseCodePoint("U+0041"), 0x41, "U+0041");
equal(unicode.parseCodePoint("0x41"), 0x41, "0x41");
equal(unicode.parseCodePoint("41"), 41, "bare digits are decimal");
equal(unicode.parseCodePoint("65"), 65, "65 is decimal");
equal(unicode.parseCodePoint(""), null, "blank is not a code point");
equal(unicode.parseCodePoint("zzz"), null, "garbage is rejected");
equal(unicode.parseCodePoint("U+110000"), null, "beyond U+10FFFF is rejected");
equal(unicode.parseCodePoint("2000000"), null, "out of range in decimal is rejected");

// --- load through a real fetch --------------------------------------------
const payload = readFileSync(indexPath);
const server = createServer((request, response) => {
  response.writeHead(200, { "Content-Type": "application/json" });
  response.end(payload);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const url = `http://127.0.0.1:${server.address().port}/unicode-index.json`;

console.log("\n--- index load ---");
const loaded = await unicode.loadIndex(url);
check(loaded !== null, "the index loads over fetch", loaded ? "ok" : unicode.meta().count);
if (!loaded) {
  server.close();
  console.log("\nSTATIC UNICODE TESTS FAILED: index did not load");
  process.exit(1);
}

const meta = unicode.meta();
equal(meta.count, 149186, "code point count matches the Python index");
equal(meta.nameslist, "15.0.0", "nameslist version is carried through");
equal(meta.block_count, 327, "block count matches");

// --- blocks and pagination ------------------------------------------------
console.log("\n--- blocks and chars ---");
const blocks = unicode.blocks();
equal(blocks.length, 327, "every block is listed");
const latin = blocks.find((b) => b.name === "Basic Latin");
check(Boolean(latin) && latin.count === 95, "Basic Latin has 95 characters", latin ? `${latin.count}` : "missing");

const first = unicode.chars(latin.index, 0, 5);
equal(first.total, 95, "the block reports its full size");
equal(first.chars.length, 5, "a page is limited to the requested size");
equal(first.chars[0].code, 32, "the first Basic Latin character is SPACE");
equal(first.chars[0].name, "SPACE", "and it is named");
equal(first.truncated, true, "a truncated page says so");

const tail = unicode.chars(latin.index, 93, 10);
equal(tail.chars.length, 2, "the last page returns the remainder");
equal(tail.truncated, false, "the final page is not truncated");
equal(unicode.chars(99999, 0, 5), null, "an unknown block returns null");

// --- search behaviour, same expectations as api_smoke.py -----------------
console.log("\n--- search ---");
const snowman = unicode.search("snowman", 8);
equal(snowman.results[0].code, 0x2603, "search by name finds SNOWMAN first");
equal(snowman.results[0].name, "SNOWMAN", "with the official name");

for (const [notation, label] of [
  ["U+2603", "U+ form"],
  ["0x2603", "0x form"],
  ["9731", "decimal"],
]) {
  const found = unicode.search(notation, 8);
  check(
    found.results.some((r) => r.code === 0x2603),
    `search by code point (${label})`,
    `${notation} -> ${found.results.slice(0, 3).map((r) => r.code)}`
  );
}

const greek = unicode.search("greek capital omega", 8);
check(
  greek.results.some((r) => r.code === 0x03a9),
  "search matches when words are skipped",
  `'greek capital omega' -> ${greek.results.slice(0, 3).map((r) => r.code)}`
);

const nonsense = unicode.search("zzzznope", 8);
equal(nonsense.total, 0, "nonsense returns nothing");
equal(unicode.search("", 8).total, 0, "an empty query returns nothing");

// --- lookup ---------------------------------------------------------------
console.log("\n--- lookup ---");
const snow = unicode.lookup(0x2603);
equal(snow.name, "SNOWMAN", "lookup returns the official name");
equal(snow.glyph_name, "uni2603", "and the FontForge short name");
equal(unicode.lookup(0xe9).name, "LATIN SMALL LETTER E WITH ACUTE", "lookup of eacute");
equal(unicode.lookup(0x378), null, "an unassigned code point has no entry");

server.close();

// ---------------------------------------------------------------------------
console.log("-".repeat(68));
console.log(`${passed}/${passed + failures.length} checks passed`);
if (failures.length) {
  console.log(`\nSTATIC UNICODE TESTS FAILED: ${failures.join(", ")}`);
  process.exit(1);
}
console.log("\nSTATIC UNICODE TESTS PASSED");
