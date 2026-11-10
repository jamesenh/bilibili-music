<script setup lang="ts">
/** 展示播放进度与控制入口，播放行为交给共享仓库。 */
import { ref, computed } from 'vue'
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  Repeat,
  Repeat1,
  Shuffle,
  Volume2,
  VolumeX,
  ListMusic,
  Layers,
  ChevronUp
} from 'lucide-vue-next'
import { usePlayerStore } from '@/stores/player'
import { formatDuration } from '@/utils/format'
import { QUALITY_OPTIONS, type AudioQuality } from '@/types/music'

const player = usePlayerStore()

const emit = defineEmits<{
  (e: 'open-pages'): void
}>()

const isDragging = ref(false)
const dragTime = ref(0)

// 进度百分比
const progressPercent = computed(() => {
  const current = isDragging.value ? dragTime.value : player.currentTime
  if (!player.duration || player.duration === 0) return 0
  return (current / player.duration) * 100
})

/** 把进度条鼠标位置映射为播放秒数并跟踪拖动。 */
function onProgressMouseDown(e: MouseEvent) {
  const bar = e.currentTarget as HTMLElement
  const rect = bar.getBoundingClientRect()
  const clickX = e.clientX - rect.left
  const ratio = Math.max(0, Math.min(1, clickX / rect.width))
  dragTime.value = ratio * player.duration
  isDragging.value = true

  const onMouseMove = (moveEvent: MouseEvent) => {
    const moveX = moveEvent.clientX - rect.left
    const moveRatio = Math.max(0, Math.min(1, moveX / rect.width))
    dragTime.value = moveRatio * player.duration
  }

  const onMouseUp = () => {
    player.seek(dragTime.value)
    isDragging.value = false
    window.removeEventListener('mousemove', onMouseMove)
    window.removeEventListener('mouseup', onMouseUp)
  }

  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
}

/** 切换播放仓库中的静音展示状态。 */
function toggleMute() {
  player.isMuted = !player.isMuted
}
</script>

