import { describe, expect, test } from "bun:test";
import { fmtAgo, fmtDuration, fmtMs } from "./format";

const start = "2026-09-01T10:00:00.000Z";
const startMs = Date.parse(start);

const endAt = (iso: string, ms: number) =>
  new Date(Date.parse(iso) + ms).toISOString();

describe("fmtMs", () => {
  test("0 → 0s", () => expect(fmtMs(0)).toBe("0s"));
  test("47_000 → 47s", () => expect(fmtMs(47_000)).toBe("47s"));
  test("60_000 → 1m 00s", () => expect(fmtMs(60_000)).toBe("1m 00s"));
  test("247_000 → 4m 07s", () => expect(fmtMs(247_000)).toBe("4m 07s"));
  test("3_600_000 → 1h 00m", () => expect(fmtMs(3_600_000)).toBe("1h 00m"));
  test("7_500_000 → 2h 05m", () => expect(fmtMs(7_500_000)).toBe("2h 05m"));
  test("-5 → 0s", () => expect(fmtMs(-5)).toBe("0s"));
});

describe("fmtDuration", () => {
  test("an open run measures against nowMs and keeps ticking seconds", () => {
    expect(fmtDuration(start, null, startMs + 47_000)).toBe("47s");
    expect(fmtDuration(start, null, startMs + 48_000)).toBe("48s");
    expect(fmtDuration(start, null, startMs + 545_000)).toBe("9m 05s");
    expect(fmtDuration(start, null, startMs + 4_320_000)).toBe("1h 12m");
  });

  test("a finished run uses ended_at and ignores nowMs", () => {
    expect(fmtDuration(start, endAt(start, 125_000), startMs + 9_999_999)).toBe(
      "2m",
    );
  });

  test("a finished run drops the seconds", () => {
    expect(fmtDuration(start, endAt(start, 0), startMs)).toBe("0s");
    expect(fmtDuration(start, endAt(start, 59_000), startMs)).toBe("59s");
    expect(fmtDuration(start, endAt(start, 60_000), startMs)).toBe("1m");
    expect(fmtDuration(start, endAt(start, 3_599_000), startMs)).toBe("59m");
    expect(fmtDuration(start, endAt(start, 41 * 60_000 + 30_000), startMs)).toBe(
      "41m",
    );
  });

  test("past an hour, no padding", () => {
    expect(fmtDuration(start, endAt(start, 3_600_000), startMs)).toBe("1h 0m");
    expect(fmtDuration(start, endAt(start, 7_500_000), startMs)).toBe("2h 5m");
  });

  test("no started_at is an em dash", () => {
    const someEnd = endAt(start, 1000);
    expect(fmtDuration(null, null, Date.now())).toBe("—");
    expect(fmtDuration(undefined, someEnd, Date.now())).toBe("—");
    expect(fmtDuration("not-a-date", null, Date.now())).toBe("—");
  });

  test("an unparseable ended_at falls back to nowMs", () => {
    expect(fmtDuration(start, "garbage", startMs + 5_000)).toBe("5s");
  });

  test("a backwards ended_at clamps to zero", () => {
    expect(fmtDuration(start, endAt(start, -10_000), startMs)).toBe("0s");
  });
});

describe("fmtAgo", () => {
  test("under a minute is just now", () => {
    expect(fmtAgo(start, startMs + 30_000)).toBe("just now");
  });

  test("a future time is just now", () => {
    expect(fmtAgo(start, startMs - 5_000)).toBe("just now");
  });

  test("minutes, hours and days floor", () => {
    expect(fmtAgo(start, startMs + 5 * 60_000)).toBe("5m ago");
    expect(fmtAgo(start, startMs + 59 * 60_000)).toBe("59m ago");
    expect(fmtAgo(start, startMs + 2 * 3_600_000)).toBe("2h ago");
    expect(fmtAgo(start, startMs + (23 * 60 + 59) * 60_000)).toBe("23h ago");
    expect(fmtAgo(start, startMs + 3 * 86_400_000)).toBe("3d ago");
  });

  test("null and garbage are an em dash", () => {
    expect(fmtAgo(null, startMs)).toBe("—");
    expect(fmtAgo(undefined, startMs)).toBe("—");
    expect(fmtAgo("not-a-date", startMs)).toBe("—");
  });
});
