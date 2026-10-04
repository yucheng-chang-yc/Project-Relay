# Foundation spike runbook — 0.2.0-spike.3

這包是隔離試驗 source，未達 Scoped Trial Ready；不是新版已部署產品，也不更新帳號 Plugin。主需求全文、source baseline、目前實測結果與 gap mapping 在上層 DEVELOPMENT_REPORT.md。

## Native trusted R（spike.3，2026-10-03 已核准範圍）

本輪採本機信任式 R，容器 route 暫停（下方容器章節保留為歷史設計，不是本輪完成條件）。使用者已明確同意：Workbench 派工的 Claude 任務以使用者帳號、無 OS containment 執行指定的 Rscript。

project 設定（operator 本機編輯；三個欄位以外的任何欄位都會被拒絕）：

```json
{"compute_runtime": {"backend": "native_trusted",
                     "rscript": "C:\\R-4.5.1\\bin\\Rscript.exe",
                     "trust_acknowledgement": "local-account-r-execution-without-os-containment"}}
```

- Claude 以 `--restricted` 執行（無 Bash/PowerShell），只能透過 task-bound `wb_runtime` MCP 的 run／get_run／terminate_run 執行 R；只有 `Rscript` profile，argv 固定為 `[Rscript, --vanilla, <凍結的腳本副本>]`，無 shell，cwd 為 task worktree。`claude_allowed_tools` 含任何 Bash 規則時，計算任務會被拒絕。
- 程序樹：Windows 以 suspended 啟動並指派 Job Object（KILL_ON_JOB_CLOSE），timeout／cancel／正常結束後確認 job 內程序數為 0；POSIX 以 process group。receipt 記錄 `termination_confirmed`、`processes_alive_at_stop`。
- 傳給 R 的環境變數採 allowlist，不含 facade／tunnel／CLI 的 key 或 token。這不是安全邊界：R 仍可用 `system()` 及帳號權限讀寫檔案。
- 輸入檔沒有 OS 唯讀保護；每次 run 前核對 SHA，run 後若被改動，receipt 標 `input_modified`。
- capabilities 會回報 `computational_runtime.os_containment=false`、`features.native_trusted_r=true`；`computational_runtime_verified` 仍為 false。
- 分析程式放在 repo（committed），資料用 stage 工具以 input 帶入；Claude 修改 repo 內的 .R，修正會成為可審查的 diff。staged input 本身不應被編輯（編輯後 run 會被拒絕）。
- 關閉方式：移除 project 的 `compute_runtime`，重啟 facade。

## Native Windows 一次性環境驗證

沿用已安裝的 Python，執行 `python scripts/compute_doctor.py --output windows-doctor.json`。輸出只包含版本/路徑/WSL raw output 與 SHA；不安裝 runtime、不改登錄、Bridge 或 tunnel profile。PATH 找不到不等於機器沒有；必要時由 operator 提供已知的絕對路徑。

Windows 隔離試裝已完成，精確 source/config/state/profile 與 rollback 路徑見上層 NEXT_STEP_FOR_LOCAL_AGENT.md。DesktopCommander 已移除且不需要安裝。沿用本機 Codex/Claude 工作階段部署新 source，保留既有試驗 config/state/project 與 0.1.2 task store。stdio 入口必須包含 `python scripts/serve.py --config <explicit-trial-config> --stdio`。保留既有 App/tunnel 身份，使用明確 `--profile-file`，不將 key 值放入 source。

## 容器 route

Workbench 沒有原生 shell fallback。需要已存在的 Docker Desktop Linux-container engine 或等效 Podman VM route；單獨有 WSL 不算 enforcement。不能把 Docker daemon socket、Windows drives、Git common directory、runtime state 或憑證 mount 到 guest。

`spikes/container/Dockerfile` 要求 operator 指定已核對的 Debian base-image digest；建構後以 image SHA 配置 Workbench，執行時 `--pull=never`。Dockerfile 的 apt resolution 目前沒有 lock/reproducibility 證據，候選 image 尚未建構，不得宣稱固定 package identity。

候選 project config 增加：

```json
{"compute_runtime":{"backend":"docker","executable":"ABSOLUTE_EXISTING_DOCKER_EXECUTABLE","image":"sha256:ACTUAL_64_HEX_IMAGE_ID","user":"65532:65532","enforcement_verified":false}}
```

Linux 使用與工作區 owner 相容的非 root UID/GID；Windows Linux container bind permission 必須實測。`enforcement_verified` 是 operator attestation，並非程式自動認證，只有在 image、engine、mount/network/resource/timeout/cancel/worker-loss canaries 全部實測並保存身份後才可設為 true。候選 build 沒有產生這份認證。

