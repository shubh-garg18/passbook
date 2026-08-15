/* The one place that talks to the server. SPEC §16.1.
 *
 * Same origin, so there is no base URL and no CORS. Auth is the httpOnly
 * `pb_session` cookie — never a token in localStorage, which any injected
 * script can read and exfiltrate. The CSRF token is the one cookie JS *can*
 * read, because it exists precisely to be echoed back in a header.
 */

export class ApiError extends Error {
  status: number
  code: string
  constructor(message: string, status: number, code = 'error') {
    super(message)
    this.status = status
    this.code = code
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)pb_csrf=([^;]*)/)
  return match?.[1] ? decodeURIComponent(match[1]) : ''
}

type Options = { method?: string; body?: unknown; form?: FormData }

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = {}
  let body: BodyInit | undefined

  if (options.form) {
    body = options.form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-Passbook-CSRF'] = csrfToken()
  }

  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body,
    // Same-origin cookies. `omit` would silently sign every request out.
    credentials: 'same-origin',
  })

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    const shaped = payload as { error?: string; code?: string } | null
    throw new ApiError(
      shaped?.error ?? `Request failed (${response.status}).`,
      response.status,
      shaped?.code ?? 'error',
    )
  }
  return payload as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', form }),
}
