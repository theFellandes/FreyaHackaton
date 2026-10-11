# Cloudflare login before the final

**Verified on 11 October 2026:** Wrangler is authenticated with OAuth and one account. Codex's Cloudflare MCP is
also authenticated, and the 17 official Cloudflare skills are installed under `C:\Users\Felladien\.codex\skills`.
The MCP entry is in `C:\Users\Felladien\.codex\config.toml`; a backup was saved before adding it. Start a new Codex
session when you need the new MCP tools. The steps below are for a future login check; repeating login is unnecessary.
No paid plan was enabled or service deployed. Use the Workers Free plan for the mock.

Create or sign in to your own [Cloudflare account](https://dash.cloudflare.com/), then authorize Wrangler for the account that will host the bank mock. Complete this before the kit opens at **20:00 Istanbul on 11 October 2026**.

Run this in PowerShell. It uses the installed Wrangler and bundled Node directly; npm/npx are not required.

```powershell
Set-Location -LiteralPath 'C:\Users\Felladien\Documents\Projects\Python\FreyaHackaton\submission\mock-bank'
$freyaNode = 'C:\Users\Felladien\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$freyaWrangler = '.\node_modules\wrangler\bin\wrangler.js'
& $freyaNode $freyaWrangler login
& $freyaNode $freyaWrangler whoami
```

Finish the browser sign-in and approval. `whoami` should then show the intended account. Keep credentials out of chat and tracked files. [Official Wrangler login and authentication commands](https://developers.cloudflare.com/workers/wrangler/commands/general/#login).

If the browser does not open or the localhost callback fails, cancel the pending login with Ctrl+C and use device login:

```powershell
& $freyaNode $freyaWrangler login --device --browser=false
& $freyaNode $freyaWrangler whoami
```

Open the printed verification URL, enter the displayed code if requested, and approve access while the terminal waits. Device login avoids the local callback server. [Official device login instructions](https://developers.cloudflare.com/workers/wrangler/commands/general/#use-wrangler-login-without-a-local-callback-server).

If Node and pnpm are already on PATH, the equivalent commands from the same directory are:

```powershell
pnpm exec wrangler login
pnpm exec wrangler whoami
```

Login prepares account access only. Keep the deployment guard in place until the released final contract and customers have been implemented and tested. Follow [FINAL_RUNBOOK.md](FINAL_RUNBOOK.md) for deployment and evidence; keep the eventual Worker online until results are published.
