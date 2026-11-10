<script setup lang="ts">
/** 展示分P标题与各自时长，将选择结果通知上层。 */
import { ref, computed } from 'vue'
import { X, Search, Layers, Play } from 'lucide-vue-next'
import type { Video, Page } from '@/types/music'
import { formatDuration } from '@/utils/format'
import { usePlayerStore } from '@/stores/player'

const props = defineProps<{
  video: Video | null
  isOpen: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'select-page', pageIndex: number): void
}>()

const player = usePlayerStore()
const filterText = ref('')

const filteredPages = computed(() => {
  if (!props.video) return []
  const q = filterText.value.trim().toLowerCase()
  if (!q) return props.video.pages
  return props.video.pages.filter((p: Page) => p.part.toLowerCase().includes(q))
})

/** 根据当前视频与cid判断该分P是否正在播放。 */
function isCurrentActive(page: Page) {
  if (!props.video || player.currentVideo?.bvid !== props.video.bvid) return false
  return player.currentPage?.cid === page.cid
}

/** 用cid定位分P的原始零起始索引并通知上层。 */
function select(page: Page) {
  if (!props.video) return
  const originalIndex = props.video.pages.findIndex((p: Page) => p.cid === page.cid)
  if (originalIndex !== -1) {
    emit('select-page', originalIndex)
  }
}
</script>

<template>
  <div
    v-if="isOpen && video"
    class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4 animate-in fade-in duration-150 select-none"
    @click.self="emit('close')"
  >
    <div
      class="w-full max-w-lg bg-surface-panel border border-surface-border rounded-2xl shadow-modal overflow-hidden flex flex-col max-h-[80vh] animate-in zoom-in-95 duration-150"
    >
      <!-- 弹窗头部 -->
      <div class="p-4 border-b border-surface-border flex items-center justify-between">
        <div class="flex items-center gap-2 min-w-0 pr-4">
          <div class="w-8 h-8 rounded-lg bg-bili-hot/20 text-bili-hot flex items-center justify-center shrink-0">
            <Layers class="w-4 h-4" />
          </div>
          <div class="flex flex-col min-w-0">
            <span class="text-xs font-semibold text-white truncate">{{ video.title }}</span>
            <span class="text-[11px] text-neutral-400">共 {{ video.pages.length }} 个分P / 歌曲</span>
          </div>
        </div>
        <button
          class="p-1 rounded-lg text-neutral-400 hover:text-white hover:bg-surface-hover transition-colors"
          @click="emit('close')"
        >
          <X class="w-4 h-4" />
        </button>
      </div>

      <!-- 分P快速过滤搜索框 -->
      <div class="px-4 py-2 border-b border-surface-border bg-surface-subtle/50">
        <div class="h-8 rounded-lg bg-surface-hover/80 border border-surface-border flex items-center px-2.5 gap-2">
          <Search class="w-3.5 h-3.5 text-neutral-500" />
          <input
            v-model="filterText"
            type="text"
            placeholder="过滤分P或曲名..."
            class="w-full bg-transparent text-xs text-neutral-200 placeholder-neutral-500 outline-none"
          />
        </div>
      </div>

      <!-- 分P列表 -->
      <div class="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
        <div
          v-for="page in filteredPages"
          :key="page.cid"
          class="group flex items-center justify-between px-3 py-2 rounded-xl text-xs transition-colors cursor-pointer"
          :class="isCurrentActive(page)
            ? 'bg-bili-hot/15 text-bili-hot font-semibold border border-bili-hot/30'
            : 'text-neutral-300 hover:bg-surface-hover hover:text-white'"
          @click="select(page)"
        >
          <div class="flex items-center gap-2.5 min-w-0 pr-3">
            <span class="text-[11px] font-mono text-neutral-500 w-6 shrink-0">
              P{{ page.page }}
            </span>
            <span class="truncate">{{ page.part }}</span>
          </div>

          <div class="flex items-center gap-2 shrink-0">
            <span class="text-[11px] font-mono text-neutral-500 group-hover:hidden">
              {{ formatDuration(page.duration) }}
            </span>
            <button
              class="hidden group-hover:flex w-5 h-5 rounded-full bg-bili-hot text-white items-center justify-center shadow"
            >
              <Play class="w-2.5 h-2.5 fill-current ml-0.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
