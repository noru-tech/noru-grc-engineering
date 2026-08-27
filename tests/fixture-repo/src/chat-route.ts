// Fixture source for the collector. Not real code; nothing here runs.
//
// This is the Article 50(1) case on purpose: a chat surface a person types into, with NOTHING in
// this repository that tells them they are talking to an AI system. The collector must find the
// trigger AND report the missing disclosure — a scan that finds only the model call has failed at
// the half that is enforceable today.
import { client, SUMMARY_MODEL } from "./inference"

export async function chatRoute(question: string) {
  return client.responses.create({ model: SUMMARY_MODEL, input: question, store: false })
}
