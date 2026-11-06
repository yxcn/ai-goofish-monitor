<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'
import Toaster from '@/components/ui/toast/Toaster.vue'
import { useAuth } from '@/composables/useAuth'

const { verifySession } = useAuth()

// localStorage only remembers that we logged in once; confirm the session cookie is still
// accepted by the server (a 401 logs out and redirects to /login).
onMounted(() => {
  verifySession()
})
</script>

<template>
  <RouterView />
  <Toaster />
</template>
