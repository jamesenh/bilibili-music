<script setup lang="ts">
/** 展示搜索历史与窗口控制，桌面操作统一走桥接层。 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  Search,
  History,
  X,
  Minus,
  Square,
  Copy,
  Sparkles,
  User,
  Music2,
  Trash2
} from 'lucide-vue-next'
import {
  apiWindowMinimize,
  apiWindowToggleMaximize,
  apiWindowClose,
  apiWindowIsMaximized
} from '@/services/tauriBridge'

const props = defineProps<{
  activeNav: string
  userName?: string
  userAvatar?: string
}>()

const emit = defineEmits<{
  (e: 'search', query: string): void
  (e: 'open-account'): void
}>()

const searchQuery = ref('')
const isFocus = ref(false)
const searchHistory = ref<string[]>([
  '周杰伦 经典合集',
  'Only My Railgun',
  '告白气球',
  '富士山下',
  'RADWIMPS 铃芽之旅',
])

// 搜索历史联想（按包含匹配过滤，最多 8 条）
const filteredHistory = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return searchHistory.value.slice(0, 8)
  return searchHistory.value
    .filter(item => item.toLowerCase().includes(q))
    .slice(0, 8)
})

/** 提交非空关键词并更新当前搜索历史展示。 */
function submitSearch(term?: string) {
  const target = (term !== undefined ? term : searchQuery.value).trim()
  if (!target) return
  searchQuery.value = target
  isFocus.value = false

  // 记录到历史（去重，置顶）
  searchHistory.value = [target, ...searchHistory.value.filter(i => i !== target)].slice(0, 30)
  emit('search', target)
}

/** 从本次界面会话的搜索历史中移除指定关键词。 */
function removeHistoryItem(e: Event, item: string) {
  e.stopPropagation()
  searchHistory.value = searchHistory.value.filter(i => i !== item)
}

/** 清空当前界面会话的历史下拉列表。 */
function clearAllHistory(e: Event) {
  e.stopPropagation()
  searchHistory.value = []
}

// 窗口控制与最大化监听
const isMaximized = ref(false)

/** 查询桌面窗口最大化状态以同步控制按钮。 */
async function checkMaximized() {
  isMaximized.value = await apiWindowIsMaximized()
}

/** 经桥接层请求最小化桌面窗口。 */
async function minimizeWindow() {
  await apiWindowMinimize()
}

/** 切换窗口最大化状态并刷新按钮。 */
async function toggleMaximizeWindow() {
  await apiWindowToggleMaximize()
  // 切换后延迟小段时间重检状态
  setTimeout(checkMaximized, 100)
}

/** 经桥接层请求关闭桌面窗口。 */
async function closeWindow() {
  await apiWindowClose()
}

/** 标题栏空白区域双击时切换最大化状态。 */
function handleHeaderDblClick(e: MouseEvent) {
  const target = e.target as HTMLElement | null
  // 如果双击在输入框、按钮或下拉浮层上，不触发最大化切换
  if (target?.closest('input, button, .cursor-pointer')) return
  toggleMaximizeWindow()
}

onMounted(async () => {
  await checkMaximized()
  window.addEventListener('resize', checkMaximized)
})

onUnmounted(() => {
  window.removeEventListener('resize', checkMaximized)
})
</script>

