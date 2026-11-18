import { describe, expect, it } from "vitest";

import { collectInputs, fieldsFor, todayIn } from "./form";
import type { InputSpec } from "./types";

const spec = (over: Partial<InputSpec>): InputSpec => ({
  name: "x",
  type: "text",
  required: false,
  description: "",
  default: null,
  choices: null,
  max_chars: null,
  ...over,
});

const GENERATE_DAILY: InputSpec[] = [
  spec({ name: "date", type: "date", required: true }),
  spec({ name: "narrative", type: "text", required: true, max_chars: 20 }),
  spec({ name: "answers", type: "text" }),
];

describe("fieldsFor", () => {
  it("maps manifest types to widgets with sensible initial values", () => {
    const fields = fieldsFor(
      [
        ...GENERATE_DAILY,
        spec({ name: "tone", type: "enum", required: true, choices: ["encouraging", "direct"], default: "encouraging" }),
        spec({ name: "range", type: "date_range" }),
        spec({ name: "n", type: "int" }),
        spec({ name: "flag", type: "bool", default: true }),
        spec({ name: "who", type: "string" }),
      ],
      "2026-11-17",
    );
    expect(fields.map((f) => [f.name, f.widget, f.initial])).toEqual([
      ["date", "date", "2026-11-17"],
      ["narrative", "textarea", ""],
      ["answers", "textarea", ""],
      ["tone", "select", "encouraging"],
      ["range", "date_range", "2026-11-17..2026-11-17"],
      ["n", "number", ""],
      ["flag", "checkbox", true],
      ["who", "text", ""],
    ]);
    expect(fields[1].maxLength).toBe(20);
  });
});

describe("collectInputs", () => {
  const fields = fieldsFor(GENERATE_DAILY, "2026-11-17");

  it("omits empty optional fields and reports missing required ones", () => {
    expect(collectInputs(fields, { date: "2026-11-17", narrative: " 修了 bug ", answers: "" })).toEqual({
      inputs: { date: "2026-11-17", narrative: "修了 bug" },
      errors: [],
    });
    expect(collectInputs(fields, { date: "2026-11-17", narrative: "" }).errors).toEqual(["narrative 必填"]);
  });

  it("validates length, numbers, json, enums and ranges", () => {
    expect(collectInputs(fields, { date: "d", narrative: "x".repeat(21) }).errors[0]).toContain("超过 20");
    const more = fieldsFor(
      [
        spec({ name: "n", type: "int" }),
        spec({ name: "j", type: "json" }),
        spec({ name: "e", type: "enum", choices: ["a", "b"] }),
        spec({ name: "r", type: "date_range" }),
      ],
      "2026-11-17",
    );
    const { inputs, errors } = collectInputs(more, { n: "1.5", j: "{bad", e: "c", r: "2026-11-18..2026-11-17" });
    expect(inputs).toEqual({});
    expect(errors).toHaveLength(4);
    expect(collectInputs(more, { n: "3", j: '{"a":1}', e: "a", r: "2026-11-17..2026-11-18" }).inputs).toEqual({
      n: 3,
      j: { a: 1 },
      e: "a",
      r: "2026-11-17..2026-11-18",
    });
  });
});

describe("todayIn", () => {
  it("uses the given time zone", () => {
    const late = new Date("2026-11-17T17:30:00Z"); // 01:30 next day in Shanghai
    expect(todayIn("Asia/Shanghai", late)).toBe("2026-11-18");
    expect(todayIn("UTC", late)).toBe("2026-11-17");
  });
});
