import { describe, expect, it } from "vitest";

import { backendPath, sameOrigin } from "./proxy";

describe("backendPath", () => {
  it("allows the user-facing API roots", () => {
    expect(backendPath(["skills", "review-member", "run"])).toBe("/skills/review-member/run");
    expect(backendPath(["reports", "zhangsan", "2026-11-17"])).toBe("/reports/zhangsan/2026-11-17");
    expect(backendPath(["me", "integrations"])).toBe("/me/integrations");
    expect(backendPath(["escalations", "abc", "resolve"])).toBe("/escalations/abc/resolve");
  });

  it("blocks internal endpoints and traversal", () => {
    expect(backendPath(["internal", "activity"])).toBeNull();
    expect(backendPath(["skills", "..", "internal", "activity"])).toBeNull();
    expect(backendPath(["skills", "a/../../internal"])).toBeNull();
    expect(backendPath(["skills", "a\\b"])).toBeNull();
    expect(backendPath(["readyz"])).toBeNull();
    expect(backendPath([])).toBeNull();
  });

  it("encodes segments", () => {
    expect(backendPath(["reports", "a b", "2026-11-17"])).toBe("/reports/a%20b/2026-11-17");
  });
});

describe("sameOrigin", () => {
  it("compares the Origin host with the request host", () => {
    expect(sameOrigin("https://dayline.corp", "dayline.corp")).toBe(true);
    expect(sameOrigin("https://evil.example", "dayline.corp")).toBe(false);
    expect(sameOrigin(null, "dayline.corp")).toBe(false);
    expect(sameOrigin("not a url", "dayline.corp")).toBe(false);
  });
});
