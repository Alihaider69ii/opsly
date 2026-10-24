import type { ComponentProps, ReactNode } from "react";

const control =
  "w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm outline-none " +
  "focus:border-zinc-900 aria-[invalid=true]:border-red-500 dark:border-zinc-700 dark:focus:border-zinc-300";

function FieldShell({
  label,
  name,
  error,
  hint,
  children,
}: {
  label: string;
  name: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={name} className="block text-sm font-medium">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${name}-error`} className="text-sm text-red-600">
          {error}
        </p>
      ) : hint ? (
        <p className="text-sm text-zinc-500">{hint}</p>
      ) : null}
    </div>
  );
}

type FieldProps = Omit<ComponentProps<"input">, "id" | "name"> & {
  label: string;
  name: string;
  error?: string;
  hint?: string;
};

export function Field({ label, name, error, hint, ...props }: FieldProps) {
  return (
    <FieldShell label={label} name={name} error={error} hint={hint}>
      <input
        id={name}
        name={name}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${name}-error` : undefined}
        className={control}
        {...props}
      />
    </FieldShell>
  );
}

type SelectProps = Omit<ComponentProps<"select">, "id" | "name"> & {
  label: string;
  name: string;
  error?: string;
  hint?: string;
  options: readonly { value: string; label: string }[];
  placeholder: string;
};

export function SelectField({
  label,
  name,
  error,
  hint,
  options,
  placeholder,
  ...props
}: SelectProps) {
  return (
    <FieldShell label={label} name={name} error={error} hint={hint}>
      <select
        id={name}
        name={name}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${name}-error` : undefined}
        className={control}
        {...props}
      >
        <option value="">{placeholder}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </FieldShell>
  );
}

export function FormError({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
    >
      {message}
    </p>
  );
}

export function SubmitButton({ pending, children }: { pending: boolean; children: ReactNode }) {
  return (
    <button
      type="submit"
      disabled={pending}
      className="w-full rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
    >
      {pending ? "Please wait…" : children}
    </button>
  );
}
