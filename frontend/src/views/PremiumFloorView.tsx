import { Users } from 'lucide-react'

import type { AgentRoster, AgentStat, AgentStatsResponse, RosterAgent, RosterDepartment, RuntimeStatus } from '../api/types'
import { activeAgentIds, groupDepartmentsByKind } from '../lib/agentRoster'
import { useT } from '../lib/i18n'
import { Badge, StatusDot } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { Stagger, StaggerItem, motion, useReducedMotion, SPRING } from '../components/ui/motion'

import styles from './PremiumFloorView.module.css'

// ── PremiumAgentTile ─────────────────────────────────────────────────────────

interface PremiumAgentTileProps {
  agent: RosterAgent
  isWorking: boolean
  todayActions?: number
  onClick: () => void
}

function PremiumAgentTile({ agent, isWorking, todayActions, onClick }: PremiumAgentTileProps) {
  const t = useT()
  const reduced = useReducedMotion()
  const initials = agent.name.charAt(0).toUpperCase()
  const isFactory = agent.source === 'ruflo'
  const hasBadges = agent.is_default || isFactory
  const ariaLabel = isWorking
    ? t('agents.card.aria').replace('{name}', agent.name)
    : t('agents.card.aria_idle').replace('{name}', agent.name)

  const Inner = (
    <article
      className={styles.tile}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      aria-label={ariaLabel}
    >
      <span className={styles.avatarWrap}>
        <span
          className={`${styles.avatarRing}${isWorking ? ` ${styles.avatarRingActive}` : ''}`}
          aria-hidden="true"
        />
        <span
          className={styles.avatar}
          style={{ background: agent.color ?? 'var(--color-accent)' }}
          aria-hidden="true"
        >
          {initials}
        </span>
      </span>

      <span className={styles.name}>{agent.name}</span>

      {hasBadges && (
        <span className={styles.badges}>
          {agent.is_default && <Badge variant="default">{t('agents.badge.default')}</Badge>}
          {isFactory && <Badge variant="success">{t('agents.badge.factory')}</Badge>}
        </span>
      )}

      <StatusDot
        state={isWorking ? 'warning' : 'success'}
        label={isWorking ? t('agents.status.working') : t('agents.status.online')}
      />

      {!!todayActions && todayActions > 0 && (
        <span className={styles.meta}>
          {todayActions === 1
            ? t('agents.premium.today_action').replace('{count}', '1')
            : t('agents.premium.today_action_pl').replace('{count}', String(todayActions))}
        </span>
      )}
    </article>
  )

  if (reduced) return Inner

  return (
    <motion.div
      whileHover={{ y: -2 }}
      whileTap={{ y: 0 }}
      transition={SPRING}
      style={{ borderRadius: 'var(--radius-lg)' }}
    >
      {Inner}
    </motion.div>
  )
}

// ── PremiumDeptSection ────────────────────────────────────────────────────────

interface PremiumDeptSectionProps {
  dept: RosterDepartment
  activeIds: Set<string>
  statsById: Map<string, AgentStat>
  agentStatsAvailable: boolean
  onAgentClick: (agent: RosterAgent) => void
  onCreateClick: () => void
  showCreateTile: boolean
}

