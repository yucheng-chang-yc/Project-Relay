# Project Relay — Preview UI

The primary workflow stays in the conversation. Open the optional Tasks panel when status or result inspection is useful. User-supplied names, paths, goals and outputs retain their original language.

## Interface changes

- **Results beside the task:** View results and View log expand the detail section directly inside the selected task card. Opening moves focus to that section and brings it into view. Closing returns focus to the task card, including while its result button is disabled during loading. Polling keeps the section attached to its task. Switching tasks or closing ignores stale responses.
- **Neutral styling:** grayscale surfaces, restrained borders, compact headings and simple actions, with light/dark themes. This is an independent Codex-inspired treatment, not an official OpenAI component library.
- **Contextual file consent:** the conversation request displays “Allow ChatGPT to read this file?”, its exact path, read-only scope and Allow once / Decline. Request and snapshot metadata are collapsed. No separate File access navigation is required.
- **Developer diagnostics:** the transfer probe remains available for explicit host-transport debugging and is described that way in MCP metadata. It is omitted from the general preview and routine workflow. This is a UX designation, not a new access-control boundary. The task connection test is under Connection details.
- **Preview:** Tasks is the main view, with a separate collapsed conversation example for consent. Width, theme and 3/60 sample-task controls are preview-only. No File access / File transfer navigation tabs remain.

## Compatibility

Package `0.2.0-preview.2`; UI `0.2.0-ui.3`; runtime baseline `0.2.0-spike.4`.

Current MCP pointers use `tasks-v3.html`, `transfer-v3.html` and `file-snapshot-v3.html` under `ui://project-workbench/`. All six previous UI pointers remain readable aliases. Existing cache hints, app-only approval tools and byte-access visibility are preserved. The existing skill and runtime behavior remain intact.

## Integration acceptance

After the candidate is deployed, refresh the MCP connection and open a new task card. With many tasks, open results and logs on an early and middle task, verify that their contents appear there, wait for refresh, switch tasks, and close. Confirm keyboard focus and scroll behavior in the real host. Check wide and narrow cards, long paths, and both themes.

Request a file in the conversation and complete one approval and one denial. Match the actual server state and retrieved bytes. Do not treat chat text as the server-verifiable approval click. Run the transfer diagnostics only when investigating that transport; input availability is still host-dependent.

The candidate has not been deployed to the active Windows service or account plugin. Browser rendering and real ChatGPT clicks require separate acceptance of this exact candidate.
