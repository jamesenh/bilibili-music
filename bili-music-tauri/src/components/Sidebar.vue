<script setup lang="ts">
/** 展示页面导航与收藏夹入口，通过事件通知上层切换。 */
import {
  Compass,
  HardDriveDownload,
  Clock,
  FolderHeart,
  RefreshCw,
  FileText
} from 'lucide-vue-next'
import type { FavFolder } from '@/types/music'

const props = defineProps<{
  currentNav: string
  favFolders?: FavFolder[]
  cachedSizeStr?: string
}>()

const emit = defineEmits<{
  (e: 'change-nav', nav: string): void
  (e: 'refresh-favs'): void
}>()
</script>

<template>
  <aside class="w-56 bg-surface-subtle border-r border-surface-border flex flex-col justify-between py-4 select-none shrink-0">
    <!-- 主导航 -->
    <div class="flex flex-col gap-6 px-3">
      <!-- 推荐与浏览 -->
      <div class="flex flex-col gap-1">
        <div class="px-3 text-[11px] font-semibold text-neutral-500 uppercase tracking-wider mb-1">
          在线发现
        </div>
        <button
          class="flex items-center gap-3 px-3 py-2 rounded-xl text-xs font-medium transition-all"
          :class="currentNav === 'discover'
            ? 'bg-gradient-to-r from-bili-hot to-bili-pink text-white shadow-glow/30 font-semibold'
            : 'text-neutral-400 hover:text-neutral-200 hover:bg-surface-hover'"
          @click="emit('change-nav', 'discover')"
        >
          <Compass class="w-4 h-4" />
          <span>音乐发现</span>
        </button>
      </div>

      <!-- 本地与历史 -->
      <div class="flex flex-col gap-1">
        <div class="px-3 text-[11px] font-semibold text-neutral-500 uppercase tracking-wider mb-1">
          我的音乐
        </div>
        <button
          class="flex items-center justify-between px-3 py-2 rounded-xl text-xs font-medium transition-all"
          :class="currentNav === 'cache'
            ? 'bg-gradient-to-r from-bili-hot to-bili-pink text-white shadow-glow/30 font-semibold'
            : 'text-neutral-400 hover:text-neutral-200 hover:bg-surface-hover'"
          @click="emit('change-nav', 'cache')"
        >
          <div class="flex items-center gap-3">
            <HardDriveDownload class="w-4 h-4" />
            <span>本地缓存</span>
          </div>
          <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-white/10 text-neutral-300">
            {{ cachedSizeStr || '0 B' }}
          </span>
        </button>

        <button
          class="flex items-center gap-3 px-3 py-2 rounded-xl text-xs font-medium transition-all"
          :class="currentNav === 'history'
            ? 'bg-gradient-to-r from-bili-hot to-bili-pink text-white shadow-glow/30 font-semibold'
            : 'text-neutral-400 hover:text-neutral-200 hover:bg-surface-hover'"
          @click="emit('change-nav', 'history')"
        >
          <Clock class="w-4 h-4" />
          <span>最近播放</span>
        </button>
      </div>

      <!-- B 站收藏夹当歌单 -->
      <div class="flex flex-col gap-1">
        <div class="flex items-center justify-between px-3 mb-1">
          <span class="text-[11px] font-semibold text-neutral-500 uppercase tracking-wider">
            B站收藏夹
          </span>
          <div class="flex items-center gap-1">
            <button
              title="刷新收藏夹"
              class="text-neutral-500 hover:text-neutral-300 p-0.5 rounded hover:bg-surface-hover transition-colors"
              @click="emit('refresh-favs')"
            >
              <RefreshCw class="w-3 h-3" />
            </button>
          </div>
        </div>

        <div class="flex flex-col gap-0.5">
          <button
            v-for="folder in (favFolders || [])"
            :key="folder.id"
            class="flex items-center justify-between px-3 py-2 rounded-xl text-xs font-medium transition-all text-neutral-400 hover:text-neutral-200 hover:bg-surface-hover"
            :class="currentNav === `fav-${folder.id}` ? 'bg-surface-panel text-white font-semibold' : ''"
            @click="emit('change-nav', `fav-${folder.id}`)"
          >
            <div class="flex items-center gap-2.5 line-clamp-1">
              <FolderHeart class="w-3.5 h-3.5 text-bili-hot shrink-0" />
              <span class="truncate">{{ folder.title }}</span>
            </div>
            <span class="text-[10px] text-neutral-500 shrink-0">{{ folder.media_count }}</span>
          </button>

          <div
            v-if="!favFolders || favFolders.length === 0"
            class="px-3 py-2 text-[11px] text-neutral-500 italic"
          >
            登录后同步歌单
          </div>
        </div>
      </div>
    </div>

    <!-- 底部状态与日志入口 -->
    <div class="px-3 pt-3 border-t border-surface-border flex items-center justify-between text-neutral-500 text-[11px]">
      <span class="flex items-center gap-1.5 hover:text-neutral-400 cursor-pointer transition-colors">
        <FileText class="w-3.5 h-3.5" />
        运行状态
      </span>
      <span class="text-[10px] text-neutral-600">v0.1.0</span>
    </div>
  </aside>
</template>
