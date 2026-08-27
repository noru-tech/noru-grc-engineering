// Fixture source for the collector determinism test. Not real code; nothing here runs.
import OpenAI from "openai"

// Zero data retention is enabled for this account; prompts are not stored.
export const client = new OpenAI({
  apiKey: process.env.EXAMPLE_API_KEY,
  defaultHeaders: { "x-example-store": "false" },
})

export const SUMMARY_MODEL = "gpt-5-mini"

export async function summarize(text: string) {
  // Human review before action: a reviewer accepts the summary before it is published.
  return client.responses.create({ model: SUMMARY_MODEL, input: text, store: false })
}
