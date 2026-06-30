/**
 * Motion system — reusable animation primitives for the Lumen UI.
 *
 * Design intent: physical, delicate transitions that feel native.
 * Every animation respects prefers-reduced-motion via Motion's built-in
 * useReducedMotion hook.  When reduced motion is requested, animated
 * elements appear immediately at their final state.
 */
import {
  motion,
  AnimatePresence,
  useReducedMotion,
  type Variants,
  type HTMLMotionProps,
  type Transition,
} from 'motion/react'
import { type ReactNode, forwardRef, type HTMLAttributes } from 'react'

// ── Shared transition tokens ──────────────────────────────────────────────────

/**
 * Soft spring — used for layout changes, hover lifts, and interactive feedback.
 * Feels physical without being bouncy.
 */
export const SPRING: Transition = {
  type: 'spring',
  stiffness: 420,
  damping: 34,
  mass: 0.7,
}

/**
 * Gentle tween — used for fade-ins, enter/exit overlays, and delayed entrances.
 * Matches the CSS --ease token (0.4, 0, 0.2, 1).
 */
export const TWEEN: Transition = {
  type: 'tween',
  ease: [0.4, 0, 0.2, 1],
  duration: 0.22,
}

/** Faster tween for very short transitions (badge swaps, micro-copy). */
export const TWEEN_FAST: Transition = {
  type: 'tween',
  ease: [0.4, 0, 0.2, 1],
  duration: 0.15,
}

// ── Stagger orchestration ─────────────────────────────────────────────────────

const staggerContainerVariants: Variants = {
  hidden: {},
  show: {
    transition: {
      staggerChildren: 0.04,
      delayChildren: 0.02,
    },
  },
}

const staggerItemVariants: Variants = {
  hidden: { opacity: 0, y: 10 },
  show: {
    opacity: 1,
    y: 0,
    transition: { ...SPRING },
  },
}

interface StaggerProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
}

/**
 * Wraps a list/section so children animate in with a staggered offset.
 * Pair with <StaggerItem> for each direct child.
 */
export function Stagger({ children, className, ...rest }: StaggerProps) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className} {...rest}>{children}</div>

  return (
    <motion.div
      className={className}
      variants={staggerContainerVariants}
      initial="hidden"
      animate="show"
      {...(rest as HTMLMotionProps<'div'>)}
    >
      {children}
    </motion.div>
  )
}

/** Single item inside a <Stagger> container. */
export function StaggerItem({ children, className, ...rest }: StaggerProps) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className} {...rest}>{children}</div>

  return (
    <motion.div
      className={className}
      variants={staggerItemVariants}
      {...(rest as HTMLMotionProps<'div'>)}
    >
      {children}
    </motion.div>
  )
}

// ── Fade-in (single element, no stagger context required) ─────────────────────

interface FadeInProps {
  children: ReactNode
  className?: string
  delay?: number
}

export function FadeIn({ children, className, delay = 0 }: FadeInProps) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className}>{children}</div>

  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...SPRING, delay }}
    >
      {children}
    </motion.div>
  )
}

// ── Animated list item (AnimatePresence-compatible) ───────────────────────────

interface AnimatedListItemProps extends HTMLAttributes<HTMLLIElement> {
  children: ReactNode
}

/**
 * A <li> that fades + slides in on mount and fades out on unmount.
 * Wrap a list in <AnimatePresence> and use this for each item.
 */
export const AnimatedListItem = forwardRef<HTMLLIElement, AnimatedListItemProps>(
  function AnimatedListItem({ children, ...rest }, ref) {
    const reduced = useReducedMotion()
    if (reduced) return <li ref={ref} {...rest}>{children}</li>

    return (
      <motion.li
        ref={ref}
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0, transition: SPRING }}
        exit={{ opacity: 0, y: -4, transition: TWEEN_FAST }}
        layout
        {...(rest as HTMLMotionProps<'li'>)}
      >
        {children}
      </motion.li>
    )
  },
)

// ── Page header title/subtitle stagger ───────────────────────────────────────

const headerTitleVariants: Variants = {
  hidden: { opacity: 0, y: -6 },
  show: { opacity: 1, y: 0, transition: SPRING },
}

const headerSubtitleVariants: Variants = {
  hidden: { opacity: 0, y: -4 },
  show: { opacity: 1, y: 0, transition: { ...SPRING, delay: 0.06 } },
}

interface AnimatedPageHeaderTextProps {
  title: string
  subtitle?: string
}

export function AnimatedPageHeaderText({ title, subtitle }: AnimatedPageHeaderTextProps) {
  const reduced = useReducedMotion()

  if (reduced) {
    return (
      <>
        <h1 className="view-title">{title}</h1>
        {subtitle && <p className="view-subtitle">{subtitle}</p>}
      </>
    )
  }

  return (
    <motion.div initial="hidden" animate="show">
      <motion.h1 className="view-title" variants={headerTitleVariants}>
        {title}
      </motion.h1>
      {subtitle && (
        <motion.p className="view-subtitle" variants={headerSubtitleVariants}>
          {subtitle}
        </motion.p>
      )}
    </motion.div>
  )
}

