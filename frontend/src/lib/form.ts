import "server-only";
import { ApiError } from "./api";
import type { FormState } from "./types";

export function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

// Turns a thrown error into form state. `values` repopulates inputs (never pass passwords).
export function failure(error: unknown, values?: Record<string, string>): FormState {
  if (error instanceof ApiError) {
    return { error: error.message, fieldErrors: error.fieldErrors, values };
  }
  return { error: "Something went wrong. Please try again.", values };
}
