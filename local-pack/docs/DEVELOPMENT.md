# Development and release process

## Source layout

| Path | Purpose |
| --- | --- |
| `project-workbench/workbench/`, `project-workbench/ui/` | Runtime, MCP and app views |
| `project-workbench/skills/`, `project-workbench/plugin.json` | Plugin workflow and metadata |
| `install.py`, `Install.ps1`, `tools/` | Installation, launch, registration, connection and package checks |
| `tests/`, `project-workbench/tests/` | Installer/bootstrap/shutdown/UI and runtime tests |
| `docs/`, `templates/`, `build.py` | Release instructions, config example and deterministic ZIP builder |

Run these commands from the extracted Local Pack or a source checkout with this layout. Python runtime and build use the standard library; there is no pip dependency installation. Git is needed by Git/task tests. Node is needed only by JavaScript UI tests. Real executor/R/tunnel tests require their separately installed tools.

## Reproducible build and checks

```powershell
python .\build.py --release-dir ..\release
python .\tools\verify_package.py --package .
python -m unittest discover -s tests -p "test_*.py" -v
Push-Location .\project-workbench
python -m unittest discover -s tests -p "test_*.py" -v
Pop-Location
node .\tests\ui-interactions.cjs
```

The first build refreshes `PACKAGE_MANIFEST.json` before installer tests consume it. Build again after any source change. Output must be outside the source tree; `--release-dir` emits the Local ZIP, universal Plugin Template, README, INSTALL, RELEASE_NOTES, LICENSE and SHA256SUMS. It excludes Python caches and refuses configured-installation state, links and key files. Identical input bytes produce identical ZIPs in the tested build environment; compare complete hashes after rebuilding.

Installer/connection tests run subprocesses with isolated temporary installations. Some tests are platform-specific and report SKIP on the other OS. Fake external clients establish offline behavior, not real tunnel authentication or ChatGPT acceptance. Runtime tests include unit/integration coverage for Git/tasks, Events, snapshots and computation. `test_native_r.py` exercises the command runtime only where its requirements are available; it does not automate a real Claude/ChatGPT session. The older `spikes/test_transfer_*.cjs` fixtures target an earlier UI harness and are not current release checks; use `tests/ui-interactions.cjs` for the shipped UI.

## Fresh install and connected Plugin

From an **extracted release ZIP**, Windows local acceptance is:

```powershell
.\Test-FreshInstall.ps1
```

Or run the actual installer, installed self-test and project smoke from [INSTALL.md](INSTALL.md). Those checks do not start an AI task or a live tunnel. Use [ACCEPTANCE.md](ACCEPTANCE.md) for the real full-client path probe, account smoke, task checks and computational E2E. The ChatGPT/Claude/R loop remains an orchestrated host test, not a single automated command. [VERIFICATION.md](VERIFICATION.md) records which exact versions ran it.

After configuring a real installed tunnel/app, generate your connected package with:

```powershell
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" package --app-id $RelayAppId
```

`build.py` creates the universal Template. The installed helper creates the private app-bound Plugin ZIP; building the Template alone does not establish a ChatGPT connection.

## Dependencies and licenses

Project Relay source is MIT-licensed; see [../LICENSE](../LICENSE). Its Python runtime/build has no third-party Python package dependency. Test and trial fixtures are source files, not installed provider executables. No Python, Node, Git, R, OpenAI Codex, Anthropic Claude Code, Docker or tunnel-client binaries are redistributed in these artifacts. Install and use those tools under their own licenses/service terms. The included container Dockerfile is an optional development fixture, not a bundled container image or the Windows default security model.

The installer copies the MIT notice into the installation. Universal Template and generated Plugin ZIPs retain it. The runtime core continues to report `0.2.0-spike.4`; package/Plugin metadata reports `0.2.0-preview.2`.

## Contribution and version status

Issues are welcome. Discuss substantial PRs in an issue before implementation; submitted changes need relevant tests and evidence for any release claim. Preview APIs/configuration may change, clean reinstallation may be needed and backward compatibility is not promised. Stable/production-ready status is not claimed.

The intended source/release repository is https://github.com/yucheng-chang-yc/Project-Relay. This candidate is prepared for publication; packaging it does not itself publish or tag that repository.
