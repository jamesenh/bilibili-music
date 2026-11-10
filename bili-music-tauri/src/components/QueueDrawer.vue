<script setup lang="ts">
/** 展示播放队列与行操作，复用共享播放状态。 */
import { X, Trash2, Music2, Play } from 'lucide-vue-next'
import { usePlayerStore } from '@/stores/player'
import { formatDuration } from '@/utils/format'
import type { QueueItem } from '@/types/music'

const player = usePlayerStore()

/** 选择队列中的曲目并交给播放仓库开始播放。 */
function playItem(item: QueueItem, index: number) {
  player.currentIndex = index
  player.playVideo(item.video, item.pageIndex)
}

/** 阻止行点击冒泡并从队列移除指定位置。 */
function removeItem(e: Event, index: number) {
  e.stopPropagation()
  player.queue.splice(index, 1)
  if (player.currentIndex === index) {
    if (player.queue.length > 0) {
      const nextIdx = index % player.queue.length
      const nextItem = player.queue[nextIdx]
      player.playVideo(nextItem.video, nextItem.pageIndex)
    } else {
      player.clearQueue()
    }
  } else if (player.currentIndex > index) {
    player.currentIndex--
  }
}
</script>

<template>
  <aside
    v-if="player.isQueueOpen"
    class="w-80 bg-surface-subtle border-l border-surface-border flex flex-col z-20 shrink-0 select-none animate-in slide-in-from-right duration-200"
  >
    <!-- 头部 -->
    <div class="h-14 px-4 border-b border-surface-border flex items-center justify-between">
      <div class="flex items-center gap-2">
        <span class="text-sm font-semibold text-white">播放队列</span>
        <span class="text-xs font-mono text-neutral-500">({{ player.queue.length }})</span>
      </div>
      <div class="flex items-center gap-1">
        <button
          v-if="player.queue.length > 0"
          title="清空队列"
          class="p-1.5 rounded-lg text-neutral-400 hover:text-red-400 hover:bg-surface-hover transition-colors"
          @click="player.clearQueue()"
        >
          <Trash2 class="w-3.5 h-3.5" />
        </button>
        <button
          title="收起队列"
          class="p-1.5 rounded-lg text-neutral-400 hover:text-white hover:bg-surface-hover transition-colors"
          @click="player.isQueueOpen = false"
        >
          <X class="w-4 h-4" />
        </button>
      </div>
    </div>

    <!-- 列表内容 -->
    <div class="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
      <div v-if="player.queue.length === 0" class="h-full flex flex-col items-center justify-center text-neutral-500 gap-2">
        <Music2 class="w-8 h-8 stroke-[1.5]" />
        <span class="text-xs">播放队列为空</span>
      </div>

      <div
        v-for="(item, index) in player.queue"
        :key="item.id"
        class="group flex items-center justify-between px-3 py-2 rounded-xl text-xs transition-colors cursor-pointer"
        :class="player.currentIndex === index
          ? 'bg-surface-panel text-bili-hot font-medium border border-bili-hot/20'
          : 'text-neutral-300 hover:bg-surface-hover hover:text-white'"
        @click="playItem(item, index)"
      >
        <div class="flex items-center gap-2.5 min-w-0 pr-2">
          <!-- 序号或播放指示 -->
          <span class="w-4 text-[11px] font-mono text-neutral-500 shrink-0">
            {{ (index + 1).toString().padStart(2, '0') }}
          </span>
          <div class="flex flex-col min-w-0">
            <span class="truncate text-xs">
              {{ item.video.pages[item.pageIndex]?.part || item.video.title }}
            </span>
            <span class="text-[10px] text-neutral-500 truncate">
              {{ item.video.owner_name }}
            </span>
          </div>
        </div>

        <div class="flex items-center gap-2 shrink-0">
          <span class="text-[11px] font-mono text-neutral-500 group-hover:hidden">
            {{ formatDuration(item.video.pages[item.pageIndex]?.duration || 0) }}
          </span>
          <button
            class="hidden group-hover:block text-neutral-500 hover:text-neutral-300 p-1"
            title="移除"
            @click="removeItem($event, index)"
          >
            <X class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  </aside>
</template>
