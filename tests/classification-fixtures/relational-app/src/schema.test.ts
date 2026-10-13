import { pgTable, text } from "drizzle-orm/pg-core";
export const testOnly = pgTable("test_only", { email: text("email") });
