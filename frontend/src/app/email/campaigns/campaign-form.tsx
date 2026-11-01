"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { EmailTemplate } from "@/lib/types";
import { createCampaign } from "../actions";

const input = "w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-sm dark:border-zinc-700";
const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

export function CampaignForm({ templates }: { templates: EmailTemplate[] }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [tags, setTags] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setPending(true);
    setError(null);
    const audience_definition: Record<string, unknown> = { marketing_consent: true };
    const tagList = tags.split(",").map((t) => t.trim()).filter(Boolean);
    if (tagList.length) audience_definition.tags = tagList;
    const result = await createCampaign({ name, template_id: templateId, audience_definition });
    setPending(false);
    if (!result.ok) {
      setError(result.state?.error ?? "Something went wrong.");
      return;
    }
    router.push(`/email/campaigns/${result.data.id}`);
  }

  if (templates.length === 0) {
    return <p className="text-sm text-zinc-500">Create an active email template first.</p>;
  }

  return (
    <div className="space-y-3" data-testid="campaign-form">
      {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Campaign name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} className={input} />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Template</span>
        <select value={templateId} onChange={(e) => setTemplateId(e.target.value)} className={input}>
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1 text-sm">
        <span className="text-zinc-500">Only customers tagged (optional, comma-separated)</span>
        <input value={tags} onChange={(e) => setTags(e.target.value)} className={input} />
      </label>
      <p className="text-xs text-zinc-500">
        Audience is always limited to customers who opted in to marketing email and are not
        suppressed.
      </p>
      <button type="button" onClick={save} disabled={pending || !name || !templateId} className={button}>
        {pending ? "Creating…" : "Create campaign (draft)"}
      </button>
    </div>
  );
}
