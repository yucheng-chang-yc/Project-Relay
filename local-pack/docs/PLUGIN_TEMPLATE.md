# Project Relay Plugin Template

This is reusable source for a private ChatGPT plugin. A connected plugin needs the app registered in the installing user's account. Generate that package from an installed Local Runtime:

```powershell
$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" package --app-id $RelayAppId
```

Set `RelayAppId` to the actual app ID or ChatGPT app URL returned by registration. The generator creates `connection/generated/Project-Relay-Plugin-v0.2.0-preview.2.zip` with the registered app dependency, manifest, workflow skill and MIT LICENSE notice. Import that generated ZIP with Plugin Creator. Keep this generated account-specific package private.

The template contains no app ID, local endpoint or credentials. Importing the template alone does not establish local tool access. The Local Runtime README provides the complete install, tunnel and registration route. The `project-workbench` package identity is retained across updates.

Project Relay source is MIT-licensed. Retain the included `LICENSE` when distributing this template or its generated Plugin package.