function PremiumDeptSection({
  dept,
  activeIds,
  statsById,
  agentStatsAvailable,
  onAgentClick,
  onCreateClick,
  showCreateTile,
}: PremiumDeptSectionProps) {
  const t = useT()
  const headingId = `premium-dept-${dept.id}`

  return (
    <section aria-labelledby={headingId} className={styles.section}>
      <div className={styles.sectionHead}>
        <h2 id={headingId} className={styles.sectionTitle}>{dept.name}</h2>
        <span className={styles.sectionCount}>{dept.agents.length}</span>
        {dept.kind === 'factory' && (
          <span className={styles.sectionTag}>{t('agents.dept.factory.tag')}</span>
        )}
      </div>

      <Stagger>
        <ul className={styles.grid} role="list">
          {dept.agents.map((a) => (
            <StaggerItem key={a.id} style={{ listStyle: 'none' }}>
              <li style={{ listStyle: 'none' }}>
                <PremiumAgentTile
                  agent={a}
                  isWorking={activeIds.has(a.id)}
                  todayActions={agentStatsAvailable ? statsById.get(a.id)?.today.tasks : undefined}
                  onClick={() => onAgentClick(a)}
                />
              </li>
            </StaggerItem>
          ))}
          {showCreateTile && (
            <StaggerItem style={{ listStyle: 'none' }}>
              <li style={{ listStyle: 'none' }}>
                <button
                  type="button"
                  className={styles.createTile}
                  onClick={onCreateClick}
                  aria-label={t('agents.card.create.aria')}
                >
                  <span className={styles.createIcon} aria-hidden="true">+</span>
                  <span className={styles.createLabel}>{t('agents.card.create.label')}</span>
                </button>
              </li>
            </StaggerItem>
          )}
        </ul>
      </Stagger>
    </section>
  )
}

// ── PremiumFloorView (root) ───────────────────────────────────────────────────

export interface PremiumFloorViewProps {
  roster: AgentRoster
  runtimeStatus: RuntimeStatus
  agentStats: AgentStatsResponse
  hasRuflo: boolean
  onAgentClick: (agent: RosterAgent) => void
  onCreateClick: () => void
}

export function PremiumFloorView({ roster, runtimeStatus, agentStats, hasRuflo, onAgentClick, onCreateClick }: PremiumFloorViewProps) {
  const t = useT()
  const activeIds = activeAgentIds(runtimeStatus)
  const { cerebroDepts, customDepts, factoryDepts, hasCustomDepts } = groupDepartmentsByKind(roster.departments)
  const statsById = new Map(agentStats.agents.map((a) => [a.agent_id, a]))

  return (
    <div className={styles.premiumBody}>
      {cerebroDepts.map((dept) => (
        <PremiumDeptSection
          key={dept.id}
          dept={dept}
          activeIds={activeIds}
          statsById={statsById}
          agentStatsAvailable={agentStats.available}
          onAgentClick={onAgentClick}
          onCreateClick={onCreateClick}
          showCreateTile={false}
        />
      ))}

      {customDepts.map((dept, i) => (
        <PremiumDeptSection
          key={dept.id}
          dept={dept}
          activeIds={activeIds}
          statsById={statsById}
          agentStatsAvailable={agentStats.available}
          onAgentClick={onAgentClick}
          onCreateClick={onCreateClick}
          showCreateTile={i === 0}
        />
      ))}

      {!hasCustomDepts && (
        <section aria-labelledby="premium-mine" className={styles.section}>
          <h2 id="premium-mine" className={styles.sectionTitle}>{t('agents.dept.mine.title')}</h2>
          <EmptyState
            compact
            icon={<Users size={20} aria-hidden="true" />}
            title={t('agents.dept.mine.empty')}
            action={
              <Button type="button" variant="ghost" size="sm" onClick={onCreateClick}>
                + {t('agents.card.create.label')}
              </Button>
            }
          />
        </section>
      )}

      {factoryDepts.map((dept) => (
        <PremiumDeptSection
          key={dept.id}
          dept={dept}
          activeIds={activeIds}
          statsById={statsById}
          agentStatsAvailable={agentStats.available}
          onAgentClick={onAgentClick}
          onCreateClick={onCreateClick}
          showCreateTile={false}
        />
      ))}

      {hasRuflo && factoryDepts.length === 0 && (
        <section aria-labelledby="premium-swarm" className={styles.section}>
          <h2 id="premium-swarm" className={styles.sectionTitle}>{t('agents.dept.swarm.title')}</h2>
          <p className={styles.sectionDesc}>
            {t('agents.dept.swarm.desc')}
            {runtimeStatus.ruflo_active && (
              <StatusDot state="success" label={t('agents.dept.swarm.active')} />
            )}
          </p>
        </section>
      )}

      <p className={styles.attribution}>Lumen</p>
    </div>
  )
}