<template>
  <footer
    class="h-20 bg-surface-subtle/95 backdrop-blur-xl border-t border-surface-border px-4 flex items-center justify-between select-none relative z-30"
  >
    <!-- 左侧：正在播放信息 + 分P选择 + 音质 -->
    <div class="flex items-center gap-3 w-80 min-w-0">
      <!-- 封面 -->
      <div class="relative w-12 h-12 rounded-xl overflow-hidden bg-neutral-800 shrink-0 border border-surface-border shadow-md">
        <img
          v-if="player.displayCover"
          :src="player.displayCover"
          :alt="player.displayTitle"
          class="w-full h-full object-cover"
        />
        <div v-else class="w-full h-full flex items-center justify-center text-neutral-600">
          <ListMusic class="w-5 h-5" />
        </div>
      </div>

      <!-- 标题与次要信息 -->
      <div class="flex flex-col min-w-0 gap-0.5">
        <span class="text-xs font-semibold text-white truncate hover:underline cursor-pointer">
          {{ player.displayTitle }}
        </span>
        <div class="flex items-center gap-2 text-[11px] text-neutral-400">
          <span class="truncate max-w-[120px]">{{ player.displayArtist }}</span>

          <!-- 分P选择胶囊按钮 -->
          <button
            v-if="player.currentVideo && player.currentVideo.pages.length > 1"
            class="shrink-0 flex items-center gap-1 px-1.5 py-0.5 rounded bg-surface-panel hover:bg-surface-hover text-bili-hot border border-bili-hot/30 text-[10px] font-medium transition-colors"
            @click="emit('open-pages')"
          >
            <Layers class="w-2.5 h-2.5" />
            <span>P{{ player.currentPageIndex + 1 }}/{{ player.currentVideo.pages.length }}</span>
          </button>

          <!-- 音质选择 -->
          <select
            v-model="player.quality"
            class="bg-surface-panel hover:bg-surface-hover text-neutral-300 rounded px-1 py-0.5 text-[10px] outline-none border border-surface-border cursor-pointer transition-colors"
          >
            <option v-for="q in QUALITY_OPTIONS" :key="q.id" :value="q.id">
              {{ q.label.split(' ')[0] }}
            </option>
          </select>
        </div>
      </div>
    </div>

    <!-- 中间：核心播放控制与进度条 -->
    <div class="flex flex-col items-center gap-1.5 w-full max-w-xl px-4">
      <!-- 控制按钮 -->
      <div class="flex items-center gap-4">
        <!-- 循环模式 -->
        <button
          class="text-neutral-400 hover:text-white transition-colors"
          title="切换播放模式"
          @click="player.togglePlayMode()"
        >
          <Shuffle v-if="player.playMode === 'shuffle'" class="w-4 h-4 text-bili-hot" />
          <Repeat1 v-else-if="player.playMode === 'single_loop'" class="w-4 h-4 text-bili-hot" />
          <Repeat v-else class="w-4 h-4" :class="player.playMode === 'list_loop' ? 'text-bili-hot' : ''" />
        </button>

        <!-- 上一首 -->
        <button
          class="text-neutral-300 hover:text-white transition-colors active:scale-90"
          title="上一首"
          @click="player.prevTrack()"
        >
          <SkipBack class="w-4 h-4 fill-current" />
        </button>

        <!-- 播放/暂停 -->
        <button
          class="w-9 h-9 rounded-full bg-gradient-to-tr from-bili-hot to-bili-pink text-white flex items-center justify-center shadow-glow transition-all active:scale-95 hover:brightness-110"
          :title="player.isPlaying ? '暂停' : '播放'"
          @click="player.togglePlay()"
        >
          <Pause v-if="player.isPlaying" class="w-4 h-4 fill-current" />
          <Play v-else class="w-4 h-4 fill-current ml-0.5" />
        </button>

        <!-- 下一首 -->
        <button
          class="text-neutral-300 hover:text-white transition-colors active:scale-90"
          title="下一首"
          @click="player.nextTrack()"
        >
          <SkipForward class="w-4 h-4 fill-current" />
        </button>
      </div>

      <!-- 进度条 -->
      <div class="w-full flex items-center gap-2 text-[10px] font-mono text-neutral-500">
        <span class="w-9 text-right">
          {{ formatDuration(isDragging ? dragTime : player.currentTime) }}
        </span>
        <div
          class="group/bar relative flex-1 h-1.5 bg-neutral-800 rounded-full cursor-pointer py-1"
          @mousedown="onProgressMouseDown"
        >
          <!-- 背景条 -->
          <div class="h-1 bg-neutral-800 group-hover/bar:h-1.5 rounded-full overflow-hidden transition-all">
            <div
              class="h-full bg-gradient-to-r from-bili-hot to-bili-pink rounded-full transition-all"
              :style="{ width: `${progressPercent}%` }"
            ></div>
          </div>
          <!-- 悬浮圆球 -->
          <div
            class="absolute top-1/2 -translate-y-1/2 -ml-1 w-2.5 h-2.5 bg-white rounded-full opacity-0 group-hover/bar:opacity-100 transition-opacity shadow pointer-events-none"
            :style="{ left: `${progressPercent}%` }"
          ></div>
        </div>
        <span class="w-9">{{ formatDuration(player.duration) }}</span>
      </div>
    </div>

    <!-- 右侧：音量与播放队列 -->
    <div class="flex items-center justify-end gap-4 w-80">
      <!-- 音量控制 -->
      <div class="flex items-center gap-2 group/vol">
        <button class="text-neutral-400 hover:text-white transition-colors" @click="toggleMute">
          <VolumeX v-if="player.isMuted || player.volume === 0" class="w-4 h-4 text-red-400" />
          <Volume2 v-else class="w-4 h-4" />
        </button>
        <input
          v-model.number="player.volume"
          type="range"
          min="0"
          max="1"
          step="0.01"
          class="w-20 h-1 bg-neutral-800 rounded-lg appearance-none cursor-pointer accent-bili-hot"
        />
      </div>

      <!-- 播放队列开关 -->
      <button
        class="relative flex items-center gap-1 px-2.5 py-1.5 rounded-xl border border-surface-border transition-all"
        :class="player.isQueueOpen
          ? 'bg-surface-panel text-bili-hot border-bili-hot/30'
          : 'bg-surface-panel/40 text-neutral-400 hover:text-white hover:bg-surface-panel'"
        title="播放队列"
        @click="player.isQueueOpen = !player.isQueueOpen"
      >
        <ListMusic class="w-4 h-4" />
        <span class="text-xs font-mono font-medium">{{ player.queue.length }}</span>
      </button>
    </div>
  </footer>
</template>
