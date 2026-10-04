/**
 * Static backend: the same surface as `api-manager.js`, with no server behind it.
 *
 * The editor was already client-side, so the only things that actually needed a
 * server were: parsing a font, writing a font, running FontForge operations,
 * storing projects, accounts, and the Unicode index. Of those, parsing and the
 * index have honest in-browser replacements; the rest are unavailable and say so
 * rather than failing obscurely.
 *
 * Returning **empty** export/operation lists is deliberate: the UI reads those
 * to decide whether to disable its panels, so this is how the static build
 * communicates "not here" to the rest of the app.
 */

import { createDocument, documentStats } from "./font-doc.js";
import { parseFontBuffer, STATIC_PARSE_LIMIT } from "./font-parser.js";
import * as unicodeData from "./unicode-static.js";

const NO_WRITER =
  "Writing a font file needs the server build - the browser cannot generate font binaries yet.";
const NO_FONTFORGE = "Cleanup operations run inside FontForge, so they need the server build.";
const NO_PROJECTS = "Projects are stored by the server; this build keeps everything in the page.";

// --- auth: a static site has no accounts ----------------------------------
export function onUnauthorized() {}
export function setAuthToken() {}
export function getAuthToken() {
  return null;
}

// --- capabilities ----------------------------------------------------------
export async function getFormats() {
  return {
    ok: true,
    export_formats: [],
    // What the in-browser parser can actually read.
    import_extensions: [".ttf", ".otf"],
    mimetypes: { ttf: "font/ttf", otf: "font/otf" },
    operations: [],
    unicode_picker: unicodeData.meta(),
    limits: {
      max_upload_bytes: STATIC_PARSE_LIMIT,
      max_glyphs: 65535,
      max_points_total: 4000000,
      max_points_per_contour: 8192,
      max_contours_per_glyph: 4096,
    },
  };
}

// --- documents -------------------------------------------------------------
export async function newFont(options = {}) {
  const document = createDocument({
    em: options.em || 1000,
    ascent: options.ascent ?? Math.round((options.em || 1000) * 0.8),
    descent: options.descent ?? Math.round((options.em || 1000) * 0.2),
    family_name: options.family_name || "Untitled",
    style_name: options.style_name || "Regular",
  });
  document.font_name = `${document.family_name.replace(/ /g, "")}-${document.style_name.replace(/ /g, "")}`;
  return { ok: true, document, stats: documentStats(document) };
}

/**
 * Parse a font file in the browser.
 *
 * Matches the HTTP contract exactly - `{document, stats}` - so the caller does
 * not care which build it is running in.
 */
export async function parseFont(file) {
  const buffer = await file.arrayBuffer();
  const fallbackName = String(file.name || "font").replace(/\.[^.]+$/, "");
  return parseFontBuffer(buffer, { familyName: fallbackName });
}

export async function buildFont() {
  throw new Error(NO_WRITER);
}

export async function runOperation() {
  throw new Error(NO_FONTFORGE);
}

// --- projects: no server, nothing to list --------------------------------
export async function listProjects() {
  return { ok: true, projects: [] };
}
export async function createProject() {
  throw new Error(NO_PROJECTS);
}
export async function getProject() {
  throw new Error(NO_PROJECTS);
}
export async function updateProject() {
  throw new Error(NO_PROJECTS);
}
export async function deleteProject() {
  throw new Error(NO_PROJECTS);
}

// --- Unicode picker: same shapes as the HTTP endpoints --------------------
export async function getUnicodeBlocks() {
  await unicodeData.loadIndex();
  return { ok: true, meta: unicodeData.meta(), blocks: unicodeData.blocks() };
}

export async function getUnicodeChars(block, offset = 0, limit = 256) {
  await unicodeData.loadIndex();
  const page = unicodeData.chars(block, offset, limit);
  if (!page) throw new Error(`unknown block index ${block}`);
  return { ok: true, ...page };
}

export async function searchUnicode(text, limit = 256) {
  await unicodeData.loadIndex();
  return { ok: true, ...unicodeData.search(text, limit) };
}

export async function lookupUnicode(code) {
  await unicodeData.loadIndex();
  const value = typeof code === "string" ? unicodeData.parseCodePoint(code) : code;
  if (value === null || value === undefined) throw new Error("invalid code point");
  const found = unicodeData.lookup(value);
  if (!found) {
    return { ok: true, code: value, name: "", glyph_name: "", named: false };
  }
  return { ok: true, named: true, ...found };
}
