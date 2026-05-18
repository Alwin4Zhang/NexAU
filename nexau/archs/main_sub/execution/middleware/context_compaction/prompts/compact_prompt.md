You have been working on the task described above but have not yet completed it. Write a continuation summary that will allow you (or another instance of yourself) to resume work efficiently in a future context window where the conversation history will be replaced with this summary. Your summary should be structured, actionable, and faithful to what the user actually said. Include:

1. Task Overview
   The user's core request and success criteria
   Any clarifications or constraints they specified

2. Current State
   What has been completed so far
   Files created, modified, or analyzed (with paths if relevant)
   Key outputs or artifacts produced

3. Important Discoveries
   Technical constraints or requirements uncovered
   Decisions made and their rationale
   Errors encountered and how they were resolved
   What approaches were tried that didn't work (and why)

4. Next Steps
   Specific actions needed to complete the task
   Any blockers or open questions to resolve
   Priority order if multiple steps remain

5. Context to Preserve
   User preferences or style requirements
   Domain-specific details that aren't obvious
   Any promises made to the user

6. All User Messages (verbatim)
   List EVERY user message from the conversation above, in chronological order, quoting each one VERBATIM.
   - Do NOT paraphrase, summarize, translate, or shorten user messages — quote the exact original text.
   - Do NOT omit any user message, even if it seems short, off-topic, redundant, corrective, or already covered in section 1. These messages are critical for understanding the user's feedback and changing intent — small corrections and rephrasings often carry the real signal.
   - Do NOT include tool results, assistant messages, or framework-injected system reminders here — only messages that originated from the human user.
   - Do NOT include THIS message itself (the current summarization instruction you are reading right now). It is not a user request — it is a system-issued summary request. Section 6 must end with the last real user turn, not with this instruction.
   - Do NOT include the conversation's `role=system` message (typically starting with text like "You are a ... agent ...", containing agent setup, available tools, runtime environment, or behavioral mandates). That message describes the AGENT's configuration, not a user turn — exclude it entirely from Section 6 even when it contains imperative language directed at the agent.
   - If a prior summary already contains a Section 6 listing earlier user messages verbatim, COPY ONLY THE BULLET ENTRIES THEMSELVES (the lines starting with `- [user turn N]: "..."`) into your own Section 6 first (preserving their original turn indices and quoted text), then APPEND any new user messages that occurred after that prior summary. Do NOT quote the prior summary's preamble or handoff text (e.g. anything starting with "Another language model started to solve this problem...") as its own user turn — that text is summary-framing, not a user message. Never re-summarize or paraphrase entries that were already preserved verbatim by a previous summary.
   - Format each entry as:
     - [user turn N]: "<exact original text>"
     where N is the 1-based index of that user turn within the conversation.
   - This section is REQUIRED and must be complete. A summary missing any user message — or one that paraphrases a previously preserved verbatim entry — is considered incomplete.

Sections 1–5 should be concise but complete; section 6 must contain every user message verbatim. Err on the side of including information that would prevent duplicate work or repeated mistakes. Write in a way that enables immediate resumption of the task.
