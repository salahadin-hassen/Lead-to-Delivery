// Renders the onboarding form from the JSON Schema the API returns
// (GET /api/portal/onboarding), so the questions and the server's validation
// cannot drift apart. The server remains the authority: its 422 errors are
// shown next to the matching fields.

import { useId, useMemo, useState, type FormEvent } from 'react';
import type { JsonSchema } from './types';

export type FieldErrors = Record<string, string>;

interface Field {
  key: string;            // dotted path, e.g. "communication.preferred_channel"
  name: string;
  schema: JsonSchema;
  required: boolean;
}

interface Section {
  key: string;
  title: string | null;
  fields: Field[];
}

const OPTION_LABELS: Record<string, string> = {
  video_call: 'Video call', biweekly: 'Every two weeks', no_preference: 'No preference', wordpress: 'WordPress',
  webflow: 'Webflow', shopify: 'Shopify', slack: 'Slack',
};
const optionLabel = (value: string) =>
  OPTION_LABELS[value] ?? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ');

const SECTION_TITLES: Record<string, string> = { service_details: 'About your service' };

/** Follow $ref and collapse "X or null" into X (marked optional). */
function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema & { nullable?: boolean } {
  let s = schema;
  if (s.$ref) {
    const name = s.$ref.replace('#/$defs/', '');
    s = { ...(root.$defs?.[name] ?? {}), title: schema.title ?? root.$defs?.[name]?.title };
  }
  if (s.anyOf) {
    const inner = s.anyOf.find((o) => o.type !== 'null') ?? {};
    return { ...resolve(inner, root), title: s.title, default: s.default, nullable: true };
  }
  return s;
}

function sections(root: JsonSchema): Section[] {
  const main: Section = { key: '', title: null, fields: [] };
  const out: Section[] = [main];
  for (const [name, raw] of Object.entries(root.properties ?? {})) {
    const s = resolve(raw, root);
    if (s.type === 'object') {
      const fields = Object.entries(s.properties ?? {}).map(([child, childRaw]) => ({
        key: `${name}.${child}`, name: child, schema: resolve(childRaw, root),
        required: (s.required ?? []).includes(child),
      }));
      if (fields.length) out.push({ key: name, title: SECTION_TITLES[name] ?? s.title ?? null, fields });
    } else {
      main.fields.push({ key: name, name, schema: s, required: (root.required ?? []).includes(name) });
    }
  }
  return out;
}

type Raw = Record<string, string>;

function initialValues(all: Section[], prefill: Record<string, unknown>): Raw {
  const values: Raw = {};
  for (const section of all) {
    for (const f of section.fields) {
      const pre = prefill[f.key];
      if (typeof pre === 'string') values[f.key] = pre;
      else if (f.schema.enum && typeof f.schema.default === 'string') values[f.key] = f.schema.default;
      else values[f.key] = '';
    }
  }
  return values;
}

/** Free text long enough for a textarea (never a URL, email or date). */
function isLongText(s: JsonSchema): boolean {
  return s.type === 'string' && !s.format && !s.enum && (s.maxLength ?? 0) > 200;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function lines(value: string): string[] {
  return value.split('\n').map((l) => l.trim()).filter(Boolean);
}

/** Client-side checks mirror the schema; the server re-validates everything. */
function validate(f: Field, raw: string): string | null {
  const s = f.schema;
  const value = raw.trim();
  if (!value) return f.required ? 'This field is required.' : null;
  if (s.type === 'array') {
    const items = lines(raw);
    if (s.maxItems !== undefined && items.length > s.maxItems) return `Enter at most ${s.maxItems} items.`;
    const max = s.items?.maxLength;
    if (max !== undefined && items.some((i) => i.length > max)) return `Each item must be at most ${max} characters.`;
    return null;
  }
  if (s.type === 'integer') {
    if (!/^-?\d+$/.test(value)) return 'Enter a whole number.';
    const n = Number(value);
    if (s.minimum !== undefined && n < s.minimum) return `Enter ${s.minimum} or more.`;
    if (s.maximum !== undefined && n > s.maximum) return `Enter ${s.maximum} or less.`;
    return null;
  }
  if (s.type === 'boolean' || s.enum) return null;
  if (s.minLength !== undefined && value.length < s.minLength)
    return s.minLength > 1 ? `Enter at least ${s.minLength} characters.` : 'This field is required.';
  if (s.maxLength !== undefined && value.length > s.maxLength) return `Enter at most ${s.maxLength} characters.`;
  if (s.format === 'email' && !EMAIL_RE.test(value)) return 'Enter a valid email address.';
  if (s.format === 'uri' && !/^https?:\/\/\S+\.\S+/i.test(value)) return 'Enter a full web address starting with https://.';
  return null;
}

function toJson(all: Section[], values: Raw): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const section of all) {
    const target: Record<string, unknown> = section.key ? {} : out;
    for (const f of section.fields) {
      const raw = values[f.key] ?? '';
      const value = raw.trim();
      const s = f.schema;
      if (s.type === 'array') target[f.name] = lines(raw);
      else if (!value) continue;  // optional and empty: let the server apply its default
      else if (s.type === 'boolean') target[f.name] = value === 'yes';
      else if (s.type === 'integer') target[f.name] = Number(value);
      else target[f.name] = value;
    }
    if (section.key) out[section.key] = target;
  }
  return out;
}

