/**
 * Unit tests for the pure document logic in static/font-doc.js.
 *
 * These cover the path-handling and glyph maths that the Python suites cannot
 * reach. They exist because of a real bug: inserting a point on a straight
 * segment produced a bare [x, y] pair with no on-curve flag, so the new point
 * was read back as an off-curve control point. The next split then thought it
 * was looking at a curve and the point count escalated +1, +3, +5. Nothing
 * server-side noticed until export, where validation rejected it.
 *
 * The canvas hit-testing bug that made insertion feel impossible (only clicks
 * within 8px of one of eleven samples worked) needs a browser and is covered by
 * manual/browser checks instead.
 *
 * Run:
 *     node tests/font_doc_test.mjs
 */

import {
  FontDocument,
  createDocument,
  createGlyph,
  drawsSomething,
  formatCodePoint,
  hasOwnPoints,
  isComposite,
  mat,
  parseCodePoint,
  stepCodePoint,
} from "../static/font-doc.js";

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
  check(
    actual === expected,
    name,
    actual === expected ? String(actual) : `expected ${expected}, got ${actual}`
  );
}

const near = (a, b, tol = 0.01) => Math.abs(a - b) <= tol;

// ---------------------------------------------------------------------------
// Point layout helpers. Every point must be a 6-element array: font_model.py
// validates point[2] as 0/1 and reads point[3] as the type.
// ---------------------------------------------------------------------------
const isWellFormed = (p) => Array.isArray(p) && p.length === 6 && (p[2] === 0 || p[2] === 1);
const onCurve = (p) => p[2] === 1;

function lerp(a, b, t) {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

/** Independent de Casteljau evaluation, used to prove the shape is preserved. */
function curveAt(points, t) {
  const pts = points.map((p) => [p[0], p[1]]);
  let level = pts;
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i < level.length - 1; i += 1) next.push(lerp(level[i], level[i + 1], t));
    level = next;
  }
  return level[0];
}

function makeDoc(points, quadratic = false) {
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("test", -1);
  // glyphName must be set before doc.glyph resolves to anything.
  doc.glyphName = "test";
  doc.glyph.contours = [{ closed: false, quadratic, points }];
  return doc;
}

const P = (x, y, on = 1) => [x, y, on, on ? 0 : 1, 0, null];

// ---------------------------------------------------------------------------
console.log("=".repeat(68));
console.log("Font Builder Studio - font-doc.js unit tests");
console.log("=".repeat(68));

// --- code point parsing ----------------------------------------------------
console.log("\n--- parseCodePoint ---");
equal(parseCodePoint("U+0041").code, 0x41, "U+0041 (hex notation)");
equal(parseCodePoint("0x41").code, 0x41, "0x41 (programmer hex)");
equal(parseCodePoint("41").code, 41, "bare digits are decimal, letter-free hex is not guessed");
equal(parseCodePoint("4a").code, 0x4a, "digits with a hex letter are hex");
equal(parseCodePoint("65").code, 65, "65 is decimal");
equal(parseCodePoint("").code, -1, "blank means unencoded");
equal(parseCodePoint("   ").code, -1, "whitespace-only means unencoded");
equal(parseCodePoint("zzz").ok, false, "garbage is rejected");
equal(parseCodePoint("U+110000").ok, false, "beyond U+10FFFF is rejected");
equal(parseCodePoint("2000000").ok, false, "beyond U+10FFFF in decimal is rejected");
equal(parseCodePoint("U+2603").code, 0x2603, "U+2603 (snowman)");

// --- code point formatting -------------------------------------------------
console.log("\n--- formatCodePoint ---");
equal(formatCodePoint(0xd0).hex, "U+00D0", "hex is padded to four digits");
equal(formatCodePoint(0xd0).dec, "208", "decimal value");
equal(formatCodePoint(0x1f600).hex, "U+1F600", "beyond the BMP is not truncated");
equal(formatCodePoint(-1).unencoded, true, "-1 formats as unencoded");

