<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { createTaskWithAI } from '@/api/tasks'
import { useTaskGenerationJob } from '@/composables/useTaskGenerationJob'
import type { TaskGenerateRequest } from '@/types/task.d.ts'
import { parseTaskFormDefaults, TASK_FORM_QUERY_KEYS, withoutQueryKeys } from '@/lib/taskFormQuery'
import type { TaskFormDefaults } from '@/lib/taskFormQuery'
import TaskForm from '@/components/tasks/TaskForm.vue'
import TaskGenerationDialog from '@/components/tasks/TaskGenerationDialog.vue'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
const { t } = useI18n()

const props = defineProps<{
  accountOptions?: { name: string; path: string }[]
}>()

const emit = defineEmits<{
  (event: 'created'): void
}>()

const route = useRoute()
const router = useRouter()
const isFormOpen = ref(false)
const isProgressOpen = ref(false)
const isSubmitting = ref(false)
// Snapshot of the `?account=` / form-default query keys taken when they appear in the URL.
// The path is resolved lazily so `accountOptions` arriving later still fills the account in.
const queryAccountName = ref('')
const defaultValues = ref<TaskFormDefaults>({})
const defaultAccountPath = computed(() => (queryAccountName.value ? resolveAccountPath(queryAccountName.value) : ''))
const {
  activeJob,
  pollingError,
  beginPolling,
  clearJob,
} = useTaskGenerationJob()

function resolveAccountPath(accountName: string) {
  const match = (props.accountOptions || []).find((account) => account.name === accountName)
  return match ? match.path : ''
}

async function handleCreateTask(data: TaskGenerateRequest) {
  isSubmitting.value = true
  clearJob()
  try {
    const result = await createTaskWithAI(data)
    if (result.job) {
      isFormOpen.value = false
      isProgressOpen.value = true
      beginPolling(result.job)
      isSubmitting.value = false
      return
    }
    emit('created')
    toast({ title: t('tasks.toasts.created') })
    isFormOpen.value = false
  } catch (error) {
    toast({
      title: t('tasks.toasts.createFailed'),
      description: (error as Error).message,
      variant: 'destructive',
    })
  } finally {
    if (!isProgressOpen.value) {
      isSubmitting.value = false
    }
  }
}

// `?create=1` opens the dialog once. The dialog/default keys are dropped from the URL as
// soon as it opens (unrelated keys are kept), so a reload or a later `accountOptions`
// update cannot reopen it or reset the form. The defaults themselves live in the snapshot
// above, not in the URL.
const CREATE_DIALOG_QUERY_KEYS: string[] = ['create', ...TASK_FORM_QUERY_KEYS]
watch(
  () => route.query,
  (query) => {
    const defaults = parseTaskFormDefaults(query)
    const accountName = typeof query.account === 'string' ? query.account : ''
    if (accountName || Object.keys(defaults).length > 0) {
      queryAccountName.value = accountName
      defaultValues.value = defaults
    }
    if (query.create !== '1') return
    isFormOpen.value = true
    const rest = withoutQueryKeys(query, CREATE_DIALOG_QUERY_KEYS)
    if (rest) router.replace({ query: rest })
  },
  { immediate: true }
)

// Closing drops the snapshot so the next manual open starts from a blank form.
watch(isFormOpen, (open) => {
  if (open) return
  queryAccountName.value = ''
  defaultValues.value = {}
})

watch(
  () => activeJob.value?.status,
  (status, previousStatus) => {
    if (!status || status === previousStatus) return
    if (status === 'completed') {
      isSubmitting.value = false
      emit('created')
      toast({ title: t('tasks.toasts.created') })
      isProgressOpen.value = false
      clearJob()
      return
    }
    if (status === 'failed') {
      isSubmitting.value = false
      toast({
        title: t('tasks.toasts.createFailed'),
        description: activeJob.value?.error || activeJob.value?.message,
        variant: 'destructive',
      })
    }
  }
)

watch(pollingError, (value) => {
  if (!value) return
  isSubmitting.value = false
  toast({
    title: t('tasks.toasts.progressFailed'),
    description: value.message,
    variant: 'destructive',
  })
})
</script>

<template>
  <Dialog v-model:open="isFormOpen">
    <DialogTrigger as-child>
      <Button>{{ t('tasks.createDialog.trigger') }}</Button>
    </DialogTrigger>
    <DialogContent class="sm:max-w-[640px] max-h-[85vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{{ t('tasks.createDialog.title') }}</DialogTitle>
      </DialogHeader>
      <TaskForm
        mode="create"
        :account-options="accountOptions"
        :default-account="defaultAccountPath"
        :default-values="defaultValues"
        @submit="(data) => handleCreateTask(data as TaskGenerateRequest)"
      />
      <DialogFooter>
        <Button type="submit" form="task-form" :disabled="isSubmitting">
          {{ isSubmitting ? t('tasks.createDialog.submitting') : t('tasks.createDialog.submit') }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
  <TaskGenerationDialog
    v-model:open="isProgressOpen"
    :job="activeJob"
  />
</template>
