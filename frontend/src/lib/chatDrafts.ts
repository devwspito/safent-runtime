import type { Skill } from '../api/types'
import type { BridgeSelection } from './folderBridge'

export interface PendingAttachment {
  id: string
  file: File
  uploading: boolean
  uploadedPath: string | null
  error: boolean
}

interface DraftState {
  text: string
  attachments: PendingAttachment[]
  selectedSkills: Skill[]
  bridge: BridgeSelection | null
  bridgeBusy: boolean
  bridgeSyncing: boolean
}

/** A draft is memory-only. File handles and private text never enter web storage.
 * Async upload callbacks retain this object, not whichever thread is on screen. */
export class ChatDraft {
  private state: DraftState = {
    text: '', attachments: [], selectedSkills: [], bridge: null, bridgeBusy: false, bridgeSyncing: false,
  }
  private listeners = new Set<() => void>()
  constructor(readonly key: string) {}
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  set<K extends keyof DraftState>(field: K, value: DraftState[K] | ((previous: DraftState[K]) => DraftState[K])) {
    const next = typeof value === 'function'
      ? (value as (previous: DraftState[K]) => DraftState[K])(this.state[field]) : value
    this.state = { ...this.state, [field]: next }
    this.listeners.forEach(listener => listener())
  }
}

/** Owned by Layout so route navigation does not discard drafts. Historical
 * threads have an unknown agent until the API provides a verified binding;
 * never reuse useChat's possibly stale previous agent as their namespace. */
export class ChatDrafts {
  private newDrafts = new Map<string | null, ChatDraft>()
  private conversations = new Map<string, { agentId: string | null | undefined; draft: ChatDraft }>()
  private sequence = 0
  forNew(agentId: string | null): ChatDraft {
    let draft = this.newDrafts.get(agentId)
    if (!draft) {
      draft = new ChatDraft(JSON.stringify(['new', agentId, ++this.sequence]))
      this.newDrafts.set(agentId, draft)
    }
    return draft
  }
  forConversation(conversationId: string): ChatDraft {
    let entry = this.conversations.get(conversationId)
    if (!entry) {
      entry = { agentId: undefined, draft: new ChatDraft(JSON.stringify(['conversation', conversationId, 'agent-unknown'])) }
      this.conversations.set(conversationId, entry)
    }
    return entry.draft
  }
  bind(conversationId: string, agentId: string | null, draft: ChatDraft): ChatDraft {
    const existing = this.conversations.get(conversationId)
    if (existing && existing.draft !== draft) return existing.draft
    this.conversations.set(conversationId, { agentId, draft })
    if (this.newDrafts.get(agentId) === draft) this.newDrafts.delete(agentId)
    return draft
  }
}