// --- stepping --------------------------------------------------------------
console.log("\n--- stepCodePoint ---");
equal(stepCodePoint(0x41, 1), 0x42, "steps up by one");
equal(stepCodePoint(0x41, -1), 0x40, "steps down by one");
equal(stepCodePoint(0xd7ff, 1), 0xe000, "skips the surrogate block going up");
equal(stepCodePoint(0xe000, -1), 0xd7ff, "skips the surrogate block going down");
equal(stepCodePoint(0x10ffff, 1), 0x10ffff, "clamps at the top instead of wrapping");
equal(stepCodePoint(0, -1), 0, "clamps at zero");
equal(stepCodePoint(-1, 1), 0x20, "unencoded starts at the first printable character");
equal(stepCodePoint(-1, -1), 0x10ffff, "unencoded stepping down starts at the top");

// --- insertion: a straight segment ----------------------------------------
console.log("\n--- insertPointOnSegment (line) ---");
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  const before = doc.glyph.contours[0].points.length;
  const ok = doc.insertPointOnSegment(0, 0, 0.5);
  const pts = doc.glyph.contours[0].points;
  const added = pts.find((p) => p[0] === 150 && p[1] === 0);

  check(ok === true, "returns true on success");
  equal(pts.length, before + 1, "a line split inserts exactly one point");
  check(pts.every(isWellFormed), "every point is a well-formed 6-element array");
  check(
    Boolean(added) && added[2] === 1 && added.length === 6,
    "the inserted point is on-curve and fully formed (a bare [x,y] reads as off-curve)",
    added ? `[${added}]` : "not found"
  );
  check(
    pts.every(onCurve),
    "splitting a straight segment leaves the contour all on-curve",
    `${pts.filter(onCurve).length} of ${pts.length}`
  );
  check(Boolean(added), "the new point sits at the requested position", added ? String(added) : "missing");
}

// --- insertion: a quadratic curve -----------------------------------------
console.log("\n--- insertPointOnSegment (quadratic) ---");
{
  const original = [P(0, 0), P(150, 200, 0), P(300, 0)];
  const doc = makeDoc(original.map((p) => [...p]), true);
  const t = 0.4;
  const expected = curveAt(original, t);
  doc.insertPointOnSegment(0, 0, t);
  const pts = doc.glyph.contours[0].points;

  equal(pts.length, 6, "a quadratic split inserts three points");
  check(pts.every(isWellFormed), "all points stay well-formed");
  equal(pts.filter(onCurve).length, 3, "exactly one on-curve point is added");

  const added = pts.filter(onCurve).find((p) => p !== pts[0]);
  check(
    added && near(added[0], expected[0], 0.001) && near(added[1], expected[1], 0.001),
    "the split point matches the original curve at t (shape preserved)",
    added ? `got ${added[0].toFixed(3)},${added[1].toFixed(3)} want ${expected[0].toFixed(3)},${expected[1].toFixed(3)}` : "missing"
  );
}

// --- insertion: a cubic curve ---------------------------------------------
console.log("\n--- insertPointOnSegment (cubic) ---");
{
  const original = [P(0, 0), P(0, 200, 0), P(300, 200, 0), P(300, 0)];
  const doc = makeDoc(original.map((p) => [...p]));
  const t = 0.25;
  const expected = curveAt(original, t);
  doc.insertPointOnSegment(0, 0, t);
  const pts = doc.glyph.contours[0].points;

  equal(pts.length, 9, "a cubic split inserts five points");
  check(pts.every(isWellFormed), "all points stay well-formed");
  equal(pts.filter(onCurve).length, 3, "exactly one on-curve point is added");

  const added = pts.filter(onCurve).find((p) => p !== pts[0]);
  check(
    added && near(added[0], expected[0], 0.001) && near(added[1], expected[1], 0.001),
    "the split point matches the original curve at t (shape preserved)",
    added ? `got ${added[0].toFixed(3)},${added[1].toFixed(3)} want ${expected[0].toFixed(3)},${expected[1].toFixed(3)}` : "missing"
  );
}

