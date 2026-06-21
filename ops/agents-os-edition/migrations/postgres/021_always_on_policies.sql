-- migration: spec 003 T025 - data-model entidad 14 AlwaysOnPolicy.

CREATE TABLE IF NOT EXISTS agents_os.always_on_policies (
  policy_id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id            uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  applied_at                      timestamptz NOT NULL DEFAULT now(),
  suspend_targets_masked          boolean NOT NULL DEFAULT true,
  logind_handle_lid_switch        text NOT NULL DEFAULT 'ignore',
  logind_handle_power_key         text NOT NULL DEFAULT 'ignore',
  -- INVARIANTE no negociable (FR-042 + CHECK).
  screen_lock_pauses_agent        boolean NOT NULL DEFAULT false
    CHECK (screen_lock_pauses_agent = false),
  restart_policy_critical         text NOT NULL DEFAULT 'always_with_backoff',
  memory_protection_oom_score_adj int NOT NULL DEFAULT -500,
  cgroup_memory_min_bytes         bigint NOT NULL DEFAULT 536870912,
  drain_required_before_ota       boolean NOT NULL DEFAULT true,
  pending_work_queue_persistent   boolean NOT NULL DEFAULT true,
  agent_activity_indicator_visible boolean NOT NULL DEFAULT true
);

-- Una policy aplicada por nodo a la vez (append-only para histórico).
CREATE INDEX IF NOT EXISTS idx_aop_node_applied
  ON agents_os.always_on_policies (node_installation_id, applied_at DESC);
