export const ESC_STATUS: Record<string, { label: string; tone: "amber" | "indigo" | "green" | "slate" }> = {
  open: { label: "跟踪中", tone: "slate" },
  escalated: { label: "已上报", tone: "amber" },
  in_progress: { label: "处理中", tone: "indigo" },
  resolved: { label: "已解决", tone: "green" },
};
