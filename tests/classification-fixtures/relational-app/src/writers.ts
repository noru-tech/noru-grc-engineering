import { createHash } from "node:crypto";
import { people, companies, assignments, integrations, ambiguousValues } from "./schema";
import type { PersonalContact } from "./contracts";

declare const db: { insert(table: unknown): { values(row: unknown): unknown }; update(table: unknown): { set(row: unknown): unknown } };
declare function randomId(): string;

export function createPerson(contact: PersonalContact) {
  return db.insert(people).values({
    id: randomId(), email: contact.email, firstName: contact.firstName, lastName: contact.lastName,
    accountEnabled: true,
    contact: { primary: contact, addresses: [{ email: contact.email, verified: false }], checksum: "pending" },
    openContact: { contact, extension: {} },
  });
}

export function registerCompany(registeredName: string, registryNumber: string) {
  return db.insert(companies).values({ id: randomId(), registeredName, registryNumber });
}

export function completeAssignment(personId: string, companyId: string) {
  return db.insert(assignments).values({
    id: randomId(), personOwner: personId, companyOwner: companyId,
    completedAt: new Date(), source: "manual",
  });
}

export function rotateServiceCredential(credential: string) {
  return db.update(integrations).set({ serviceCredential: credential });
}

export function storeAuthorization(personId: string, credential: string, state: string) {
  return db.insert(integrations).values({ id: randomId(), personId, personCredential: credential, authorizationState: state });
}

export function storeBuildManifest(fileContents: string, raceNumber: number, externalValue: unknown) {
  return db.insert(ambiguousValues).values({
    id: randomId(), email: false,
    fingerprint: createHash("sha256").update(fileContents).digest("hex"), race: raceNumber,
    opaque: String(externalValue), payload: externalValue,
  });
}

export function suppressContact(contact: PersonalContact) {
  return db.insert(ambiguousValues).values({ id: randomId(), emailDigest: createHash("sha256").update(contact.email).digest("hex") });
}
