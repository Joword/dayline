// Which backend paths the browser may reach through the BFF.

const ALLOWED_ROOTS = new Set(["skills", "me", "reports", "team", "activity", "route", "escalations"]);

/**
 * Validate catch-all segments and build the backend path, or return null.
 * Rejects dot segments and embedded separators: "skills/../internal/activity" would
 * otherwise pass a prefix check and be normalised by fetch into /internal/activity.
 */
export function backendPath(segments: string[]): string | null {
  if (segments.length === 0 || !ALLOWED_ROOTS.has(segments[0])) return null;
  for (const s of segments) {
    if (s === "" || s === "." || s === ".." || s.includes("/") || s.includes("\\") || s.includes("\0")) return null;
  }
  return "/" + segments.map(encodeURIComponent).join("/");
}

/** Same-origin check for state-changing requests (defence in depth on top of SameSite cookies). */
export function sameOrigin(origin: string | null, host: string | null): boolean {
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
