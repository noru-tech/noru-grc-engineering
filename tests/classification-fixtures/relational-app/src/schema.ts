import { pgTable as defineTable, text, integer, boolean, timestamp, jsonb } from "drizzle-orm/pg-core";
import type { ContactEnvelope as StoredContact, OpenEnvelope, AccountSubject } from "./contracts";

export const people = defineTable("people", {
  id: text("id").primaryKey(),
  email: text("email").notNull(),
  firstName: text("first_name").notNull(),
  lastName: text("last_name"),
  createdAt: timestamp("created_at").defaultNow(),
  accountEnabled: boolean("account_enabled"),
  contact: jsonb("contact").$type<StoredContact>(),
  openContact: jsonb("open_contact").$type<OpenEnvelope>(),
});

export const companies = defineTable("companies", {
  id: text("id").primaryKey(),
  registeredName: text("registered_name").notNull(),
  registryNumber: text("registry_number"),
});

export const assignments = defineTable("assignments", {
  id: text("id").primaryKey(),
  personOwner: text("person_owner").references(() => people.id),
  companyOwner: text("company_owner").references(() => companies.id),
  completedAt: timestamp("completed_at"),
  updatedAt: timestamp("updated_at").defaultNow(),
  source: text("source"),
});

export const integrations = defineTable("integrations", {
  id: text("id").primaryKey(),
  serviceCredential: text("service_credential"),
  personCredential: text("person_credential"),
  personId: text("person_id").references(() => people.id),
  authorizationState: text("authorization_state"),
});

export const ambiguousValues = defineTable("ambiguous_values", {
  id: text("id").primaryKey(),
  email: boolean("email"),
  fingerprint: text("fingerprint"),
  race: integer("race"),
  ownerId: text("owner_id"),
  opaque: text("opaque"),
  payload: jsonb("payload"),
  subject: jsonb("subject").$type<AccountSubject>(),
  emailDigest: text("email_digest"),
});
