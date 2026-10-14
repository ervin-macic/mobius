/* An added `/btw` side question in the transcript: context, not an owner message. */

import { ChatTemporary } from '@openai/apps-sdk-ui/components/Icon'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import MarkerCard from './MarkerCard.jsx'

/**
 * The row's `side_question` holds the exact question and answer; `content` is
 * the same pair as plain text for the agent, search and compaction. The answer
 * opens by default because the owner kept it to read it.
 */
export default function SideQuestionNote({ msg }) {
  const { question = '', answer = '' } = msg?.side_question || {}
  return (
    <MarkerCard
      title="Side question"
      subtitle={question}
      icon={<ChatTemporary width={14} height={14} aria-hidden="true" />}
      defaultOpen
    >
      {answer ? <StandardMarkdown text={answer} /> : null}
    </MarkerCard>
  )
}