// --- repeated splits must not escalate ------------------------------------
console.log("\n--- repeated splits stay lines ---");
{
  const doc = makeDoc([P(0, 0), P(400, 0), P(400, 400)]);
  const sizes = [doc.glyph.contours[0].points.length];
  for (const t of [0.3, 0.6, 0.9]) {
    doc.insertPointOnSegment(0, 0, t);
    sizes.push(doc.glyph.contours[0].points.length);
  }
  equal(sizes.join(","), "3,4,5,6", "each split adds exactly one point (no +1/+3/+5 escalation)");
  check(doc.glyph.contours[0].points.every(isWellFormed), "still well-formed after repeated splits");
}

// --- the split parameter is honoured --------------------------------------
console.log("\n--- split position ---");
{
  const doc = makeDoc([P(0, 0), P(1000, 0)]);
  doc.insertPointOnSegment(0, 0, 0.9);
  const added = doc.glyph.contours[0].points[1];
  check(near(added[0], 900, 0.001), "splits where asked, not at the midpoint", `x=${added[0]}`);
}
{
  const doc = makeDoc([P(0, 0), P(1000, 0)]);
  doc.insertPointOnSegment(0, 0, 0); // clamped
  const added = doc.glyph.contours[0].points[1];
  check(added[0] > 0 && added[0] < 1000, "t=0 is clamped inside the segment", `x=${added[0]}`);
}

// --- undo ------------------------------------------------------------------
console.log("\n--- undo ---");
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  doc.insertPointOnSegment(0, 0, 0.5);
  equal(doc.glyph.contours[0].points.length, 4, "insert added a point");
  check(doc.canUndo(), "the insert is undoable");
  doc.undo();
  equal(doc.glyph.contours[0].points.length, 3, "undo removes the inserted point");
  doc.redo();
  equal(doc.glyph.contours[0].points.length, 4, "redo puts it back");
}

// --- coalesced history -----------------------------------------------------
// insertPointOnSegment takes its own snapshot (each insert is individually
// undoable). Coalescing exists for the code-point steppers, which mutate the
// glyph directly and would otherwise fill the history with one entry per click.
console.log("\n--- undo granularity ---");
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  doc.insertPointOnSegment(0, 0, 0.3);
  doc.insertPointOnSegment(0, 0, 0.6);
  equal(doc.glyph.contours[0].points.length, 5, "two inserts applied");
  doc.undo();
  equal(doc.glyph.contours[0].points.length, 4, "each double-click insert is its own undo step");
}

console.log("\n--- coalesced undo ---");
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  const start = doc.glyph.unicode;
  for (const code of [0x41, 0x42, 0x43]) {
    doc.beginCoalescedEdit("unicode-step");
    doc.glyph.unicode = code;
    doc.commitEdit("edit");
  }
  equal(doc.glyph.unicode, 0x43, "three coalesced steps applied");
  doc.undo();
  equal(doc.glyph.unicode, start, "one undo reverts the whole burst");
  equal(doc.canUndo(), false, "the burst left exactly one history entry");
}
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  doc.beginCoalescedEdit("a");
  doc.glyph.unicode = 0x41;
  doc.commitEdit("edit");
  doc.beginCoalescedEdit("b");
  doc.glyph.unicode = 0x42;
  doc.commitEdit("edit");
  doc.undo();
  equal(doc.glyph.unicode, 0x41, "a different tag starts a new history entry");
}

