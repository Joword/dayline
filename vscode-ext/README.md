# Dayline for VS Code

Runs Dayline skills from the command palette. The extension talks to the same API as the CLI and the web app.

## Setup

Cloning the repository does not install the extension. `node_modules/` and the compiled `out/` directory stay on your machine.

```bash
cd vscode-ext
npm install
npm run compile
npx @vscode/vsce package
code --install-extension dayline-0.1.0.vsix
```

To try it without installing, open this folder in VS Code and press F5.

Set `dayline.apiUrl`. Direct to the API it is `http://127.0.0.1:8000`. Behind the gateway it is `https://<host>/v1`.
4. Set `dayline.token` to a personal token (`dl token create <user>`). With the service token, also set `dayline.user`. In dev mode, with no token configured on the API, `dayline.user` alone is enough.

## Use

**Dayline: Run a skill** loads `GET /skills`, lets you pick one, asks for each input, then posts to `POST /skills/{name}/run` and opens the Markdown result.

**Dayline: Refresh skills** checks that the API is reachable and shows how many skills it returned.
