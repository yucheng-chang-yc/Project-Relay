# preview.3 installation documentation erratum

2026-10-06 Windows operator report: tunnel-client 0.0.11 exposes `/readyz` (ready) and `/healthz` (live). The instruction's `/livez` returns 404 and is incorrect for that client version.

Use the health port saved in your connection configuration; do not assume 8080 if a different port was selected:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/readyz'
Invoke-RestMethod 'http://127.0.0.1:8080/healthz'
```

Online connection and operator documentation has been corrected. The installed/frozen preview.3 Local ZIP and handoff bytes are retained unchanged for traceability; this erratum also applies to the copies of connection instructions inside those packages. No runtime change, restart, reinstallation or new Plugin generation is required for this documentation fix. Include the corrected URL when building the next candidate package.

Local ZIP SHA-256: `d5b445d6aaf7f8c2b42565bf73f0a70690b376132bee9e1b347bea9df68f5f7b`.

Windows offline installation/new fixture tests and the real tunnel switch/Plugin generation were reported PASS. Live ChatGPT shared-folder approval, byte transfer/revoke and task archive acceptance remain NOT_TESTED at this checkpoint. Runtime preview.3 identity was independently observed from `open_workbench`; the current conversation's exposed tool registry still needs refresh for the new tools.
