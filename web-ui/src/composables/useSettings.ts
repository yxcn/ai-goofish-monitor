import { ref, onMounted } from 'vue'
import * as settingsApi from '@/api/settings'
import type {
  NotificationSettings,
  NotificationSettingsUpdate,
  NotificationTestResponse,
  AiProvider,
  AiSettings,
  RotationSettings,
  SystemStatus
} from '@/api/settings'

export function useSettings() {
  const notificationSettings = ref<NotificationSettings>({})
  const aiSettings = ref<AiSettings>({})
  const rotationSettings = ref<RotationSettings>({})
  const systemStatus = ref<SystemStatus | null>(null)
  const isReady = ref(false)
  
  const isLoading = ref(false)
  const isSaving = ref(false)
  const error = ref<Error | null>(null)

  async function fetchAll() {
    isLoading.value = true
    error.value = null
    try {
      const [notif, ai, rotation, status] = await Promise.all([
        settingsApi.getNotificationSettings(),
        settingsApi.getAiSettings(),
        settingsApi.getRotationSettings(),
        settingsApi.getSystemStatus()
      ])
      notificationSettings.value = notif
      aiSettings.value = ai
      rotationSettings.value = rotation
      systemStatus.value = status
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      isLoading.value = false
      isReady.value = true
    }
  }

  async function refreshStatus() {
    isLoading.value = true
    error.value = null
    try {
      systemStatus.value = await settingsApi.getSystemStatus()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function saveNotificationSettings(payload: NotificationSettingsUpdate) {
    isSaving.value = true
    try {
      await settingsApi.updateNotificationSettings(payload)
      const [notif, status] = await Promise.all([
        settingsApi.getNotificationSettings(),
        settingsApi.getSystemStatus()
      ])
      notificationSettings.value = notif
      systemStatus.value = status
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSaving.value = false
    }
  }

  async function testNotification(payload: {
    channel?: string
    settings: NotificationSettingsUpdate
  }): Promise<NotificationTestResponse> {
    isSaving.value = true
    try {
      return await settingsApi.testNotificationSettings(payload)
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSaving.value = false
    }
  }

  // Key 留空 = 不修改：不放进请求体，免得把已保存的 Key 清掉
  function buildAiPayload(): AiSettings {
    const payload = { ...aiSettings.value }
    delete payload.CLAUDE_BUILTIN_PRICES // 只读字段，不回传
    for (const field of ['OPENAI_API_KEY', 'CLAUDE_API_KEY'] as const) {
      const apiKey = (payload[field] || '').trim()
      if (apiKey) {
        payload[field] = apiKey
      } else {
        delete payload[field]
      }
    }
    return payload
  }

  async function saveAiSettings() {
    isSaving.value = true
    try {
      await settingsApi.updateAiSettings(buildAiPayload())
      aiSettings.value.OPENAI_API_KEY = ''
      aiSettings.value.CLAUDE_API_KEY = ''
      // Refresh status
      systemStatus.value = await settingsApi.getSystemStatus()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSaving.value = false
    }
  }

  async function saveRotationSettings() {
    isSaving.value = true
    try {
      await settingsApi.updateRotationSettings(rotationSettings.value)
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSaving.value = false
    }
  }

  async function testAiConnection(provider: AiProvider = 'openai') {
    isSaving.value = true
    try {
      const res = await settingsApi.testAiSettings({ ...buildAiPayload(), provider })
      return res
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSaving.value = false
    }
  }

  onMounted(fetchAll)

  return {
    notificationSettings,
    aiSettings,
    rotationSettings,
    systemStatus,
    isLoading,
    isSaving,
    isReady,
    error,
    fetchAll,
    saveNotificationSettings,
    testNotification,
    saveAiSettings,
    saveRotationSettings,
    testAiConnection,
    refreshStatus,
  }
}
