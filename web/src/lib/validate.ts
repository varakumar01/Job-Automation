// Small, focused input validators shared across the app's forms. Each
// returns an error string (shown inline) or null when the value is valid.

export function required(value: string, label: string): string | null {
  return value.trim() ? null : `${label} is required`
}

export function positiveInt(value: number, label: string): string | null {
  if (!Number.isInteger(value) || value < 1) return `${label} must be a positive whole number`
  return null
}

export function nonNegativeInt(value: number, label: string): string | null {
  if (!Number.isInteger(value) || value < 0) return `${label} must be a whole number ≥ 0`
  return null
}

const ENV_KEY_RE = /^[A-Z][A-Z0-9_]*$/

export function envKey(key: string): string | null {
  if (!key.trim()) return 'key is required'
  if (!ENV_KEY_RE.test(key)) return 'key must be UPPER_SNAKE_CASE (e.g. APIFY_TOKEN)'
  return null
}

export function envValue(value: string): string | null {
  if (/[\n\r]/.test(value)) return 'value cannot contain a newline'
  return null
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function email(value: string, required_ = true): string | null {
  if (!value.trim()) return required_ ? 'email is required' : null
  return EMAIL_RE.test(value) ? null : 'not a valid email address'
}

const JOB_IDS_RE = /^\s*\d+(\s*[,\s]\s*\d+)*\s*$/

export function jobIdList(value: string): string | null {
  if (!value.trim()) return 'at least one job id is required'
  return JOB_IDS_RE.test(value) ? null : 'expected comma-separated job ids, e.g. "12,47,103"'
}
