<script setup lang="ts">
import { statusWord } from '../lib/status'

defineProps<{ status: string }>()
</script>

<template>
  <span class="statuschip" :class="status">{{ statusWord(status) }}</span>
</template>

<style scoped>
.statuschip {
  font-size: 11.5px;
  font-weight: 500;
  padding: 3px 10px;
  border-radius: 999px;
  color: var(--faint);
  background: var(--surface-2);
  white-space: nowrap;
}

.statuschip.running {
  color: var(--run);
  background: var(--run-chip);
}

.statuschip.success,
.statuschip.deployed {
  color: var(--ok);
  background: var(--ok-chip);
}

.statuschip.fail,
.statuschip.budget_exhausted {
  color: var(--bad);
  background: var(--bad-chip);
}

/* A preserved run awaiting an engineer's decision — a warning, not a failure. */
.statuschip.needs_rescope {
  color: var(--warn);
  background: var(--warn-chip);
}
</style>