// --- composite glyphs ------------------------------------------------------
// Scaron-style glyphs own no points, so nothing about them is selectable until
// the references are unlinked. This is what "the glyph isn't coloured and not
// really clickable" turned out to be.
console.log("\n--- unlinkReferences ---");
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());

  doc.addGlyph("base", -1);
  doc.glyphName = "base";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(100, 0), P(100, 100)] }];

  doc.addGlyph("accent", -1);
  doc.glyphName = "accent";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(50, 0), P(50, 50)] }];

  doc.addGlyph("combo", -1);
  doc.glyphName = "combo";
  doc.glyph.references = [
    ["base", [1, 0, 0, 1, 0, 0]],
    ["accent", [1, 0, 0, 1, 200, 300]],
  ];

  check(isComposite(doc.glyph), "a reference-only glyph is composite");
  check(!hasOwnPoints(doc.glyph), "a composite glyph has no points of its own");
  check(drawsSomething(doc.glyph), "a composite glyph still draws something");

  const ok = doc.unlinkReferences();
  check(ok === true, "unlinkReferences reports success");
  equal(doc.glyph.contours.length, 2, "one contour is baked per reference");
  equal(doc.glyph.references.length, 0, "references are cleared");
  check(hasOwnPoints(doc.glyph), "the glyph now owns editable points");
  check(
    doc.glyph.contours.every((c) => c.points.every(isWellFormed)),
    "baked points stay well-formed"
  );

  // accent's second point is (50, 0); its matrix translates by (200, 300)
  const moved = doc.glyph.contours[1].points[1];
  check(
    near(moved[0], 250, 0.001) && near(moved[1], 300, 0.001),
    "the reference matrix is applied when baking",
    `[${moved.slice(0, 2)}]`
  );

  doc.undo();
  check(
    isComposite(doc.glyph) && !hasOwnPoints(doc.glyph),
    "undo restores the composite state"
  );
}

console.log("\n--- unlinkReferences edge cases ---");
{
  // nested: combo -> middle -> leaf
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("leaf", -1);
  doc.glyphName = "leaf";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(10, 0), P(10, 10)] }];
  doc.addGlyph("middle", -1);
  doc.glyphName = "middle";
  doc.glyph.references = [["leaf", [1, 0, 0, 1, 5, 5]]];
  doc.addGlyph("combo", -1);
  doc.glyphName = "combo";
  doc.glyph.references = [["middle", [1, 0, 0, 1, 100, 0]]];

  doc.unlinkReferences();
  equal(doc.glyph.contours.length, 1, "nested references resolve through to the leaf");
  const pt = doc.glyph.contours[0].points[1];
  check(near(pt[0], 115, 0.001), "both matrices are applied", `x=${pt[0]}`);
}
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("broken", -1);
  doc.glyphName = "broken";
  doc.glyph.references = [["does-not-exist", [1, 0, 0, 1, 0, 0]]];
  check(doc.unlinkReferences() === false, "a missing reference target is refused");
  check(isComposite(doc.glyph), "and the composite is left untouched");
}
{
  // a reference cycle must not hang the editor
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("a", -1);
  doc.glyphName = "a";
  doc.glyph.references = [["b", [1, 0, 0, 1, 0, 0]]];
  doc.addGlyph("b", -1);
  doc.glyphName = "b";
  doc.glyph.references = [["a", [1, 0, 0, 1, 0, 0]]];
  doc.glyphName = "a";
  check(doc.unlinkReferences() === false, "a reference cycle terminates instead of hanging");
}
{
  const doc = makeDoc([P(0, 0), P(300, 0), P(300, 300)]);
  check(doc.unlinkReferences() === false, "a glyph with no references is left alone");
}

