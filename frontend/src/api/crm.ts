import { ApiError } from './client'
import { token } from '../lib/token'

export interface CrmConnection {
  id: string
  name: string
  operations: { method: 'GET'; path: string }[]
  read_only: true
}
export interface CrmInventory {
  connections: CrmConnection[]
  context: string
  limit: number
}
export interface CrmRead {
  context: string
  data: unknown
  operation: { method: 'GET'; path: string }
  untrusted_external_data: true
}

// Read requests can contact a customer's CRM. Never replay automatically,
// including after auth failure. The owner may explicitly review and retry.
async function crmRequest<T>(
  path: string,
  signal: AbortSignal,
  payload?: unknown,
): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timeout = setTimeout(abort, 25_000)
  try {
    const bearer = token()
    const response = await fetch(`/api/v1/crm${path}`, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
      },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      cache: 'no-store',
      signal: controller.signal,
    })
    const body: unknown = await response.json().catch(() => null)
    if (controller.signal.aborted)
      throw new DOMException('Aborted', 'AbortError')
    if (!response.ok)
      throw new ApiError('CRM request failed', response.status, body)
    return body as T
  } finally {
    clearTimeout(timeout)
    signal.removeEventListener('abort', abort)
  }
}

export async function listCrmConnections(
  signal: AbortSignal,
): Promise<CrmInventory> {
  const result = await crmRequest<CrmInventory>('', signal)
  if (
    !result ||
    !/^[a-f0-9]{64}$/.test(result.context) ||
    !Array.isArray(result.connections) ||
    result.connections.some(
      (item) =>
        !item ||
        typeof item.id !== 'string' ||
        typeof item.name !== 'string' ||
        item.read_only !== true ||
        !Array.isArray(item.operations) ||
        item.operations.some(
          (op) => op.method !== 'GET' || typeof op.path !== 'string',
        ),
    )
  ) {
    throw new ApiError('Invalid CRM inventory', 502, {
      detail: { code: 'crm_response_invalid' },
    })
  }
  return result
}

export async function readCrm(
  connection: CrmConnection,
  path: string,
  context: string,
  signal: AbortSignal,
): Promise<CrmRead> {
  if (
    !connection.operations.some((op) => op.method === 'GET' && op.path === path)
  ) {
    throw new ApiError('Unknown CRM operation', 403, {
      detail: { code: 'crm_forbidden' },
    })
  }
  const result = await crmRequest<CrmRead>(
    `/${encodeURIComponent(connection.id)}/read`,
    signal,
    { context, path },
  )
  if (
    !result ||
    result.context !== context ||
    result.untrusted_external_data !== true ||
    result.operation?.method !== 'GET' ||
    result.operation.path !== path ||
    !Object.prototype.hasOwnProperty.call(result, 'data')
  ) {
    throw new ApiError('Invalid CRM response', 502, {
      detail: { code: 'crm_response_invalid' },
    })
  }
  return result
}
