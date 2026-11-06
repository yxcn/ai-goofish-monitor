import { useAuth } from '@/composables/useAuth'

interface FetchOptions extends RequestInit {
  params?: Record<string, string | number | boolean | undefined>;
}

/** Non-2xx response. `status` lets a caller tell a 404 (feature not deployed) from a real failure. */
export class HttpError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

/**
 * Turn a FastAPI error `detail` into a readable message.
 * - string: as is
 * - array (422 validation errors): `loc.join('.')：msg` per item, joined by `；`
 * - object: JSON
 */
function formatErrorDetail(detail: unknown, status: number): string {
  const fallback = `HTTP error! status: ${status}`
  if (typeof detail === 'string') {
    return detail || fallback
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>
          const msg = typeof record.msg === 'string' ? record.msg : JSON.stringify(record)
          const loc = Array.isArray(record.loc) ? record.loc.map(String).join('.') : ''
          return loc ? `${loc}：${msg}` : msg
        }
        return item === undefined || item === null ? '' : String(item)
      })
      .filter((part) => part.length > 0)
    return parts.length ? parts.join('；') : fallback
  }
  if (detail && typeof detail === 'object') {
    try {
      return JSON.stringify(detail)
    } catch {
      return fallback
    }
  }
  return fallback
}

export async function http(url: string, options: FetchOptions = {}) {
  const { logout } = useAuth()
  
  const headers = new Headers(options.headers)

  // Handle Query Params
  let fullUrl = url
  if (options.params) {
    const searchParams = new URLSearchParams()
    Object.entries(options.params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        searchParams.append(key, String(value))
      }
    })
    const queryString = searchParams.toString()
    if (queryString) {
      fullUrl += (url.includes('?') ? '&' : '?') + queryString
    }
  }

  const config: RequestInit = {
    // Send the HttpOnly session cookie set by POST /auth/status.
    credentials: 'same-origin',
    ...options,
    headers,
  }

  const response = await fetch(fullUrl, config)

  if (response.status === 401) {
    // Session cookie missing/expired (or Basic Auth rejected)
    logout()
    // Optional: Redirect to login handled by router or state change
    throw new Error('Unauthorized')
  }

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new HttpError(formatErrorDetail(errorData?.detail, response.status), response.status)
  }

  // Handle 204 No Content
  if (response.status === 204) {
    return null
  }

  return response.json()
}