// --- spacing: LSB, width, and the invariant that keeps export honest -------
console.log("\n--- setLeftSideBearing ---");
{
  // Mirrors the engine: setting LSB slides the outline and drags the width with
  // it, so the right sidebearing is preserved. Measured against FontForge first.
  const doc = makeDoc([P(100, 0), P(500, 700), P(900, 0)]);
  doc.glyph.width = 1000;
  doc.glyph.lsb = 100;

  const before = doc.spacing();
  check(before.lsb === 100 && before.inkWidth === 800, "spacing reports the measured values",
    `lsb=${before.lsb} ink=${before.inkWidth}`);
  check(before.rsb === 1000 - 100 - 800, "rsb = width - lsb - ink", `rsb=${before.rsb}`);

  doc.setLeftSideBearing(300);
  const after = doc.spacing();
  check(after.lsb === 300, "lsb is applied");
  check(doc.glyph.contours[0].points[0][0] === 300, "the outline slid with it",
    `x=${doc.glyph.contours[0].points[0][0]}`);
  check(after.width === 1200, "the advance width moved by the same amount", `width=${after.width}`);
  check(after.inkWidth === before.inkWidth, "the drawing itself is unchanged");
  check(after.rsb === before.rsb, "so the right sidebearing is preserved", `rsb=${after.rsb}`);

  doc.undo();
  check(doc.spacing().lsb === 100 && doc.glyph.width === 1000, "undo restores both numbers");
}
{
  const doc = makeDoc([P(0, 0)]);
  doc.glyph.width = 500;
  doc.setLeftSideBearing(-120);
  check(doc.spacing().lsb === -120, "a negative lsb is allowed (overhang)");
  check(doc.glyph.width === 380, "and still moves the width", `width=${doc.glyph.width}`);
}
{
  const doc = makeDoc([]);
  doc.glyph.contours = [];
  doc.setLeftSideBearing(40);
  check(doc.glyph.lsb === 40, "a glyph with no ink still stores the number");
  check(doc.spacing().empty === true, "and reports itself as having no ink");
}

console.log("\n--- syncSideBearing ---");
{
  // Dragging a point changes xMin; the stored lsb has to follow it or the build
  // slides the glyph back to where the stale value said it was.
  const doc = makeDoc([P(100, 0), P(500, 700), P(900, 0)]);
  doc.glyph.lsb = 100;
  doc.glyph.contours[0].points[0][0] = -75;
  doc.syncSideBearing();
  check(doc.glyph.lsb === -75, "lsb tracks the outline after an edit", `lsb=${doc.glyph.lsb}`);
}

console.log("\n--- typicalWidth ---");
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.doc.em = 1000;
  for (const [name, width] of [["a", 600], ["b", 600], ["c", 600], ["d", 900]]) {
    doc.addGlyph(name, -1);
    doc.glyphName = name;
    doc.glyph.width = width;
  }
  check(doc.typicalWidth() === 600, "the most common width wins", `${doc.typicalWidth()}`);
}
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.doc.em = 1000;
  check(doc.typicalWidth() === 1000, "an empty font falls back to the em");
}

// --- references move with a whole-glyph transform -------------------------
console.log("\n--- transform applies to references ---");
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("base", -1);
  doc.glyphName = "base";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(100, 0), P(100, 100)] }];
  doc.addGlyph("combo", -1);
  doc.glyphName = "combo";
  doc.glyph.contours = [];
  doc.glyph.references = [["base", [1, 0, 0, 1, 0, 0]]];

  const ok = doc.transform(mat.identity(), { dx: 50, dy: 20, aboutCenter: false });
  check(ok === true, "a glyph with only references can still be transformed");
  check(
    doc.glyph.references[0][1][4] === 50 && doc.glyph.references[0][1][5] === 20,
    "the reference translation moved",
    `[${doc.glyph.references[0][1].slice(4)}]`
  );
}

// --- decomposition templates ---------------------------------------------
console.log("\n--- templateParts ---");
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("S", 0x53);
  doc.glyphName = "S";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(100, 0), P(100, 100)] }];
  doc.addGlyph("caron", 0x2c7);
  doc.glyphName = "caron";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(40, 0), P(20, 40)] }];

  doc.addGlyph("Scaron", 0x160);
  doc.glyphName = "Scaron";
  const parts = doc.templateParts();
  check(Array.isArray(parts) && parts.length === 2, "S with caron decomposes into two parts",
    parts ? parts.map((p) => p.code.toString(16)).join(",") : "null");
  check(parts && parts[0].glyph && parts[0].glyph.name === "S", "the base is found by code point");
  check(
    parts && parts[1].glyph && parts[1].glyph.name === "caron",
    "the mark is found by name - U+02C7 has no compatibility decomposition",
    parts && parts[1].glyph ? parts[1].glyph.name : "missing"
  );
  check(parts && parts[1].combining === true, "the mark is flagged as combining");

  doc.glyphName = "S";
  check(doc.templateParts() === null, "a character that does not decompose has no template");
}