// ── Hoverable row wrapper ─────────────────────────────────────────────────────

interface HoverRowProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
  /** Extra CSS class names */
  className?: string
}

/**
 * Wraps any row element with a spring-powered whileHover lift.
 * The CSS hover styles (border, shadow) remain in CSS — motion only
 * handles the y-translate so we don't duplicate shadow logic.
 */
export const HoverRow = forwardRef<HTMLDivElement, HoverRowProps>(
  function HoverRow({ children, className, ...rest }, ref) {
    const reduced = useReducedMotion()
    if (reduced) {
      return (
        <div ref={ref} className={className} {...rest}>
          {children}
        </div>
      )
    }

    return (
      <motion.div
        ref={ref}
        className={className}
        whileHover={{ y: -2 }}
        transition={SPRING}
        {...(rest as HTMLMotionProps<'div'>)}
      >
        {children}
      </motion.div>
    )
  },
)

// ── Animated drawer (slide from right + backdrop fade) ───────────────────────

interface AnimatedDrawerProps {
  open: boolean
  children: ReactNode
  onBackdropClick: () => void
  width?: number
  label: string
}

/**
 * AnimatePresence-driven drawer.
 * Backdrop fades; panel slides in from the right with spring physics.
 */
export function AnimatedDrawer({ open, children, onBackdropClick, width = 400, label }: AnimatedDrawerProps) {
  const reduced = useReducedMotion()

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="office-drawer-backdrop ds-drawer-backdrop"
          onClick={e => { if (e.target === e.currentTarget) onBackdropClick() }}
          aria-modal="true"
          role="dialog"
          aria-label={label}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={TWEEN}
        >
          <motion.div
            className="office-drawer ds-drawer"
            style={{ maxWidth: width }}
            initial={reduced ? false : { x: 48, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={reduced ? { opacity: 0 } : { x: 48, opacity: 0 }}
            transition={SPRING}
          >
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── Animated empty state with icon glow pulse ─────────────────────────────────

interface AnimatedEmptyStateProps {
  icon: ReactNode
  title: string
  description?: string
  action?: ReactNode
  /** Tighter vertical padding for an empty SUB-section (not a full-page empty). */
  compact?: boolean
}

export function AnimatedEmptyState({ icon, title, description, action, compact }: AnimatedEmptyStateProps) {
  const reduced = useReducedMotion()

  return (
    <motion.div
      className={compact ? 'ds-empty-state ds-empty-state--compact' : 'ds-empty-state'}
      initial={reduced ? false : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...SPRING, delay: 0.05 }}
    >
      <motion.span
        className="ds-empty-state__icon"
        aria-hidden="true"
        animate={reduced ? undefined : {
          filter: [
            'drop-shadow(0 0 0px rgba(10, 132, 255, 0))',
            'drop-shadow(0 0 6px rgba(10, 132, 255, 0.28))',
            'drop-shadow(0 0 0px rgba(10, 132, 255, 0))',
          ],
        }}
        transition={reduced ? undefined : {
          duration: 3.2,
          repeat: Infinity,
          ease: 'easeInOut',
          delay: 0.6,
        }}
      >
        {icon}
      </motion.span>
      <div className="ds-empty-state__text">
        <p className="ds-empty-state__title">{title}</p>
        {description && <p className="ds-empty-state__desc">{description}</p>}
      </div>
      {action && <div className="ds-empty-state__action">{action}</div>}
    </motion.div>
  )
}

// ── Animated accordion / expander content ────────────────────────────────────

interface AnimatedExpanderContentProps {
  open: boolean
  children: ReactNode
}

/**
 * Height-animated content area for accordion/expander patterns.
 * Wrap the collapsible body in this; control `open` from outside.
 */
export function AnimatedExpanderContent({ open, children }: AnimatedExpanderContentProps) {
  const reduced = useReducedMotion()

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="content"
          initial={reduced ? false : { height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
          transition={{ height: SPRING, opacity: TWEEN_FAST }}
          style={{ overflow: 'hidden' }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── Animated chevron (rotate on open) ────────────────────────────────────────

interface AnimatedChevronProps {
  open: boolean
  size?: number
  className?: string
}

export function AnimatedChevron({ open, size = 13, className }: AnimatedChevronProps) {
  const reduced = useReducedMotion()
  // Import ChevronRight at call site; we receive size and apply it via motion
  return (
    <motion.span
      aria-hidden="true"
      className={className}
      animate={reduced ? undefined : { rotate: open ? 90 : 0 }}
      transition={TWEEN}
      style={{ display: 'inline-flex', flexShrink: 0, fontSize: size }}
    >
      ▸
    </motion.span>
  )
}

// Re-export AnimatePresence so consumers import from one place
export { AnimatePresence, motion, useReducedMotion }
