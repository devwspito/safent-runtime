import { useEffect, useReducer, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import {
  getUsageSummary,
  getUsageByAgent,
  getUsageTimeseries,
} from '../api/client'
import type {
  UsageSummary,
  UsageByAgent,
  UsageTimeseries,
  UsagePeriod,
  UsageDimension,
} from '../api/types'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { Stagger, StaggerItem, FadeIn } from '../components/ui/motion'
import { useT, useLocale, type Locale } from '../lib/i18n'
import styles from './UsageView.module.css'

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatUSD(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`
  if (value > 0) return `$${value.toFixed(4)}`
  return '$0.00'
}

function formatNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function formatDay(day: string, locale: Locale): string {
  try {
    const d = new Date(day + 'T00:00:00')
    return d.toLocaleDateString(locale === 'en' ? 'en-US' : 'es-ES', { day: 'numeric', month: 'short' })
  } catch {
    return day
  }
}

// ── State machine ─────────────────────────────────────────────────────────────

interface UsageData {
  summary: UsageSummary
  byAgent: UsageByAgent
  timeseries: UsageTimeseries
}

type State =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'success'; data: UsageData; period: UsagePeriod; dimension: UsageDimension }

type Action =
  | { type: 'LOADED'; data: UsageData; period: UsagePeriod; dimension: UsageDimension }
  | { type: 'FAILED'; message: string }
  | { type: 'RELOAD' }

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case 'LOADED': return { status: 'success', data: action.data, period: action.period, dimension: action.dimension }
    case 'FAILED': return { status: 'error', message: action.message }
    case 'RELOAD': return { status: 'loading' }
  }
}

// ── Loading skeleton — mirrors final layout, not spinner-only ─────────────────

function LoadingSkeleton() {
  return (
    <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>
      {/* Hero stat row */}
      <StaggerItem>
        <div className={styles.skeletonHeroGrid}>
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className={`skeleton ${styles.skeletonHeroCard}`}
              style={{ animationDelay: `${i * 40}ms` }}
            />
          ))}
        </div>
      </StaggerItem>

      {/* Chart block */}
      <StaggerItem>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div className={`skeleton skeleton--line-sm ${styles.skeletonLabel}`} />
          <div className={`skeleton ${styles.skeletonChartBlock}`} style={{ animationDelay: '80ms' }} />
        </div>
      </StaggerItem>

      {/* Breakdown + governance */}
      <StaggerItem>
        <div className={styles.skeletonBreakdownGrid}>
          {[0, 1].map(col => (
            <div key={col} className={styles.skeletonSection}>
              <div className={`skeleton skeleton--line-sm ${styles.skeletonLabel}`} style={{ animationDelay: `${col * 30}ms` }} />
              {[...Array(4)].map((_, i) => (
                <div
                  key={i}
                  className="skeleton skeleton--block"
                  style={{ animationDelay: `${(col * 4 + i) * 35}ms` }}
                />
              ))}
            </div>
          ))}
        </div>
      </StaggerItem>

      <StaggerItem>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div className={`skeleton skeleton--line-sm ${styles.skeletonLabel}`} />
          <div className={styles.govGrid}>
            {[0, 1].map(i => (
              <div key={i} className={`skeleton ${styles.skeletonGovCard}`} style={{ animationDelay: `${i * 50}ms` }} />
            ))}
          </div>
        </div>
      </StaggerItem>
    </Stagger>
  )
}

// ── Period selector ───────────────────────────────────────────────────────────

interface PeriodSelectorProps {
  value: UsagePeriod
  onChange: (p: UsagePeriod) => void
  disabled: boolean
}

function PeriodSelector({ value, onChange, disabled }: PeriodSelectorProps) {
  const t = useT()
  const periodOptions: { value: UsagePeriod; label: string }[] = [
    { value: '7d', label: t('cost.period.7d') },
    { value: '30d', label: t('cost.period.30d') },
    { value: 'mtd', label: t('cost.period.mtd') },
  ]

  return (
    <div
      className="office-seg-toggle"
      role="group"
      aria-label={t('cost.period.aria')}
    >
      {periodOptions.map(opt => (
        <button
          key={opt.value}
          type="button"
          className={`office-seg-btn${value === opt.value ? ' office-seg-btn--active' : ''}`}
          onClick={() => onChange(opt.value)}
          disabled={disabled}
          aria-pressed={value === opt.value}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// ── Share bar ─────────────────────────────────────────────────────────────────

function ShareBar({ share, success = false }: { share: number; success?: boolean }) {
  const pct = Math.min(100, Math.max(0, share * 100))
  return (
    <div className={styles.shareTrack} aria-hidden="true">
      <div
        className={`${styles.shareFill}${success ? ` ${styles['shareFill--success']}` : ''}`}
        style={{ width: `${pct.toFixed(1)}%` }}
      />
    </div>
  )
}

// ── Hero stat card ─────────────────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: string
  suffix?: string
  highlight?: boolean
}

function StatCard({ label, value, suffix, highlight }: StatCardProps) {
  return (
    <div
      className={`${styles.statCard}${highlight ? ` ${styles['statCard--highlight']}` : ''}`}
      role="listitem"
    >
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue}>
        {value}
        {suffix && <span className={styles.statSuffix}>{suffix}</span>}
      </span>
    </div>
  )
}

// ── Chart tooltip ─────────────────────────────────────────────────────────────

interface ChartTooltipProps {
  active?: boolean
  payload?: Array<{ value: number; name: string }>
  label?: string
  dimension: UsageDimension
}

function ChartTooltip({ active, payload, label, dimension }: ChartTooltipProps) {
  const t = useT()
  const { locale } = useLocale()
  if (!active || !payload?.length) return null
  const value = payload[0]?.value ?? 0
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipDate}>{label ? formatDay(label, locale) : ''}</div>
      <div className={styles.tooltipValue}>
        {dimension === 'cost' ? formatUSD(value) : `${formatNumber(value)} ${t('cost.unit.actions')}`}
      </div>
    </div>
  )
}

// ── Agent ranking ──────────────────────────────────────────────────────────────

interface AgentRankingProps {
  byAgent: UsageByAgent
  onRowClick: (agentId: string) => void
}

function AgentRanking({ byAgent, onRowClick }: AgentRankingProps) {
  const t = useT()
  const agents = (byAgent.agents ?? []).slice().sort((a, b) => b.cost_usd - a.cost_usd)

  if (!byAgent.available || agents.length === 0) {
    return (
      <EmptyState
        icon={
          <svg width="28" height="28" viewBox="0 0 28 28" fill="none" aria-hidden="true">
            <circle cx="14" cy="10" r="5" stroke="currentColor" strokeWidth="1.5" />
            <path d="M5 24c0-4.418 4.03-8 9-8s9 3.582 9 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        }
        title={t('cost.agent.empty.title')}
        description={t('cost.agent.empty.desc')}
      />
    )
  }

  return (
    <ul className={`${styles.rankingList} stagger-list`} role="list">
      {agents.map(agent => (
        <li key={agent.agent_id}>
          <button
            type="button"
            className={`${styles.rankRow} ${styles['rankRow--clickable']} usage-agent-row`}
            onClick={() => onRowClick(agent.agent_id)}
            aria-label={t('cost.agent.row.aria').replace('{name}', agent.name)}
          >
            <div className={styles.rankRowInfo}>
              <div className={styles.rankRowName}>{agent.name}</div>
              <div className={styles.rankRowMeta}>
                <span className="num">{formatNumber(agent.cycles)}</span>
                {` ${t('cost.unit.actions')} · `}
                <span className="num">{(agent.share * 100).toFixed(0)}%</span>
              </div>
            </div>
            <ShareBar share={agent.share} />
            <span className={`${styles.rankRowCost} num`}>
              {formatUSD(agent.cost_usd)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}

// ── Model breakdown ────────────────────────────────────────────────────────────

interface ModelBreakdownProps {
  summary: UsageSummary
}

function ModelBreakdown({ summary }: ModelBreakdownProps) {
  const t = useT()
  const models = summary.top_models ?? []

  if (!summary.available || models.length === 0) {
    return (
      <EmptyState
        icon={
          <svg width="28" height="28" viewBox="0 0 28 28" fill="none" aria-hidden="true">
            <rect x="4" y="4" width="20" height="20" rx="4" stroke="currentColor" strokeWidth="1.5" />
            <path d="M9 16l3.5-3.5L16 16l3.5-4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        }
        title={t('cost.model.empty.title')}
        description={t('cost.model.empty.desc')}
      />
    )
  }

  return (
    <ul className={`${styles.rankingList} stagger-list`} role="list">
      {models.map(m => {
        const isSelfHosted = m.cost_usd === 0
        return (
          <li key={m.model}>
            <div className={styles.rankRow}>
              <div className={styles.rankRowInfo}>
                <div className={styles.rankRowName}>{m.model}</div>
                <div className={styles.rankRowMeta}>
                  <span className="num">{(m.share * 100).toFixed(0)}%</span>
                  {t('cost.model.share_suffix')}
                </div>
              </div>
              <ShareBar share={m.share} success={isSelfHosted} />
              <span className={`${styles.rankRowCost}${isSelfHosted ? ` ${styles['rankRowCost--selfhosted']}` : ''} num`}>
                {isSelfHosted ? t('cost.model.selfhosted') : formatUSD(m.cost_usd)}
              </span>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// ── Governance row ─────────────────────────────────────────────────────────────

interface GovernanceRowProps {
  summary: UsageSummary
}

function GovernanceRow({ summary }: GovernanceRowProps) {
  const t = useT()
  const failurePct = summary.cycles > 0
    ? ((summary.failures / summary.cycles) * 100).toFixed(1)
    : '0.0'
  const hasData = summary.available && summary.cycles > 0

  return (
    <div className={styles.govGrid}>
      <div className={styles.govCard}>
        <span className={styles.govCardLabel}>{t('cost.gov.failures.label')}</span>
        <span
          className={styles.govCardValue}
          style={{ color: summary.failures > 0 ? 'var(--color-warning)' : 'var(--color-text)' }}
        >
          {hasData ? formatNumber(summary.failures) : '—'}
        </span>
        {hasData && (
          <span className={styles.govCardNote}>
            <span className="num">{failurePct}%</span>
            {t('cost.gov.failures.note')}
          </span>
        )}
      </div>

      <div className={styles.govCard}>
        <span className={styles.govCardLabel}>{t('cost.gov.selfhosted.label')}</span>
        <span className={styles.govCardValue} style={{ color: 'var(--color-success)' }}>
          {hasData ? formatNumber(summary.self_hosted_cycles) : '—'}
        </span>
        {hasData && (
          <span className={styles.govCardNote}>{t('cost.gov.selfhosted.note')}</span>
        )}
      </div>
    </div>
  )
}

// ── Section title ──────────────────────────────────────────────────────────────

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className={styles.sectionLabel}>{children}</h2>
}

// ── Main view ─────────────────────────────────────────────────────────────────

export default function UsageView() {
  const navigate = useNavigate()
  const t = useT()
  const { locale } = useLocale()
  const [state, dispatch] = useReducer(reducer, { status: 'loading' })

  const currentPeriod: UsagePeriod = state.status === 'success' ? state.period : '30d'
  const currentDimension: UsageDimension = state.status === 'success' ? state.dimension : 'cost'

  const load = useCallback((period: UsagePeriod, dimension: UsageDimension) => {
    dispatch({ type: 'RELOAD' })
    Promise.all([
      getUsageSummary(period),
      getUsageByAgent(period),
      getUsageTimeseries(period, dimension),
    ]).then(([summary, byAgent, timeseries]) => {
      dispatch({
        type: 'LOADED',
        data: { summary, byAgent, timeseries },
        period,
        dimension,
      })
    }).catch((err: unknown) => {
      dispatch({
        type: 'FAILED',
        message: err instanceof Error ? err.message : t('cost.err.default'),
      })
    })
  }, [t])

  useEffect(() => { load(currentPeriod, currentDimension) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function handlePeriodChange(period: UsagePeriod) {
    load(period, currentDimension)
  }

  function handleDimensionToggle() {
    const next: UsageDimension = currentDimension === 'cost' ? 'tokens' : 'cost'
    if (state.status === 'success') {
      load(state.period, next)
    }
  }

  function handleAgentRowClick(_agentId: string) {
    navigate('/agentes')
  }

  const isLoading = state.status === 'loading'

  return (
    <>
      <PageHeader
        title={t('cost.title')}
        subtitle={t('cost.subtitle')}
        actions={
          <div className={styles.controlsRow}>
            <PeriodSelector
              value={currentPeriod}
              onChange={handlePeriodChange}
              disabled={isLoading}
            />
          </div>
        }
      />

      <div className="view-body cv-view-body page-enter">

        {/* ── Loading ── */}
        {state.status === 'loading' && <LoadingSkeleton />}

        {/* ── Error ── */}
        {state.status === 'error' && (
          <FadeIn>
            <div className={styles.errorState} role="alert">
              <svg width="40" height="40" viewBox="0 0 40 40" fill="none" aria-hidden="true" style={{ color: 'var(--color-danger)', opacity: 0.7 }}>
                <circle cx="20" cy="20" r="17" stroke="currentColor" strokeWidth="1.5" />
                <path d="M20 13v8M20 27h.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
              <p className={styles.errorMessage}>{state.message}</p>
              <Button variant="secondary" onClick={() => load(currentPeriod, currentDimension)}>
                {t('cost.retry')}
              </Button>
            </div>
          </FadeIn>
        )}

        {/* ── Success ── */}
        {state.status === 'success' && (() => {
          const { summary, byAgent, timeseries } = state.data
          const noData = !summary.available || (summary.cycles === 0 && summary.total_cost_usd === 0)
          const chartPoints = (timeseries.points ?? []).map(p => ({
            ...p,
            day: p.day,
            value: state.dimension === 'cost' ? p.cost_usd : p.cycles,
          }))

          if (noData) {
            return (
              <EmptyState
                icon={
                  <svg width="36" height="36" viewBox="0 0 36 36" fill="none" aria-hidden="true">
                    <rect x="4" y="4" width="28" height="28" rx="5" stroke="currentColor" strokeWidth="1.5" />
                    <path d="M10 24l7-7 5 5 7-9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                }
                title={t('cost.empty.title')}
                description={t('cost.empty.desc')}
              />
            )
          }

          return (
            <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

              {/* ── 1. Hero row ── */}
              <StaggerItem>
                <div
                  className={styles.heroGrid}
                  role="list"
                  aria-label={t('cost.summary.aria')}
                >
                  <StatCard
                    label={t('cost.kpi.spend')}
                    value={formatUSD(summary.total_cost_usd)}
                    highlight
                  />
                  <StatCard
                    label={t('cost.kpi.projection')}
                    value={formatUSD(summary.projected_cost_usd)}
                    suffix={t('cost.kpi.projection_suffix')}
                  />
                  <StatCard
                    label={t('cost.kpi.activity')}
                    value={formatNumber(summary.cycles)}
                    suffix={t('cost.unit.actions')}
                  />
                  <StatCard
                    label={t('cost.kpi.issues')}
                    value={formatNumber(summary.failures)}
                  />
                </div>
              </StaggerItem>

              {/* ── 2. Time series chart ── */}
              <StaggerItem>
                <section aria-label={t('cost.chart.spend_over_time')}>
                  <div className={styles.sectionHeader}>
                    <SectionTitle>
                      {state.dimension === 'cost' ? t('cost.chart.spend_over_time') : t('cost.chart.activity_over_time')}
                    </SectionTitle>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={handleDimensionToggle}
                      aria-label={t('cost.chart.toggle_aria').replace(
                        '{mode}',
                        state.dimension === 'cost' ? t('cost.chart.mode.activity') : t('cost.chart.mode.spend'),
                      )}
                    >
                      {state.dimension === 'cost' ? t('cost.chart.view_activity') : t('cost.chart.view_spend')}
                    </Button>
                  </div>

                  <div className={styles.chartCard}>
                    {chartPoints.length === 0 ? (
                      <div className={styles.chartEmpty}>
                        {t('cost.chart.empty')}
                      </div>
                    ) : (
                      <ResponsiveContainer width="100%" height={220}>
                        <AreaChart data={chartPoints} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                          <defs>
                            <linearGradient id="usageGradient" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="var(--color-accent)" stopOpacity={0.22} />
                              <stop offset="95%" stopColor="var(--color-accent)" stopOpacity={0.02} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid
                            strokeDasharray="3 3"
                            stroke="var(--color-border-subtle)"
                            vertical={false}
                          />
                          <XAxis
                            dataKey="day"
                            tickFormatter={(d: string) => formatDay(d, locale)}
                            tick={{ fontSize: 11, fill: 'var(--color-text-dim)', fontFamily: 'var(--font-ui)' }}
                            axisLine={false}
                            tickLine={false}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            tickFormatter={v =>
                              state.dimension === 'cost' ? formatUSD(v as number) : formatNumber(v as number)
                            }
                            tick={{ fontSize: 11, fill: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}
                            axisLine={false}
                            tickLine={false}
                            width={state.dimension === 'cost' ? 58 : 44}
                          />
                          <Tooltip
                            content={<ChartTooltip dimension={state.dimension} />}
                            cursor={{ stroke: 'var(--color-border)', strokeWidth: 1 }}
                          />
                          <Area
                            type="monotone"
                            dataKey="value"
                            stroke="var(--color-accent)"
                            strokeWidth={2}
                            fill="url(#usageGradient)"
                            dot={false}
                            activeDot={{ r: 4, fill: 'var(--color-accent)', strokeWidth: 0 }}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </section>
              </StaggerItem>

              {/* ── 3. Two-column: by agent + by model ── */}
              <StaggerItem>
                <div className={styles.breakdownGrid}>
                  <section aria-label={t('cost.section.by_employee.aria')}>
                    <div className={styles.sectionHeader}>
                      <SectionTitle>{t('cost.section.by_employee.title')}</SectionTitle>
                    </div>
                    <AgentRanking byAgent={byAgent} onRowClick={handleAgentRowClick} />
                  </section>

                  <section aria-label={t('cost.section.by_model.aria')}>
                    <div className={styles.sectionHeader}>
                      <SectionTitle>{t('cost.section.by_model.title')}</SectionTitle>
                    </div>
                    <ModelBreakdown summary={summary} />
                  </section>
                </div>
              </StaggerItem>

              {/* ── 4. Governance row ── */}
              <StaggerItem>
                <section aria-label={t('cost.section.governance.aria')}>
                  <div className={styles.sectionHeader}>
                    <SectionTitle>{t('cost.section.governance.title')}</SectionTitle>
                  </div>
                  <GovernanceRow summary={summary} />
                </section>
              </StaggerItem>

            </Stagger>
          )
        })()}

      </div>
    </>
  )
}