console.log("\n--- composeFromTemplate ---");
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("S", 0x53);
  doc.glyphName = "S";
  doc.glyph.width = 500;
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(500, 0), P(500, 700), P(0, 700)] }];
  doc.addGlyph("caron", 0x2c7);
  doc.glyphName = "caron";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(100, 0), P(50, 60)] }];
  doc.addGlyph("Scaron", 0x160);
  doc.glyphName = "Scaron";

  const result = doc.composeFromTemplate();
  check(result.ok === true, "compose succeeds");
  check(doc.glyph.references.length === 2, "one reference per part", `${doc.glyph.references.length}`);
  check(doc.glyph.contours.length === 0, "a composite owns no contours");
  check(
    doc.glyph.references.every((ref) => ref[0] !== "Scaron"),
    "and never references itself"
  );
  const mark = doc.glyph.references[1][1];
  check(mark[4] > 0 || mark[4] < 0, "the mark is offset from the base", `dx=${mark[4]} dy=${mark[5]}`);
  check(doc.glyph.width === 500, "it inherits the base's advance width", `${doc.glyph.width}`);

  doc.undo();
  check(!doc.glyph.references || doc.glyph.references.length === 0, "undo removes the composition");
}
{
  // a base with no spacing yet should not hand down a width of zero
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.doc.em = 1000;
  doc.addGlyph("S", 0x53);
  doc.glyphName = "S";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(500, 0), P(500, 700)] }];
  doc.addGlyph("caron", 0x2c7);
  doc.glyphName = "caron";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(100, 0), P(50, 60)] }];
  doc.addGlyph("Scaron", 0x160);
  doc.glyphName = "Scaron";
  doc.composeFromTemplate();
  check(doc.glyph.width === 1000, "an unspaced base falls back to the font's usual width", `${doc.glyph.width}`);
}
{
  // a part that is not in the font has to be reported, not silently skipped
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("S", 0x53);
  doc.glyphName = "S";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(500, 0), P(500, 700)] }];
  doc.addGlyph("Scaron", 0x160);
  doc.glyphName = "Scaron";
  const result = doc.composeFromTemplate();
  check(result.ok === false && result.missing === 0x30c, "a missing part is reported by code point",
    JSON.stringify(result));
}
{
  const doc = new FontDocument();
  doc.loadDocument(createDocument());
  doc.addGlyph("S", 0x53);
  doc.glyphName = "S";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0), P(500, 0), P(500, 700)] }];
  doc.addGlyph("Scaron", 0x160);
  doc.glyphName = "Scaron";
  // crafted parts that would make the glyph reference itself
  const parts = [
    { code: 0x53, glyph: doc.doc.glyphs.find((g) => g.name === "S") },
    { code: 0x30c, glyph: doc.glyph },
  ];
  const result = doc.composeFromTemplate(parts);
  check(result.ok === false && result.selfReference === true, "a self-reference is refused",
    JSON.stringify(result));
}

