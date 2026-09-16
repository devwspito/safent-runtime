import type { ComposioApp } from '../api/types'

const toolkitNames: Record<string, string> = {
  gmail: 'Gmail', googleads: 'Google Ads', metaads: 'Meta Ads', googledrive: 'Google Drive',
  googlecalendar: 'Google Calendar', googlesheets: 'Google Sheets',
}

/** Use catalog names when available, including when only a connection was returned. */
export function composioAppName(app: ComposioApp): string {
  return app.name?.trim() || toolkitNames[app.slug]
    || app.slug.replace(/[_-]+/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase())
}
