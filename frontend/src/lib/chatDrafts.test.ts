import { expect, it } from 'vitest'
import { ChatDrafts } from './chatDrafts'

it('retains the original async target after binding a new draft to a conversation', () => {
  const drafts = new ChatDrafts()
  const original = drafts.forNew('agent-a')
  drafts.bind('conversation-a', 'agent-a', original)
  const next = drafts.forNew('agent-a')
  original.set('text', 'Late update for original thread')
  expect(next.getSnapshot().text).toBe('')
  expect(drafts.forConversation('conversation-a').getSnapshot().text).toBe('Late update for original thread')
})

it('does not overwrite an existing historical draft when a binding collides', () => {
  const drafts = new ChatDrafts()
  const existing = drafts.forConversation('existing')
  existing.set('text', 'Keep this')
  const pending = drafts.forNew('agent-a')
  pending.set('text', 'Different context')
  expect(drafts.bind('existing', 'agent-a', pending)).toBe(existing)
  expect(drafts.forConversation('existing').getSnapshot().text).toBe('Keep this')
  expect(drafts.forNew('agent-a').getSnapshot().text).toBe('Different context')
})
