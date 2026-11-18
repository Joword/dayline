import * as vscode from "vscode";

interface InputSpec {
  name: string;
  type: string;
  required: boolean;
  description: string;
  choices: string[] | null;
}

interface SkillInfo {
  name: string;
  description: string;
  inputs: InputSpec[];
}

interface RunResult {
  status: string;
  markdown: string;
  errors: string[];
  notice: string | null;
}

function cfg() {
  const c = vscode.workspace.getConfiguration("dayline");
  return {
    apiUrl: c.get<string>("apiUrl", "http://127.0.0.1:8000").replace(/\/$/, ""),
    token: c.get<string>("token", "").trim(),
    user: c.get<string>("user", "").trim(),
    lang: c.get<string>("lang", "zh"),
  };
}

function headers(): Record<string, string> {
  const { token, user } = cfg();
  const h: Record<string, string> = { accept: "application/json" };
  if (token) h.authorization = `Bearer ${token}`;
  if (user) h["x-dayline-user"] = user;
  return h;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${cfg().apiUrl}${path}`, { ...init, headers: { ...headers(), ...init?.headers } });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

async function loadSkills(): Promise<SkillInfo[]> {
  return api<SkillInfo[]>("/skills");
}

async function askInputs(skill: SkillInfo): Promise<Record<string, string> | undefined> {
  const inputs: Record<string, string> = {};
  for (const spec of skill.inputs) {
    const pick = spec.type === "enum" && spec.choices?.length
      ? await vscode.window.showQuickPick(spec.choices, {
          title: skill.name,
          placeHolder: spec.description || spec.name,
          ignoreFocusOut: true,
        })
      : await vscode.window.showInputBox({
          title: `${skill.name}: ${spec.name}`,
          prompt: spec.description || spec.name,
          ignoreFocusOut: true,
          placeHolder: spec.required ? "required" : "optional",
        });
    if (pick === undefined) return undefined;
    if (pick !== "") inputs[spec.name] = pick;
    else if (spec.required) {
      void vscode.window.showWarningMessage(`${spec.name} is required`);
      return undefined;
    }
  }
  return inputs;
}

async function runSkill(skill: SkillInfo): Promise<void> {
  const inputs = await askInputs(skill);
  if (!inputs) return;
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Dayline: ${skill.name}` },
    () =>
      api<RunResult>(`/skills/${encodeURIComponent(skill.name)}/run`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ inputs, lang: cfg().lang }),
      }),
  );
  const parts = [result.notice, result.markdown, result.errors.length ? result.errors.join("\n") : ""]
    .filter(Boolean)
    .join("\n\n");
  const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: parts || result.status });
  await vscode.window.showTextDocument(doc, { preview: true });
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("dayline.refresh", async () => {
      try {
        const skills = await loadSkills();
        void vscode.window.showInformationMessage(`Dayline: ${skills.length} skills`);
      } catch (e) {
        void vscode.window.showErrorMessage(`Dayline: ${e instanceof Error ? e.message : String(e)}`);
      }
    }),
    vscode.commands.registerCommand("dayline.run", async () => {
      try {
        const skills = await loadSkills();
        const picked = await vscode.window.showQuickPick(
          skills.map((s) => ({ label: s.name, description: s.description, skill: s })),
          { title: "Dayline skills", ignoreFocusOut: true },
        );
        if (!picked) return;
        await runSkill(picked.skill);
      } catch (e) {
        void vscode.window.showErrorMessage(`Dayline: ${e instanceof Error ? e.message : String(e)}`);
      }
    }),
  );
}

export function deactivate(): void {}