// --- marquee selection -----------------------------------------------------
console.log("\n--- selectInBox ---");
// A factory, not a shared constant: makeDoc stores the array it is handed by
// reference, so one transform test would otherwise mutate the fixture the next
// test builds on.
const square = () => [P(0, 0), P(100, 0), P(100, 100), P(0, 100)];
{
  const doc = makeDoc([P(0, 0), P(100, 0), P(100, 100), P(0, 100)]);
  equal(
    doc.selectInBox({ minX: -10, minY: -10, maxX: 50, maxY: 50 }),
    1,
    "only the point inside the box is caught"
  );
  check(doc.isSelected(0, 0), "the caught point is the one at the origin");
  check(!doc.isSelected(0, 1), "a point outside the box is left alone");

  equal(
    doc.selectInBox({ minX: -10, minY: -10, maxX: 110, maxY: 110 }),
    4,
    "a wider box replaces the previous selection"
  );

  const add = makeDoc(square());
  equal(
    add.selectInBox({ minX: -10, minY: -10, maxX: 50, maxY: 50 }),
    1,
    "a plain marquee replaces what was selected before"
  );
  equal(
    add.selectInBox({ minX: 90, minY: -10, maxX: 110, maxY: 10 }, { add: true }),
    2,
    "an additive marquee keeps what was already selected"
  );
  check(
    add.isSelected(0, 0) && add.isSelected(0, 1),
    "both the old point and the new one are held"
  );

  // The box edges are inclusive, so a marquee drawn exactly to a point catches it.
  const tight = makeDoc(square());
  equal(
    tight.selectInBox({ minX: 0, minY: 0, maxX: 100, maxY: 100 }),
    4,
    "the box edge counts as inside"
  );
  equal(
    tight.selectInBox({ minX: 0, minY: 0, maxX: 99.9, maxY: 99.9 }),
    1,
    "shrinking the box by a hair drops the far corners"
  );

  // A click with no drag is a zero-area box: it must clear, not throw.
  equal(
    tight.selectInBox({ minX: 5, minY: 5, maxX: 5, maxY: 5 }),
    0,
    "a zero-area marquee selects nothing"
  );
}

// --- select all / bounds ---------------------------------------------------
console.log("\n--- selectAll and selectionBounds ---");
{
  const doc = makeDoc([P(0, 0), P(10, 0)]);
  doc.glyph.contours.push({ closed: true, quadratic: false, points: [P(20, 20), P(30, 30)] });
  equal(doc.selectAll(), 4, "selectAll reaches every contour");
  check(doc.isSelected(1, 1), "including points on the second contour");

  const box = doc.selectionBounds();
  check(
    box && box.minX === 0 && box.minY === 0 && box.maxX === 30 && box.maxY === 30,
    "the selection's bounds cover all of it",
    JSON.stringify(box)
  );

  doc.clearSelection();
  equal(doc.selectionBounds(), null, "an empty selection has no bounds");
  equal(doc.selectAll(), 4, "selectAll can be called again");
  doc.setSelection(0, 0);
  const single = doc.selectionBounds();
  check(
    single && single.minX === 0 && single.maxX === 0 && single.minY === 0 && single.maxY === 0,
    "one point collapses its bounds onto itself"
  );
}

// --- clipboard: copy -------------------------------------------------------
console.log("\n--- copySelection ---");
{
  const doc = makeDoc([P(0, 0), P(100, 0), P(100, 100), P(0, 100)]);
  equal(doc.copySelection(), null, "copying with nothing selected yields null");

  doc.glyph.contours.push({ closed: true, quadratic: false, points: [P(200, 200), P(300, 200)] });
  doc.selectAll();
  const payload = doc.copySelection();
  equal(payload.fromGlyph, "test", "the payload records where it came from");
  equal(payload.groups.length, 2, "points stay grouped per contour");
  equal(payload.groups[0].length, 4, "the first group holds its contour's points");
  equal(payload.groups[1].length, 2, "the second group holds the other contour's points");

  // Undo the click order: selecting 2 then 0 must still copy 0 before 2, or the
  // pasted contour would walk its points out of order.
  doc.clearSelection();
  doc.setSelection(0, 2);
  doc.toggleSelection(0, 0);
  const ordered = doc.copySelection();
  equal(ordered.groups[0].length, 2, "the out-of-order pair copied");
  check(
    ordered.groups[0][0][0] === 0 && ordered.groups[0][1][0] === 100,
    "the clipboard restores contour order, not click order",
    JSON.stringify(ordered.groups[0].map((p) => p[0]))
  );

  // The clipboard must not alias the document, or a later edit would silently
  // change what gets pasted.
  doc.glyph.contours[0].points[0][0] = 9999;
  equal(ordered.groups[0][0][0], 0, "the clipboard is a copy, not a live reference");
  doc.glyph.contours[0].points[0][0] = 0;
}