Canary 必須包含 outside read/write、network、daemon socket、host drive、另起 interpreter、timeout、terminate、worker loss 與 primary tree preservation。`containment_canary.py` 只覆蓋最小 filesystem/network observations，不能單獨用它宣稱整個安全驗收通過。所有 command run 的 lease、frozen primary script、readonly inputs、stdout/stderr 與 receipt 保存在 task store；不確定 lease 保留 writer lock。長期獨立 lease watchdog 尚未完成，worker 突然遺失仍要求 operator recovery，不能宣稱已自動回收所有 container。

Claude computational adapter 要求 CLI 實測版本 >= 2.1.248，使用 `--restricted`、既有 subscription auth、`dontAsk` 和唯一 `wb_runtime` MCP。只提供 run/get_run/terminate_run；Rscript/Python 固定 profile，不提供 raw argv/shell。Windows 報告已確認 Claude 2.1.251 的 --restricted 旗標存在；實際 computational MCP startup 與 container lifecycle 尚未通過。

## ChatGPT host route（可先測，無容器依賴）

固定 host_probe command 只在 computetrial 產生 deterministic 1 MiB/20 MiB ZIP，保存 artifact/result SHA。這是 transport fixture，沒有呼叫模型或 R；不要以 fixture 運行當 computational sandbox 證據。配置與任務 contract 見上層 NEXT_STEP_FOR_LOCAL_AGENT.md。

候選 MCP 掃描 `stage_binary_input` 的四欄 fileParams schema；用真實 ChatGPT 附件測試 1 MiB 和 5–20 MiB ZIP。傳輸設定需要 `binary_transfer.allowed_download_hosts` 的精確 HTTPS hostnames，沒有 wildcard、redirect、proxy 或內網 route。signed URL 不進任何持久 state。spike.2 widget 只顯示 download_host，供本機 operator 核對並設定 exact allowlist；設定變更後重啟 facade/重新開啟 widget，使 server allowlist 與 CSP 同步。若 host API 缺少或 CSP/網路阻擋，保存實際 blocker，不開 wildcard/CSP bypass。

執行 `open_transfer_spike`：選擇已在 ChatGPT 的 file，stage 並比對 SHA。artifact 路線以 app-only chunks 取回 byte、程式建立 File、uploadFile、取得 host file 並獨立 SHA round-trip。OpenAI 文件將 uploadFile 描述為 user-selected file，因此 programmatic File acceptance 必須實測；這個候選 widget 正是為辨識該 compatibility boundary。

UI 的 `widget_roundtrip=PASS` 仍不足以完成驗收。下一回合 GPT 要真正讀到 host file、核對內容和 SHA；widgetState 或 file_id 文字不構成 file attachment 證據。Host route 不成立時，要記為平台限制/implementation gap/未驗證其中之一，保留原產品要求；MCP resource blob 或 manual dashboard ZIP 不得代替成功。

## Tomato acceptance（需真實 host + 真實 Claude + 安全 R）

1. ChatGPT 直接 stage `input.csv` 與 intentional-error `analysis.R`，輸入 SHA/size 由獨立讀取取得。啟動 Claude task，destination 分別 `wb_inputs/tomato.csv`、`analysis.R`，`compute_runtime=true`，declared artifacts 為 result.csv/model.rds/plot.png/result_bundle.zip。
2. Claude 呼叫 restricted run(profile=Rscript, script=analysis.R)；poll get_run，確認非 0、stderr 含 WB_INTENTIONAL_ERROR；只修改 task workspace 的 R，rerun，取得 exit 0 與 termination_confirmed。
3. Workbench capture 4 個成果、command-run evidence 和 manifest SHA。檢查 CSV 有 6 行且 prediction 合理；用 R readRDS 檢查 lm；PNG 可解碼且合理；ZIP 正好包含這 3 個檔案、解壓 bytes 與 individual artifacts SHA 相同。
4. WebGPT 直接取得 binary host file，SHA round-trip 相同，實際讀 CSV／plot／ZIP。保存不可覆寫 Review Object；PASS 仍與 Human acceptance、apply、commit/push 分開。
5. stage_prior_artifact 選 result.csv/model.rds，附 exact result/SHA。follow-up task 帶 parent_task_id/parent_result_sha256，沿用 Work Unit；stage followup.R 到 task workspace，安全 R 產生 followup.csv。全程不要求 Human 下載再上傳。
6. 正常完成、intentional error、permission deny、取消、逾時、worker loss、stale input/result/artifact/log/head 都要有 distinct observations。完成通知再驗證 MCP Events 2.0，不能把 widget 自動刷新算成跨回合 completion relay 已解決。

`python spikes/tomato/preflight.py --output tomato-preflight.json` 在缺少 foundation 時 exit 2、BLOCKED；沒有模擬 E2E PASS 路徑。若只能人工搬 ZIP，結果是 PARTIAL。
