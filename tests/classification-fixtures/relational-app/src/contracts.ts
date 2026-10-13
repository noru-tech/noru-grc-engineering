export interface PersonalContact {
  email: string;
  firstName: string;
  lastName: string;
}

export interface ContactEnvelope {
  primary: PersonalContact;
  addresses: Array<{ email: string; verified: boolean }>;
  checksum: string;
}

export interface OpenEnvelope {
  contact: PersonalContact;
  extension: Record<string, unknown>;
}

export type AccountSubject =
  | { kind: "person"; contact: PersonalContact }
  | { kind: "company"; registeredName: string };
