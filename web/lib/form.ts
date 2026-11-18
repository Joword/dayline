// Manifest inputs -> form fields (plan §7.5: the Web builds each skill's form from skill.yaml).
// Pure functions, so the mapping is unit-tested without a browser.

import type { InputSpec } from "./types";

export type Widget = "textarea" | "text" | "date" | "date_range" | "select" | "number" | "checkbox" | "json";

export interface Field {
  name: string;
  label: string;
  help: string;
  widget: Widget;
  required: boolean;
  choices: string[];
  maxLength?: number;
  initial: string | boolean;
}

const WIDGETS: Record<InputSpec["type"], Widget> = {
  text: "textarea",
  string: "text",
  date: "date",
  date_range: "date_range",
  enum: "select",
  int: "number",
  bool: "checkbox",
  json: "json",
};

export function fieldsFor(inputs: InputSpec[], today: string): Field[] {
  return inputs.map((i) => {
    const widget = WIDGETS[i.type] ?? "text";
    let initial: string | boolean = "";
    if (widget === "checkbox") initial = Boolean(i.default);
    else if (i.default !== null && i.default !== undefined) initial = String(i.default);
    else if (widget === "date" && i.required) initial = today;
    else if (widget === "date_range") initial = `${today}..${today}`;
    else if (widget === "select" && i.required && i.choices?.length) initial = i.choices[0];
    return {
      name: i.name,
      label: i.name,
      help: i.description,
      widget,
      required: i.required,
      choices: i.choices ?? [],
      maxLength: i.max_chars ?? undefined,
      initial,
    };
  });
}

export type FormValues = Record<string, string | boolean>;

/** Values -> the `inputs` object for POST /skills/{name}/run. Empty optional fields are omitted. */
export function collectInputs(fields: Field[], values: FormValues): { inputs: Record<string, unknown>; errors: string[] } {
  const inputs: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const f of fields) {
    const v = values[f.name];
    if (f.widget === "checkbox") {
      inputs[f.name] = Boolean(v);
      continue;
    }
    const s = typeof v === "string" ? v.trim() : "";
    if (!s) {
      if (f.required) errors.push(`${f.name} 必填`);
      continue;
    }
    if (f.maxLength && s.length > f.maxLength) {
      errors.push(`${f.name} 超过 ${f.maxLength} 字`);
      continue;
    }
    if (f.widget === "number") {
      const n = Number(s);
      if (!Number.isInteger(n)) errors.push(`${f.name} 需要整数`);
      else inputs[f.name] = n;
    } else if (f.widget === "json") {
      try {
        inputs[f.name] = JSON.parse(s);
      } catch {
        errors.push(`${f.name} 不是合法的 JSON`);
      }
    } else if (f.widget === "date_range") {
      const [a, b] = s.split("..");
      if (!a || !b || b < a) errors.push(`${f.name} 日期范围不正确`);
      else inputs[f.name] = s;
    } else if (f.widget === "select" && !f.choices.includes(s)) {
      errors.push(`${f.name} 只能是 ${f.choices.join(" / ")}`);
    } else {
      inputs[f.name] = s;
    }
  }
  return { inputs, errors };
}

/** YYYY-MM-DD in the given IANA time zone. */
export function todayIn(timeZone: string, now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
}
