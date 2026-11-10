<script setup lang="ts">
/** 展示曲目列表并转发播放与分P选择事件。 */
import { Play, Plus, Download, ListMusic, User, Clock, Layers } from 'lucide-vue-next'
import type { Video } from '@/types/music'
import { formatDuration } from '@/utils/format'
import { usePlayerStore } from '@/stores/player'

const props = defineProps<{
  videos: Video[]
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'play', video: Video, pageIndex?: number): void
  (e: 'open-pages', video: Video): void
  (e: 'cache-all', video: Video): void
}>()

const player = usePlayerStore()

/** 根据视频标识判断列表行是否属于当前播放视频。 */
function isCurrentPlaying(video: Video) {
  return player.currentVideo?.bvid === video.bvid
}
</script>

<template>
  <div class="w-full flex flex-col">
    <!-- 表头 -->
    <div class="grid grid-cols-12 gap-4 px-4 py-2 text-[11px] font-semibold text-neutral-500 uppercase tracking-wider border-b border-surface-border">
      <div class="col-span-1 text-center">#</div>
      <div class="col-span-6">歌曲 / 视频标题</div>
      <div class="col-span-3">UP主</div>
      <div class="col-span-2 text-right flex items-center justify-end gap-1">
        <Clock class="w-3 h-3" />
        时长
      </div>
    </div>

    <!-- 列表项 -->
    <div class="flex flex-col py-1">
      <div
        v-for="(video, index) in videos"
        :key="video.bvid"
        class="group grid grid-cols-12 gap-4 px-4 py-2.5 rounded-xl items-center hover:bg-surface-hover/80 transition-all cursor-pointer select-none"
        :class="isCurrentPlaying(video) ? 'bg-surface-panel/90 text-white' : 'text-neutral-300'"
        @dblclick="emit('play', video, 0)"
      >
        <!-- 序号 / 播放动画指示器 -->
        <div class="col-span-1 flex items-center justify-center">
          <div v-if="isCurrentPlaying(video)" class="flex items-end gap-0.5 h-3">
            <span class="w-0.5 h-full bg-bili-hot rounded-full animate-bounce [animation-delay:-0.3s]"></span>
            <span class="w-0.5 h-2/3 bg-bili-hot rounded-full animate-bounce [animation-delay:-0.1s]"></span>
            <span class="w-0.5 h-4/5 bg-bili-hot rounded-full animate-bounce [animation-delay:-0.2s]"></span>
          </div>
          <span v-else class="text-xs font-mono text-neutral-500 group-hover:hidden">
            {{ (index + 1).toString().padStart(2, '0') }}
          </span>
          <button
            v-if="!isCurrentPlaying(video)"
            class="hidden group-hover:flex w-6 h-6 rounded-full bg-bili-hot text-white items-center justify-center shadow-glow transition-transform active:scale-90"
            @click.stop="emit('play', video, 0)"
          >
            <Play class="w-3 h-3 fill-current ml-0.5" />
          </button>
        </div>

        <!-- 封面与标题 -->
        <div class="col-span-6 flex items-center gap-3 min-w-0">
          <div class="relative w-11 h-11 rounded-lg overflow-hidden shrink-0 bg-neutral-800 border border-surface-border">
            <img
              :src="video.pic"
              :alt="video.title"
              class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
              loading="lazy"
            />
          </div>
          <div class="flex flex-col min-w-0 pr-2">
            <div class="flex items-center gap-2">
              <span
                class="text-xs font-medium truncate group-hover:text-white transition-colors"
                :class="isCurrentPlaying(video) ? 'text-bili-hot font-semibold' : ''"
              >
                {{ video.title }}
              </span>
              <!-- 分P数量徽章 -->
              <span
                v-if="video.pages.length > 1"
                class="shrink-0 text-[10px] font-semibold px-1.5 py-0.2 rounded bg-surface-panel text-bili-accent border border-bili-accent/30 flex items-center gap-0.5"
                title="合集分P"
                @click.stop="emit('open-pages', video)"
              >
                <Layers class="w-2.5 h-2.5" />
                {{ video.pages.length }}P
              </span>
            </div>
            <!-- 如果正在播这首歌的多P，显示当前分P名字 -->
            <span v-if="isCurrentPlaying(video) && player.currentPage" class="text-[11px] text-neutral-400 truncate">
              当前: {{ player.currentPage.part }}
            </span>
          </div>
        </div>

        <!-- UP主 -->
        <div class="col-span-3 flex items-center gap-1.5 text-xs text-neutral-400 truncate">
          <User class="w-3 h-3 text-neutral-500 shrink-0" />
          <span class="truncate hover:text-neutral-200 transition-colors">{{ video.owner_name }}</span>
        </div>

        <!-- 悬停操作与时长 -->
        <div class="col-span-2 flex items-center justify-end gap-2 text-xs text-neutral-400 font-mono">
          <div class="hidden group-hover:flex items-center gap-1">
            <button
              v-if="video.pages.length > 1"
              title="查看选集/分P"
              class="w-7 h-7 rounded-lg hover:bg-surface-panel text-neutral-400 hover:text-white flex items-center justify-center transition-colors"
              @click.stop="emit('open-pages', video)"
            >
              <ListMusic class="w-3.5 h-3.5" />
            </button>
            <button
              title="加入播放队列"
              class="w-7 h-7 rounded-lg hover:bg-surface-panel text-neutral-400 hover:text-white flex items-center justify-center transition-colors"
              @click.stop="player.addToQueue(video, 0)"
            >
              <Plus class="w-3.5 h-3.5" />
            </button>
            <button
              title="一键缓存整张合集"
              class="w-7 h-7 rounded-lg hover:bg-surface-panel text-neutral-400 hover:text-white flex items-center justify-center transition-colors"
              @click.stop="emit('cache-all', video)"
            >
              <Download class="w-3.5 h-3.5" />
            </button>
          </div>
          <span class="group-hover:hidden">
            {{ formatDuration(video.pages[0]?.duration || video.duration) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