function Control({ f, id, value, error, onChange }: {
  f: Field; id: string; value: string; error?: string; onChange: (v: string) => void;
}) {
  const s = f.schema;
  const describedBy = [error ? `${id}-error` : null, `${id}-hint`].filter(Boolean).join(' ');
  const common = { id, 'aria-invalid': error ? true : undefined, 'aria-describedby': describedBy,
                   'aria-required': f.required || undefined } as const;
  if (s.type === 'boolean') {
    return (
      <div className="choice-row" role="radiogroup" aria-labelledby={`${id}-label`} aria-describedby={describedBy}>
        {(['yes', 'no'] as const).map((option) => (
          <label key={option} className="choice">
            <input type="radio" name={id} value={option} checked={value === option}
                   onChange={() => onChange(option)} />
            {option === 'yes' ? 'Yes' : 'No'}
          </label>
        ))}
      </div>
    );
  }
  if (s.enum) {
    return (
      <select {...common} value={value} onChange={(e) => onChange(e.target.value)}>
        {(!f.required || !s.default) && <option value="">Choose…</option>}
        {s.enum.map((o) => <option key={o} value={o}>{optionLabel(o)}</option>)}
      </select>
    );
  }
  if (s.type === 'array' || isLongText(s)) {
    return <textarea {...common} rows={s.type === 'array' ? 3 : 4} value={value}
                     onChange={(e) => onChange(e.target.value)} />;
  }
  const type = s.format === 'date' ? 'date' : s.format === 'email' ? 'email' : s.format === 'uri' ? 'url'
    : s.type === 'integer' ? 'number' : 'text';
  return <input {...common} type={type} value={value} inputMode={s.type === 'integer' ? 'numeric' : undefined}
                min={s.minimum} max={s.maximum} onChange={(e) => onChange(e.target.value)} />;
}

function hint(f: Field): string {
  const s = f.schema;
  const parts: string[] = [];
  if (!f.required && s.type !== 'array') parts.push('Optional.');
  if (s.type === 'array') parts.push(`One per line${s.maxItems ? `, up to ${s.maxItems}` : ''}. Optional.`);
  if (isLongText(s) && (s.minLength ?? 0) > 1) parts.push(`At least ${s.minLength} characters.`);
  if (s.format === 'date') parts.push('Must be a future date.');
  return parts.join(' ');
}

export function SchemaForm({ schema, prefill, serverErrors, submitting, onSubmit }: {
  schema: JsonSchema;
  prefill: Record<string, unknown>;
  serverErrors: FieldErrors;
  submitting: boolean;
  onSubmit: (body: Record<string, unknown>) => void;
}) {
  const all = useMemo(() => sections(schema), [schema]);
  const [values, setValues] = useState<Raw>(() => initialValues(all, prefill));
  const [errors, setErrors] = useState<FieldErrors>({});
  const base = useId();
  const shown = { ...serverErrors, ...errors };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    const found: FieldErrors = {};
    for (const section of all) {
      for (const f of section.fields) {
        const problem = validate(f, values[f.key] ?? '');
        if (problem) found[f.key] = problem;
      }
    }
    setErrors(found);
    const first = Object.keys(found)[0];
    if (first) {
      document.getElementById(`${base}-${first}`)?.focus();
      return;
    }
    onSubmit(toJson(all, values));
  };

  return (
    <form className="portal-form" onSubmit={submit} noValidate aria-busy={submitting}>
      {all.map((section) => (
        <fieldset key={section.key || 'main'} className="portal-fieldset" disabled={submitting}>
          {section.title && <legend>{section.title}</legend>}
          {section.fields.map((f) => {
            const id = `${base}-${f.key}`;
            const error = shown[f.key];
            const fieldHint = hint(f);
            return (
              <div key={f.key} className="portal-field">
                <label id={`${id}-label`} htmlFor={f.schema.type === 'boolean' ? undefined : id}>
                  {f.schema.title ?? f.name}{f.required && <span aria-hidden="true" className="req"> *</span>}
                </label>
                <Control f={f} id={id} value={values[f.key] ?? ''} error={error}
                         onChange={(v) => {
                           setValues((prev) => ({ ...prev, [f.key]: v }));
                           if (errors[f.key]) setErrors((prev) => { const next = { ...prev }; delete next[f.key]; return next; });
                         }} />
                <p id={`${id}-hint`} className="hint">{fieldHint}</p>
                {error && <p id={`${id}-error`} className="field-error">{error}</p>}
              </div>
            );
          })}
        </fieldset>
      ))}
      <p className="hint">Fields marked * are required. You can submit this form once.</p>
      <button type="submit" className="button primary" disabled={submitting}>
        {submitting ? 'Submitting…' : 'Submit project details'}
      </button>
    </form>
  );
}

/** Map the API's 422 detail ({loc: ["body", ...path], msg}) onto field keys. */
export function serverFieldErrors(fieldErrors: { loc: (string | number)[]; msg: string }[]): FieldErrors {
  const out: FieldErrors = {};
  for (const e of fieldErrors) {
    const path = e.loc[0] === 'body' ? e.loc.slice(1) : e.loc;
    const key = path.filter((p) => typeof p === 'string').join('.');
    const field = String(path[path.length - 1] ?? '');
    let msg = e.msg.replace(/^Value error, /, '');
    if (field && msg.startsWith(`${field} `)) msg = msg.slice(field.length + 1);
    msg = msg.charAt(0).toUpperCase() + msg.slice(1);
    if (key && !out[key]) out[key] = /[.!?]$/.test(msg) ? msg : `${msg}.`;
  }
  return out;
}
