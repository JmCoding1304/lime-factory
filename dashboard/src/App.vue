<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch, watchEffect } from 'vue'
import type { ProjectInfo, RunSummary } from './lib/types'
import { fetchProjects, fetchRuns } from './lib/api'
import { titleLine } from './lib/liveness'
import { refetcher } from './lib/runsSheet'
import { live } from './lib/live'
import Workspace from './components/Workspace.vue'

// The sidebar is gone; the run pool feeds the tab title and the Runs sheet.
// The pool refetches when the live feed's `running` changes; no poll.
const runs = shallowRef<RunSummary[]>([])
const projects = shallowRef<ProjectInfo[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)

async function load() {
  try {
    runs.value = await fetchRuns()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  }
}

const refetch = refetcher(load)
watch(live.running, () => refetch())

onMounted(() => {
  live.start()
  refetch()
  // The registry changes only when a repo is `lime init`ed — one fetch is enough.
  void fetchProjects()
    .then((p) => (projects.value = p))
    .catch(() => {})
})

onUnmounted(() => {
  live.stop()
})

const projectNames = computed(() => projects.value.map((p) => p.name))

// The tab title is the pool's liveness, not the filtered view's: a failed or
// running Run anywhere still shows. A failed tick keeps the last title.
watchEffect(() => {
  if (loaded.value) document.title = titleLine(runs.value)
})
</script>

<template>
  <Workspace
    :projects="projectNames"
    :runs="runs"
    :loaded="loaded"
    :api-error="apiError"
  />
</template>
