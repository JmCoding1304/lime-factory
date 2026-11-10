import { expect, test } from "bun:test";
import {
  BLOCK_H_EST,
  BLOCK_W,
  COL_PITCH,
  GRID,
  ORIGIN,
  chainPairs,
  panBy,
  placeMissing,
  slotPos,
  snap,
  toStage,
  wheelToView,
  wirePath,
  zoomAt,
  type Rect,
} from "./bench";
import { ROLE_ORDER } from "./profiles";

const BLK = (x: number, y: number): Rect => ({
  x,
  y,
  w: BLOCK_W,
  h: BLOCK_H_EST,
});

test("snap rounds to the dot grid and never goes negative", () => {
  expect(snap(23)).toBe(GRID);
  expect(snap(24)).toBe(GRID);
  expect(snap(35)).toBe(2 * GRID);
  expect(snap(10)).toBe(0);
  expect(snap(-5)).toBe(0);
  expect(snap(-500)).toBe(0);
});

test("slotPos walks three per row", () => {
  expect(slotPos(0)).toEqual(ORIGIN);
  expect(slotPos(1)).toEqual({ x: ORIGIN.x + COL_PITCH, y: ORIGIN.y });
  expect(slotPos(2)).toEqual({ x: ORIGIN.x + 2 * COL_PITCH, y: ORIGIN.y });
  expect(slotPos(3)).toEqual({ x: ORIGIN.x, y: ORIGIN.y + 198 });
  expect(slotPos(7)).toEqual({
    x: ORIGIN.x + COL_PITCH,
    y: ORIGIN.y + 2 * 198,
  });
});

test("placeMissing keeps existing positions and skips occupied slots", () => {
  const pos = new Map([
    ["a", slotPos(0)],
    ["b", slotPos(2)],
  ]);
  placeMissing(["a", "b", "c"], pos);
  expect(pos.get("a")).toEqual(slotPos(0)); // untouched
  expect(pos.get("b")).toEqual(slotPos(2)); // untouched
  // c's natural slot (display index 2) is occupied by b → next free slot
  expect(pos.get("c")).toEqual(slotPos(3));
  expect(placeMissing(["a", "b", "c"], pos)).toBe(pos); // same Map, mutated
});

test("a 7-role roster plus a defaults block fits inside 1230×700", () => {
  const pos = placeMissing(["__defs", ...ROLE_ORDER], new Map());
  expect(pos.size).toBe(8);
  for (const p of pos.values()) {
    expect(p.x + BLOCK_W).toBeLessThanOrEqual(1230);
    expect(p.y + BLOCK_H_EST).toBeLessThanOrEqual(700);
  }
  // nothing collided: the 8 keys sit on slots 0..7 in order
  expect([...pos.values()]).toEqual(
    [0, 1, 2, 3, 4, 5, 6, 7].map((i) => slotPos(i)),
  );
});

test("chainPairs follows ROLE_ORDER and ignores absent roles", () => {
  expect(chainPairs(["zz", "builder", "scout"])).toEqual([
    ["scout", "builder"],
    ["builder", "zz"],
  ]);
  expect(chainPairs(["reviewer", "planner", "builder"])).toEqual([
    ["planner", "builder"],
    ["builder", "reviewer"],
  ]);
  expect(chainPairs(["solo"])).toEqual([]);
  expect(chainPairs([])).toEqual([]);
});

test("wirePath goes right when b is to the right, down when it wraps", () => {
  const right = wirePath(BLK(66, 66), BLK(66 + COL_PITCH, 66));
  const dx = Math.max(40, Math.min(120, (368 - 302) / 2)); // 40
  expect(right).toBe(`M 302 141 C ${302 + dx} 141, ${368 - dx} 141, 368 141`);

  // b starts left of a's midline → the wire drops from a's bottom to b's top
  const down = wirePath(BLK(66, 66), BLK(66, 66 + 198));
  const dy = Math.max(40, Math.min(120, (264 - 216) / 2)); // 40
  expect(down).toBe(`M 184 216 C 184 ${216 + dy}, 184 ${264 - dy}, 184 264`);
});

test("zoomAt keeps the cursor point fixed and clamps", () => {
  const v = { x: 0, y: 0, k: 1 };
  const zoomed = zoomAt(v, 100, 50, 2);
  expect(zoomed).toEqual({ x: -100, y: -50, k: 2 });
  // the stage point under the cursor is unchanged
  expect((100 - zoomed.x) / zoomed.k).toBeCloseTo(100);
  expect((50 - zoomed.y) / zoomed.k).toBeCloseTo(50);
  expect(zoomAt(v, 0, 0, 100).k).toBe(3); // clamps to max
  expect(zoomAt(v, 0, 0, 0.001).k).toBe(0.25); // clamps to min
  // factor 1 is a no-op
  expect(zoomAt({ x: 10, y: 20, k: 0.5 }, 40, 60, 1)).toEqual({
    x: 10,
    y: 20,
    k: 0.5,
  });
});

test("wheelToView pans on a plain wheel and zooms with ctrl/meta", () => {
  const v = { x: 0, y: 0, k: 1 };
  const panned = wheelToView(
    v,
    { deltaX: 10, deltaY: -4, ctrlKey: false, metaKey: false },
    100,
    100,
  );
  expect(panned).toEqual({ x: -10, y: 4, k: 1 });
  const zoomed = wheelToView(
    v,
    { deltaX: 0, deltaY: -100, ctrlKey: true, metaKey: false },
    100,
    100,
  );
  expect(zoomed.k).toBeCloseTo(Math.exp(1));
  // the cursor point stays fixed
  expect((100 - zoomed.x) / zoomed.k).toBeCloseTo(100);
  expect((100 - zoomed.y) / zoomed.k).toBeCloseTo(100);
  const meta = wheelToView(
    v,
    { deltaX: 0, deltaY: 0, ctrlKey: false, metaKey: true },
    100,
    100,
  );
  expect(meta.k).toBe(1); // deltaY 0 → factor 1
  expect(meta.x).toBe(0);
});

test("toStage converts canvas-relative client coords", () => {
  const v = { x: -100, y: -50, k: 2 };
  expect(toStage(v, 100, 50)).toEqual({ x: 100, y: 50 });
});

test("panBy shifts the view without touching k", () => {
  expect(panBy({ x: 1, y: 2, k: 1.5 }, 5, -3)).toEqual({ x: 6, y: -1, k: 1.5 });
});