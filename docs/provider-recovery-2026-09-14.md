# Provider/chat recovery — 2026-09-14

## Acceptance boundary

An OAuth approval is **not** proof of inference. A saved provider is **not** the
active provider. A successful diagnostic request is **not** acceptance of the
whole desktop chat. Report those separately. Do not clean installations or erase
credentials to hide regressions.

The Mac still had desktop 0.9.20 and Ads 0.2.13 during diagnosis. Candidate
0.9.22/0.2.15 completed CI but predates the fixes in this note. It must not be
promoted as the solution to these chat failures.

## Observed failures and changes

| Failure reproduced | Correction | Evidence |
| --- | --- | --- |
| SQL Qwen remained active after native Codex OAuth; the settings view showed both active and the chat chip named Qwen. | Use the native effective selection as authority; collapse only the matching custom mirror. A failed native lookup is 503, not an empty selection. | Full ProvidersView regressions plus useActiveProvider/chat tests. |
| Codex chat rejected `chat_template_kwargs` with HTTP 400. | No universal Qwen extension. Default it only for native custom Qwen Chat Completions. Do not mutate nested shared profile settings. | Engine factory regression matrix: Codex Astra/Terra, OpenAI, Anthropic, OpenRouter, custom and managed paths. |
| Nonzero temperature was passed as an unsupported AIAgent constructor keyword. | Use the supported request override only on Chat Completions; leave Responses sampling native. | Actual pinned AIAgent signature inspected; three transport regressions. |
| Native Codex “Probar” used raw Chat Completions rather than Responses. | Use Hermes' native Responses adapter, with its auth headers and payload conversion, and require a returned text response. No tools or agent loop. | New adapter tests; real request in the installed container returned text with saved OAuth and Astra. |
| Retrying a failed custom connection repeated POST against a saved unique alias. | PATCH the same saved provider, preserve its vaulted key unless explicitly replaced, and retain recovery UI instead of unmounting it. | REST permissions/validation tests and UI retry/secret-clearing tests. |
| Transient retries produced repeated terminal warnings and busy UI could remain stuck. | Durable task lifecycle and exact task-status lookup; one final error; UI reconciles terminal state without resending the user message. | Backend queue/mirror/stream integration and frontend lifecycle regressions. |

## Real, narrowly scoped checks

- A Qwen chat task completed and persisted “Conexión verificada” before switching
  to Codex. That result does not prove the later provider switching path.
- A corrected native Responses diagnostic using the saved Codex OAuth returned
  text. The diagnostic did not alter the selected provider, publish a campaign,
  use tools, or print credentials.
- Live Codex catalog discovery returned `gpt-6-astra`, `gpt-5.6-sol`,
  `gpt-5.6-terra`, `gpt-5.6-luna`, and `gpt-5.5` for this account. The account
  consistency check passed. This is not a hardcoded fallback catalog.

## Required release acceptance (not yet inferred from unit tests)

1. Include native OAuth model selection UI/routes and the corrected probe wiring.
2. Verify custom Qwen activation resolves to native custom Chat Completions, not
   the official OpenAI Responses route. Verify switching Qwen → Codex → Qwen.
3. Ensure auxiliary memory/skill requests do not reintroduce the same local-only
   parameters or use the wrong transport.
4. Publish immutable runtime/Ads images and desktop artifacts including all
   relevant fixes. Keep prior candidate identifiers immutable.
5. On the resulting installed build: send two successive messages; verify final
   text, durable completion, enabled composer, model chip and persisted history.
6. Change the connected Codex model via the UI; verify the next new task uses the
   selected model without repeating OAuth. Do not silently change the user's
   selection during diagnostics.
7. Verify campaign list and drill-down against existing persisted Ads data.
8. Verify offer creation and a campaign proposal under the existing approval
   boundary. Publishing/activating campaigns and spending still need approval.
9. Meta remains a separate user setup/consent flow through the guided Composio UI;
   never claim Meta connected because the toolkit or app setup exists.

Tests using fake credentials/transports are regression coverage, not provider or
GUI acceptance. Live diagnostics and their exact scope must remain explicit.