<template>
  <header
    data-tauri-drag-region
    class="h-12 bg-surface-subtle/80 backdrop-blur-md border-b border-surface-border flex items-center justify-between px-4 z-40 select-none relative cursor-default"
    @dblclick="handleHeaderDblClick"
  >
    <!-- 左侧 Logo 与品牌 -->
    <div class="flex items-center gap-2.5 w-52 shrink-0">
      <div class="w-7 h-7 rounded-lg bg-gradient-to-tr from-bili-hot to-bili-pink flex items-center justify-center shadow-glow">
        <Music2 class="w-4 h-4 text-white" />
      </div>
      <div class="flex items-center gap-1.5">
        <span class="font-bold text-sm tracking-wide text-white">BiliMusic</span>
        <span class="text-[10px] uppercase font-semibold tracking-wider px-1.5 py-0.5 rounded bg-bili-hot/20 text-bili-hot border border-bili-hot/30">
          Tauri
        </span>
      </div>
    </div>

    <!-- 中间：搜索框与历史联想浮层 -->
    <div class="relative w-full max-w-md">
      <div
        class="h-8 rounded-full bg-surface-panel/90 border border-surface-border hover:border-neutral-500 focus-within:!border-bili-hot focus-within:shadow-glow/20 transition-all flex items-center px-3 gap-2"
      >
        <Search class="w-3.5 h-3.5 text-neutral-400 shrink-0" />
        <input
          v-model="searchQuery"
          type="text"
          placeholder="搜索歌曲、歌手、合集或 BV 号..."
          class="w-full bg-transparent text-xs text-neutral-200 placeholder-neutral-500 outline-none"
          @focus="isFocus = true"
          @keydown.enter="submitSearch()"
        />
        <button
          v-if="searchQuery"
          class="text-neutral-500 hover:text-neutral-300 transition-colors"
          @click="searchQuery = ''"
        >
          <X class="w-3.5 h-3.5" />
        </button>
      </div>

      <!-- 搜索历史下拉框 -->
      <div
        v-if="isFocus && filteredHistory.length > 0"
        class="absolute top-10 left-0 right-0 bg-surface-panel/95 backdrop-blur-xl border border-surface-border rounded-xl shadow-modal overflow-hidden p-2 z-50 animate-in fade-in zoom-in-95 duration-150"
      >
        <div class="flex items-center justify-between px-2 py-1 text-[11px] text-neutral-400 font-medium">
          <span class="flex items-center gap-1">
            <History class="w-3 h-3 text-neutral-500" />
            搜索历史
          </span>
          <button
            class="hover:text-red-400 text-[10px] transition-colors flex items-center gap-0.5"
            @mousedown="clearAllHistory($event)"
          >
            <Trash2 class="w-2.5 h-2.5" />
            清空
          </button>
        </div>

        <div class="mt-1 flex flex-col gap-0.5">
          <div
            v-for="item in filteredHistory"
            :key="item"
            class="group flex items-center justify-between px-2.5 py-1.5 rounded-lg hover:bg-surface-hover text-xs text-neutral-300 hover:text-white cursor-pointer transition-colors"
            @mousedown="submitSearch(item)"
          >
            <span class="line-clamp-1">{{ item }}</span>
            <button
              class="opacity-0 group-hover:opacity-100 text-neutral-500 hover:text-neutral-300 p-0.5 rounded transition-all"
              @mousedown="removeHistoryItem($event, item)"
            >
              <X class="w-3 h-3" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 右侧：账号与窗口控制 -->
    <div class="flex items-center gap-2">
      <!-- 账号按钮 -->
      <button
        class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs bg-surface-hover hover:bg-neutral-800 text-neutral-300 hover:text-white border border-surface-border transition-colors"
        @click="emit('open-account')"
      >
        <div class="w-5 h-5 rounded-full bg-neutral-700 flex items-center justify-center overflow-hidden shrink-0">
          <img
            v-if="userAvatar"
            :src="userAvatar"
            :alt="userName || '用户头像'"
            class="w-full h-full object-cover"
          />
          <User v-else class="w-3 h-3 text-neutral-400" />
        </div>
        <span class="font-medium text-[11px] max-w-[90px] truncate">{{ userName || '未登录' }}</span>
      </button>

      <!-- 窗口控制按钮 -->
      <div class="flex items-center ml-2 border-l border-surface-border pl-2 gap-1 text-neutral-400">
        <button
          title="最小化"
          class="w-6 h-6 rounded flex items-center justify-center hover:bg-surface-hover hover:text-white transition-colors"
          @click="minimizeWindow"
        >
          <Minus class="w-3 h-3" />
        </button>
        <button
          :title="isMaximized ? '向下还原' : '最大化'"
          class="w-6 h-6 rounded flex items-center justify-center hover:bg-surface-hover hover:text-white transition-colors"
          @click="toggleMaximizeWindow"
        >
          <Copy v-if="isMaximized" class="w-2.5 h-2.5" />
          <Square v-else class="w-2.5 h-2.5" />
        </button>
        <button
          title="关闭"
          class="w-6 h-6 rounded flex items-center justify-center hover:bg-red-500/80 hover:text-white transition-colors"
          @click="closeWindow"
        >
          <X class="w-3 h-3" />
        </button>
      </div>
    </div>
  </header>
</template>
