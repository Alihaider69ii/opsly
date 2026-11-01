"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { EmailClassification, EmailTemplate } from "@/lib/types";
import { createTemplate, previewTemplate, setTemplateStatus, updateTemplate } from "../actions";

const input = "w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-sm dark:border-zinc-700";
const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

export function TemplateForm({ existing }: { existing?: EmailTemplate }) {
  const router = useRouter();
  const [name, setName] = useState(existing?.name ?? "");
  const [type, setType] = useState<EmailClassification>(existing?.type ?? "LIFECYCLE");
  const [subject, setSubject] = useState(existing?.subject ?? "");
  const [preheader, setPreheader] = useState(existing?.preheader ?? "");
  const [bodyHtml, setBodyHtml] = useState(existing?.body_html ?? "<p>Hi {{customer.first_name}},</p>");
  const [bodyText, setBodyText] = useState(existing?.body_text ?? "");
  const [variables, setVariables] = useState(existing?.variables?.join(", ") ?? "customer.first_name");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ subject: string; html: string } | null>(null);

  async function save() {
    setPending(true);
    setError(null);
    const input = {
      name,
      type,
      subject,
      preheader: preheader || null,
      body_html: bodyHtml,
      body_text: bodyText || null,
      variables: variables.split(",").map((v) => v.trim()).filter(Boolean),
    };
    const result = existing ? await updateTemplate(existing.id, input) : await createTemplate(input);
    setPending(false);
    if (!result.ok) {
      setError(result.state?.error ?? "Something went wrong.");
      return;
    }
    if (!existing) router.push(`/email/templates/${result.data.id}`);
    else router.refresh();
  }

  async function doPreview() {
    if (!existing) return;
    const result = await previewTemplate(existing.id, {
      customer: { first_name: "Sam", email: "sam@example.com" },
    });
    if (result.ok) setPreview({ subject: result.data.subject, html: result.data.html });
  }

  async function toggleStatus() {
    if (!existing) return;
    const next = existing.status === "ACTIVE" ? "ARCHIVED" : "ACTIVE";
    await setTemplateStatus(existing.id, next);
    router.refresh();
  }

  return (
    <div className="space-y-4" data-testid="template-form">
      {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Type</span>
        <select value={type} onChange={(e) => setType(e.target.value as EmailClassification)} className={input}>
          <option value="TRANSACTIONAL">Transactional</option>
          <option value="LIFECYCLE">Lifecycle</option>
          <option value="MARKETING">Marketing</option>
        </select>
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Subject</span>
        <input value={subject} onChange={(e) => setSubject(e.target.value)} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Preheader (optional)</span>
        <input value={preheader} onChange={(e) => setPreheader(e.target.value)} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">
          HTML body (use <code>{"{{customer.first_name}}"}</code> style placeholders)
        </span>
        <textarea value={bodyHtml} onChange={(e) => setBodyHtml(e.target.value)} rows={6} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Plain text body (optional)</span>
        <textarea value={bodyText} onChange={(e) => setBodyText(e.target.value)} rows={3} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Declared variables (comma-separated)</span>
        <input value={variables} onChange={(e) => setVariables(e.target.value)} className={input} />
      </label>

      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={save} disabled={pending} className={button} data-testid="save-template">
          {pending ? "Saving…" : existing ? "Save changes" : "Create template"}
        </button>
        {existing ? (
          <>
            <button type="button" onClick={doPreview} className={button} data-testid="preview-template">
              Preview
            </button>
            <button type="button" onClick={toggleStatus} className={button} data-testid="toggle-template-status">
              {existing.status === "ACTIVE" ? "Archive" : "Activate"}
            </button>
          </>
        ) : null}
      </div>

      {preview ? (
        <div data-testid="template-preview" className="space-y-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
          <p className="text-sm font-medium">{preview.subject}</p>
          <div className="text-sm" dangerouslySetInnerHTML={{ __html: preview.html }} />
        </div>
      ) : null}
    </div>
  );
}
