/**
 * Unit tests for the outline geometry in static/glyph-outline.js.
 *
 * `buildSegments` was moved out of editor-canvas.js so the editor and the
 * character picker's thumbnails share one reading of FontForge's point rules.
 * That move is exactly why these tests exist: the function used to be reachable
 * only through a canvas, so nothing checked it, and the quadratic case - where
 * two consecutive off-curve points imply an on-curve point at their midpoint -
 * is the kind of rule that can be subtly wrong for years without anyone
 * noticing until outlines render differently in two places.
 *
 * The drawing half of the module needs a real canvas and is covered by the
 * browser checks instead.
 *
 * Run:
 *     node App/tests/glyph_outline_test.mjs
 */

import { boundsOfContours, buildSegments, hit } from "../static/glyph-outline.js";

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

const near = (a, b, tol = 0.01) => Math.abs(a - b) <= tol;

const ON = (x, y) => [x, y, 1, 0, 0, null];
const OFF = (x, y) => [x, y, 0, 1, 0, null];

console.log("=".repeat(68));
console.log("Font Builder Studio - glyph-outline.js unit tests");
console.log("=".repeat(68));

// --- on-curve test ---------------------------------------------------------
console.log("\n--- hit ---");
check(hit([0, 0, 1, 0, 0, null]), "1 means on-curve");
check(!hit([0, 0, 0, 1, 0, null]), "0 means off-curve");
check(hit([0, 0, true, 0, 0, null]), "a JSON boolean is accepted too");
check(!hit([0, 0, false, 0, 0, null]), "and false is off-curve");

// --- straight lines --------------------------------------------------------
console.log("\n--- line contours ---");
{
  const square = {
    closed: true,
    quadratic: false,
    points: [ON(0, 0), ON(100, 0), ON(100, 100), ON(0, 100)],
  };
  const segs = buildSegments(square);
  equal(segs.length, 4, "a closed square is four segments");
  check(segs.every((s) => s.kind === "line"), "all of them are lines");
  equal(segs[3].p1[0], 0, "the last one closes back to the start");
  equal(segs[3].p1[1], 0, "in both axes");

  const open = { ...square, closed: false };
  equal(buildSegments(open).length, 3, "an open contour is not closed off");
}

// --- cubic -----------------------------------------------------------------
console.log("\n--- cubic ---");
{
  const withTwo = {
    closed: false,
    quadratic: false,
    points: [ON(0, 0), OFF(10, 10), OFF(20, 20), ON(100, 0)],
  };
  const segs = buildSegments(withTwo);
  equal(segs.length, 1, "two off-curve points make one cubic");
  equal(segs[0].kind, "cubic", "and it is a cubic");
  check(segs[0].c1 === withTwo.points[1], "the first control point is used");
  check(segs[0].c2 === withTwo.points[2], "and the second");

  const withOne = { closed: false, quadratic: false, points: [ON(0, 0), OFF(50, 50), ON(100, 0)] };
  const lone = buildSegments(withOne)[0];
  equal(lone.kind, "cubic", "a lone off-curve is still a cubic in a cubic contour");
  check(lone.c1 === lone.c2, "with both controls on the same point");
}

// --- quadratic implied midpoints -------------------------------------------
console.log("\n--- quadratic implied midpoints ---");
{
  // The case that makes this function worth testing: in a quadratic contour two
  // consecutive off-curve points mean an on-curve point sits between them, which
  // is not written down anywhere in the data.
  const quad = {
    closed: false,
    quadratic: true,
    points: [ON(0, 0), OFF(50, 100), OFF(150, 100), ON(200, 0)],
  };
  const segs = buildSegments(quad);
  equal(segs.length, 2, "the run splits into two quadratics");
  check(segs.every((s) => s.kind === "quad"), "both are quadratics");
  check(
    near(segs[0].p1[0], 100) && near(segs[0].p1[1], 100),
    "the implied on-curve point is the midpoint of the two off-curve points",
    `${segs[0].p1[0]}, ${segs[0].p1[1]}`
  );
  check(
    near(segs[1].p0[0], 100) && near(segs[1].p0[1], 100),
    "and the next segment continues from it"
  );
  equal(segs[0].c[0], 50, "the first segment keeps the first off-curve point");
  equal(segs[1].c[0], 150, "the second keeps the second");

  // startOrig indexes the contour's own array; an implied point has no index.
  equal(segs[0].startOrig, 0, "the first start is a real point, so it has an index");
  equal(segs[1].startOrig, null, "an implied start has none");
  equal(segs[1].endOrig, 3, "the end is the real on-curve point");

  // A quadratic contour with a single off-curve point needs no implication.
  const simple = { closed: false, quadratic: true, points: [ON(0, 0), OFF(50, 50), ON(100, 0)] };
  const one = buildSegments(simple);
  equal(one.length, 1, "one off-curve in a quadratic contour stays one segment");
  equal(one[0].kind, "quad", "and is a quadratic");
  check(one[0].c === simple.points[1], "using that off-curve point");
}

// --- rotation --------------------------------------------------------------
console.log("\n--- contours that do not start on-curve ---");
{
  const rotated = { closed: false, quadratic: false, points: [OFF(50, 50), ON(0, 0), ON(100, 0)] };
  const segs = buildSegments(rotated);
  equal(segs.length, 1, "the walk starts at the first on-curve point");
  equal(segs[0].p0[0], 0, "so the segment starts there");
  equal(segs[0].startOrig, 1, "and reports its real index");
}

// --- degenerate input ------------------------------------------------------
console.log("\n--- degenerate contours ---");
equal(buildSegments({ points: [] }).length, 0, "no points, no segments");
equal(buildSegments({ points: [ON(0, 0)] }).length, 0, "one point, no segments");
equal(
  buildSegments({ closed: true, quadratic: false, points: [OFF(0, 0), OFF(10, 10)] }).length,
  0,
  "no on-curve point at all, no segments"
);
equal(buildSegments({}).length, 0, "a missing points array is handled");

// --- bounds ----------------------------------------------------------------
console.log("\n--- boundsOfContours ---");
{
  const box = boundsOfContours([
    { points: [ON(0, 0), ON(100, 0), ON(100, 100), ON(0, 100)] },
    { points: [ON(-20, 40), ON(50, 60)] },
  ]);
  check(
    box.minX === -20 && box.minY === 0 && box.maxX === 100 && box.maxY === 100,
    "bounds span every contour",
    JSON.stringify(box)
  );

  // Control points bound their curve, so they are included.
  const withControl = boundsOfContours([{ points: [ON(0, 0), OFF(0, 999), ON(10, 0)] }]);
  equal(withControl.maxY, 999, "off-curve points are counted toward the bounds");

  equal(boundsOfContours([]), null, "no contours, no bounds");
  equal(boundsOfContours([{ points: [] }]), null, "a contour with no points has no bounds");
  equal(boundsOfContours(null), null, "null is handled");
}

// ---------------------------------------------------------------------------
console.log("-".repeat(68));
console.log(`${passed}/${passed + failures.length} checks passed`);
if (failures.length) {
  console.log(`\nGLYPH-OUTLINE UNIT TESTS FAILED: ${failures.join(", ")}`);
  process.exit(1);
}
console.log("\nGLYPH-OUTLINE UNIT TESTS PASSED");
