# SnapKey Vision AI target architecture

## Identity hierarchy

All new cloud-facing contracts use:

tenant_id -> company_code -> shop_id -> edge_id -> camera_id

The legacy site_id/store_id fields remain supported during migration. Do not delete or rename them until all deployed edge agents have moved to the versioned contracts.

## Inference modes

- LOCAL_CPU is the mandatory baseline and offline-safe fallback.
- LOCAL_GPU is optional and registered only when a supported local runtime is detected.
- CLOUD_GPU is optional and requires an explicit authenticated endpoint.
- Edge event/rule logic consumes normalized InferenceResult objects rather than backend-specific Ultralytics objects.

Cloud inference receives sampled frames, not direct customer RTSP credentials. Private RTSP cameras remain reachable through the customer edge agent. A future secure relay may be used for on-demand live viewing, but RTSP URLs must not be published to the cloud.

## Missing later local work

The GitHub repository does not contain the later camera-automation-fixed jewellery-tag/proximity/compound-threat implementation. This branch therefore preserves an explicit integration boundary instead of inventing that code. When the real source is recovered, port it as a separate reviewed change.

## Rollout order

1. Run existing production tests unchanged.
2. Run domain/backend contract tests.
3. Shadow the normalized CPU adapter beside the legacy CameraManager path.
4. Enable local GPU per edge after capability checks.
5. Enable cloud GPU only for tenants explicitly configured for it.
6. Migrate event producers to EventPublisher incrementally.
7. Deploy cloud persistence/observability before enabling fleet-wide remote inference.
