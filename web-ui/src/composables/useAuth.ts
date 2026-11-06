import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(localStorage.getItem('auth_username'))
const isLoggedIn = ref(localStorage.getItem('auth_logged_in') === 'true')

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true

    localStorage.setItem('auth_username', user)
    localStorage.setItem('auth_logged_in', 'true')

    // 启动 WebSocket 连接
    wsService.start()
  }

  function logout() {
    // Best-effort: ask the server to clear the HttpOnly session cookie. Not awaited; a
    // failure (or an already-expired session) must not block the local logout.
    if (isLoggedIn.value) {
      fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => {})
    }

    username.value = null
    isLoggedIn.value = false
    localStorage.removeItem('auth_username')
    localStorage.removeItem('auth_logged_in')

    // 停止 WebSocket 连接
    wsService.stop()

    // Redirect to login if using router
    if (router) {
      router.push('/login')
    } else {
      window.location.href = '/login'
    }
  }

  /**
   * Check whether the server still recognises our session cookie. Only an explicit 401 logs
   * out; network errors or an older backend without `/auth/me` keep the local state.
   */
  async function verifySession() {
    if (!isLoggedIn.value) return
    try {
      const response = await fetch('/auth/me', { credentials: 'same-origin' })
      if (response.status === 401) logout()
    } catch {
      /* offline or server unreachable: leave the local state alone */
    }
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/status', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        return false
      }
    } catch (e) {
      console.error('Login error', e)
      return false
    }
  }

  return {
    username,
    isAuthenticated,
    login,
    logout,
    verifySession,
  }
}