// --- clipboard: paste ------------------------------------------------------
console.log("\n--- pastePoints ---");
{
  const doc = makeDoc([P(0, 0), P(100, 0), P(100, 100), P(0, 100)]);
  doc.selectAll();
  const payload = doc.copySelection();
  const before = doc.glyph.contours.length;

  const result = doc.pastePoints(payload);
  equal(doc.glyph.contours.length, before + 1, "paste adds exactly one contour");
  equal(result.contours, 1, "the result reports one contour");
  equal(result.points, 4, "and four points");
  equal(result.skipped, 0, "nothing was skipped");
  equal(
    doc.glyph.contours[before].points[0][0],
    20,
    "a paste back onto its own glyph is offset so it is visible"
  );
  check(doc.glyph.contours[before].closed === true, "the pasted contour is closed");
  check(doc.isSelected(before, 0), "the pasted points become the selection");

  doc.undo();
  equal(doc.glyph.contours.length, before, "one undo removes the whole paste");
}

{
  // Across glyphs the original position is the point of a paste, so no offset.
  const doc = makeDoc([P(0, 0), P(100, 0), P(100, 100)]);
  doc.selectAll();
  const payload = doc.copySelection();
  doc.addGlyph("other", -1);
  doc.glyphName = "other";
  doc.glyph.contours = [{ closed: true, quadratic: false, points: [P(0, 0)] }];

  const result = doc.pastePoints(payload);
  equal(result.points, 3, "a paste into another glyph carries the points across");
  equal(doc.glyph.contours[1].points[0][0], 0, "and keeps their original position");
  check(doc.glyph.contours[1].points.every(isWellFormed), "the pasted points are well formed");
}

{
  // font_model.py requires >= 2 points per contour, so a smaller fragment is
  // skipped rather than producing a document export would reject.
  const doc = makeDoc([P(0, 0), P(100, 0), P(100, 100)]);
  doc.setSelection(0, 0);
  equal(doc.pastePoints(doc.copySelection()), null, "a one-point fragment is refused");

  doc.glyph.contours.push({ closed: false, quadratic: false, points: [P(500, 500)] });
  doc.selectAll();
  const mixed = doc.pastePoints(doc.copySelection());
  equal(mixed.contours, 1, "only the complete contour is pasted");
  equal(mixed.skipped, 1, "the fragment is reported as skipped");
  equal(doc.glyph.contours.length, 3, "and no contour is created for it");
}

// --- flip / rotate the selection -------------------------------------------
console.log("\n--- flip and rotate ---");
{
  const doc = makeDoc(square());
  doc.selectAll();
  doc.transform(mat.flipH());
  const mirror = doc.glyph.contours[0].points;
  check(near(mirror[0][0], 100), "flip H mirrors within the selection", `x=${mirror[0][0]}`);
  equal(mirror[0][1], 0, "flip H leaves y alone");
  check(mirror.every(isWellFormed), "flipped points stay well formed");
}

{
  const doc = makeDoc(square());
  doc.selectAll();
  doc.transform(mat.rotate(90));
  const p = doc.glyph.contours[0].points[0];
  check(
    near(p[0], 100) && near(p[1], 0),
    "rotate 90 maps the origin corner onto (100, 0) about the selection centre",
    `${p[0]}, ${p[1]}`
  );
}

{
  // With nothing selected a transform falls back to the whole glyph, which is
  // what the toolbar's Flip and Rotate buttons advertise.
  const doc = makeDoc([P(0, 0), P(400, 0), P(400, 700)]);
  doc.clearSelection();
  check(doc.transform(mat.flipH()), "a transform with no selection still applies");
  equal(
    doc.glyph.contours[0].points[0][0],
    400,
    "and flips the whole glyph about its own centre"
  );
}

// ---------------------------------------------------------------------------
console.log("-".repeat(68));
console.log(`${passed}/${passed + failures.length} checks passed`);
if (failures.length) {
  console.log(`\nFONT-DOC UNIT TESTS FAILED: ${failures.join(", ")}`);
  process.exit(1);
}
console.log("\nFONT-DOC UNIT TESTS PASSED");
