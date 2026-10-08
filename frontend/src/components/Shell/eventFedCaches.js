import {
  modelQueries, authQueries, appSourceQueries, chatAppArtifactQueries,
} from '../../hooks/queries.js'
import { invalidateAllChatActivity } from '../ChatView/chatActivityQueries.js'

/**
 * Every system-stream open closes the fetch-before-subscribe gap, including
 * the first open and Shell remounts. Mount-time reads can have taken their
 * snapshot before the server registered the listener, even on this page.
 * Without domain revisions we cannot safely skip their reconciliation.
 * Chat and app lists have their own durable reads in the open barrier.
 */
export function invalidateEventFedCachesOnOpen(queryClient) {
  return [
    modelQueries.registry.invalidate(queryClient),
    authQueries.provider.statuses.invalidate(queryClient),
    appSourceQueries.invalidate(queryClient),
    chatAppArtifactQueries.invalidateAll(queryClient),
    invalidateAllChatActivity(queryClient),
    queryClient.invalidateQueries({ queryKey: ['projects', 'files'] }),
    queryClient.invalidateQueries({ queryKey: ['projects', 'git'] }),
  ]
}
