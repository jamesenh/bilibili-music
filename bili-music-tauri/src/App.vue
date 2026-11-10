<script setup lang="ts">
/** 组装桌面视图与弹窗，通过桥接层加载列表，通过播放仓库转发点播。 */
import { ref, computed, onMounted, watch } from 'vue'
import TitleBar from '@/components/TitleBar.vue'
import Sidebar from '@/components/Sidebar.vue'
import TrackTable from '@/components/TrackTable.vue'
import PlayerBar from '@/components/PlayerBar.vue'
import QueueDrawer from '@/components/QueueDrawer.vue'
import PageSelectModal from '@/components/PageSelectModal.vue'
import AccountModal from '@/components/AccountModal.vue'
import { usePlayerStore } from '@/stores/player'
import { MOCK_VIDEOS } from '@/stores/mockData'
import type { Video, CachedTrackItem, PlayHistoryItem, FavFolder, UserInfo } from '@/types/music'
import {
  apiSearch,
  apiGetCachedTracks,
  apiGetHistory,
  apiClearPlayHistory,
  apiGetUserInfo,
  apiGetSessionPath,
  apiGetUserFavFolders,
  apiGetFavResources,
  isTauri
} from '@/services/tauriBridge'
import {
  HardDriveDownload,
  Clock,
  FolderHeart,
  Sparkles,
  Download,
  Trash2,
  Loader2
} from 'lucide-vue-next'
import { formatSize } from '@/utils/format'

const player = usePlayerStore()

// 视图导航：discover | cache | history | fav-{id}
const currentNav = ref('discover')
const currentQuery = ref('')
const isSearching = ref(false)

// 账号与会话状态
const userInfo = ref<UserInfo | null>(null)
const sessionPath = ref('')
const favFolders = ref<FavFolder[]>([])

// 模态弹层
const isAccountOpen = ref(false)
const isPageModalOpen = ref(false)
const selectedVideoForPages = ref<Video | null>(null)

// 歌曲数据
const searchVideos = ref<Video[]>(MOCK_VIDEOS)
const cachedTracks = ref<CachedTrackItem[]>([])
const cachedTotalSize = ref(0)
const historyItems = ref<PlayHistoryItem[]>([])
const favFolderVideos = ref<Video[]>([])
const currentFavTitle = ref('B站云端歌单')

// 适配数据类型到统一的 Video 模型供列表渲染
/** 将缓存记录适配为保留原cid和时长的单分P视频。 */
function cachedItemToVideo(c: CachedTrackItem): Video {
  return {
    bvid: c.bvid,
    aid: 0,
    title: c.title,
    pic: c.cover,
    owner_name: c.artist,
    owner_mid: 0,
    duration: c.duration,
    pages: [
      {
        cid: c.cid,
        page: 1,
        part: c.part || c.title,
        duration: c.duration,
      },
    ],
  }
}

/** 将播放记录适配为可供列表点播的单分P视频。 */
function historyItemToVideo(h: PlayHistoryItem): Video {
  return {
    bvid: h.bvid,
    aid: 0,
    title: h.title,
    pic: h.cover,
    owner_name: h.artist,
    owner_mid: 0,
    duration: h.duration,
    pages: [
      {
        cid: h.cid,
        page: 1,
        part: h.part || h.title,
        duration: h.duration,
      },
    ],
  }
}

// 缓存与历史对应的列表
const cachedVideos = computed(() => cachedTracks.value.map(cachedItemToVideo))
const historyVideos = computed(() => historyItems.value.map(historyItemToVideo))

// 当前展示的视频列表
const currentDisplayVideos = computed(() => {
  if (currentNav.value === 'discover') return searchVideos.value
  if (currentNav.value === 'cache') return cachedVideos.value
  if (currentNav.value === 'history') return historyVideos.value
  if (currentNav.value.startsWith('fav-')) return favFolderVideos.value
  return searchVideos.value
})

// 搜索视频
/** 切换到搜索视图并加载关键词结果，失败时释放忙碌状态。 */
async function handleSearch(query: string) {
  currentQuery.value = query
  currentNav.value = 'discover'
  isSearching.value = true

  try {
    const results = await apiSearch(query)
    searchVideos.value = results
  } catch (err) {
    console.error('搜索异常', err)
  } finally {
    isSearching.value = false
  }
}

// 刷新并重载各面板数据
/** 刷新缓存展示列表与总占用，读取失败时保留当前界面。 */
async function refreshCacheList() {
  try {
    const list = await apiGetCachedTracks()
    cachedTracks.value = list
    cachedTotalSize.value = list.reduce((acc, cur) => acc + cur.sizeBytes, 0)
  } catch (e) {
    console.error('获取缓存曲目失败', e)
  }
}

/** 从桥接层刷新最近播放列表。 */
async function refreshHistoryList() {
  try {
    historyItems.value = await apiGetHistory()
  } catch (e) {
    console.error('获取播放历史失败', e)
  }
}

/** 清空持久化播放历史并同步当前列表。 */
async function handleClearHistory() {
  try {
    await apiClearPlayHistory()
    historyItems.value = []
  } catch (e) {
    console.error('清空播放历史失败', e)
  }
}

/** 刷新当前账号可读取的收藏夹列表。 */
async function refreshFavFolders() {
  try {
    favFolders.value = await apiGetUserFavFolders()
  } catch (e) {
    console.error('获取收藏夹失败', e)
  }
}

/** 根据导航选择刷新对应列表，收藏夹内容按第一页加载。 */
async function handleNavChange(nav: string) {
  currentNav.value = nav
  if (nav === 'cache') {
    await refreshCacheList()
  } else if (nav === 'history') {
    await refreshHistoryList()
  } else if (nav.startsWith('fav-')) {
    const folderId = parseInt(nav.replace('fav-', ''), 10)
    const folder = favFolders.value.find((f) => f.id === folderId)
    if (folder) {
      currentFavTitle.value = folder.title
    }
    isSearching.value = true
    try {
      favFolderVideos.value = await apiGetFavResources(folderId, 1)
    } catch (e) {
      console.error('拉取收藏夹歌曲失败', e)
      favFolderVideos.value = []
    } finally {
      isSearching.value = false
    }
  }
}

/** 把用户选择的视频与分P交给播放状态仓库。 */
function handlePlay(video: Video, pageIndex = 0) {
  player.playVideo(video, pageIndex)
}

/** 保存待选择的视频并打开分P选择器。 */
function handleOpenPages(video: Video) {
  selectedVideoForPages.value = video
  isPageModalOpen.value = true
}

/** 播放弹窗选中的分P并关闭选择器。 */
function handleSelectPage(pageIndex: number) {
  if (selectedVideoForPages.value) {
    player.playVideo(selectedVideoForPages.value, pageIndex)
    isPageModalOpen.value = false
  }
}

/** 更新账号展示信息并刷新收藏夹。 */
function handleLoginSuccess(user: UserInfo) {
  userInfo.value = user
  refreshFavFolders()
}

/** 清空账号与收藏夹展示,必要时回到发现页面。 */
function handleLogoutSuccess() {
  userInfo.value = null
  favFolders.value = []
  if (currentNav.value.startsWith('fav-')) {
    currentNav.value = 'discover'
  }
}

// 模拟纯网页环境下的进度推进
let mockTimer: number | null = null
watch(
  () => player.isPlaying,
  (playing) => {
    if (!isTauri()) {
      if (playing) {
        mockTimer = window.setInterval(() => {
          if (player.currentTime >= player.duration) {
            player.nextTrack()
          } else {
            player.currentTime += 1
          }
        }, 1000)
      } else if (mockTimer) {
        clearInterval(mockTimer)
        mockTimer = null
      }
    }
  }
)

onMounted(async () => {
  // 1. 初始化账号与凭据信息
  try {
    userInfo.value = await apiGetUserInfo()
    sessionPath.value = await apiGetSessionPath()
    if (userInfo.value?.is_login) {
      await refreshFavFolders()
    }
  } catch (e) {
    console.error('加载用户信息失败', e)
  }

  // 2. 初始化缓存统计
  await refreshCacheList()

  // 3. 初始首曲
  if (searchVideos.value.length > 0) {
    player.currentVideo = searchVideos.value[0]
    player.currentPageIndex = 0
    player.duration = searchVideos.value[0].pages[0]?.duration || 269
  }
})
</script>

<template>
  <div class="flex flex-col h-screen w-screen overflow-hidden bg-[#0e0e11] text-neutral-100 font-sans">
    <!-- 顶部标题栏（支持原生无边框拖拽移动与窗口控制） -->
    <TitleBar
      :active-nav="currentNav"
      :user-name="userInfo?.uname"
      :user-avatar="userInfo?.avatar"
      @search="handleSearch"
      @open-account="isAccountOpen = true"
    />

    <!-- 主体：侧边栏 + 内容区 + 播放队列抽屉 -->
    <div class="flex flex-1 min-h-0 overflow-hidden relative">
      <!-- 左侧边栏 -->
      <Sidebar
        :current-nav="currentNav"
        :fav-folders="favFolders"
        :cached-size-str="formatSize(cachedTotalSize)"
        @change-nav="handleNavChange"
        @refresh-favs="refreshFavFolders"
      />

      <!-- 中间内容滚动区 -->
      <main class="flex-1 overflow-y-auto px-6 py-5 flex flex-col gap-6">
        <!-- 发现 / 搜索结果 -->
        <div v-if="currentNav === 'discover'" class="flex flex-col gap-4">
          <!-- Banner 卡片 -->
          <div class="relative rounded-2xl overflow-hidden p-6 bg-gradient-to-r from-bili-hot/30 via-purple-900/20 to-bili-pink/20 border border-surface-border">
            <div class="relative z-10 flex flex-col gap-2 max-w-lg">
              <span class="text-xs font-semibold uppercase tracking-wider text-bili-hot flex items-center gap-1.5">
                <Sparkles class="w-3.5 h-3.5" />
                哔哩哔哩音乐区精选
              </span>
              <h1 class="text-xl font-bold text-white tracking-wide">
                {{ currentQuery ? `搜索结果: “${currentQuery}”` : '发现海量多P音乐合集与原创MV' }}
              </h1>
              <p class="text-xs text-neutral-400">
                将视频与分P当作歌单随心畅听，流式无缝缓存到本地，零网络延迟播放。
              </p>
            </div>
          </div>

          <!-- 搜索加载指示 -->
          <div v-if="isSearching" class="py-12 flex flex-col items-center justify-center gap-2 text-neutral-400">
            <Loader2 class="w-6 h-6 animate-spin text-bili-hot" />
            <span class="text-xs">正在搜索 B 站音乐内容...</span>
          </div>

          <!-- 歌曲列表 -->
          <div v-else class="flex flex-col gap-2">
            <div class="flex items-center justify-between">
              <h2 class="text-sm font-semibold text-white">
                {{ currentQuery ? '搜索曲目' : '推荐合集' }}
              </h2>
              <span class="text-xs text-neutral-500">共 {{ searchVideos.length }} 项结果</span>
            </div>
            <TrackTable
              :videos="searchVideos"
              @play="handlePlay"
              @open-pages="handleOpenPages"
            />
          </div>
        </div>

        <!-- 本地缓存页 -->
        <div v-else-if="currentNav === 'cache'" class="flex flex-col gap-4">
          <div class="flex items-center justify-between pb-3 border-b border-surface-border">
            <div class="flex flex-col gap-1">
              <div class="flex items-center gap-2">
                <HardDriveDownload class="w-5 h-5 text-bili-hot" />
                <h1 class="text-base font-bold text-white">本地离线缓存</h1>
              </div>
              <span class="text-xs text-neutral-400">
                已缓存 {{ cachedTracks.length }} 首歌曲 · 占用空间 {{ formatSize(cachedTotalSize) }}（离线点播零网络延迟）
              </span>
            </div>
          </div>
          <TrackTable
            :videos="cachedVideos"
            @play="handlePlay"
            @open-pages="handleOpenPages"
          />
        </div>

        <!-- 最近播放 -->
        <div v-else-if="currentNav === 'history'" class="flex flex-col gap-4">
          <div class="flex items-center justify-between pb-3 border-b border-surface-border">
            <div class="flex items-center gap-2">
              <Clock class="w-5 h-5 text-bili-hot" />
              <h1 class="text-base font-bold text-white">最近播放历史</h1>
            </div>
            <button
              v-if="historyItems.length > 0"
              class="text-xs text-neutral-400 hover:text-red-400 transition-colors"
              @click="handleClearHistory"
            >
              清空历史记录
            </button>
          </div>
          <TrackTable
            :videos="historyVideos"
            @play="handlePlay"
            @open-pages="handleOpenPages"
          />
        </div>

        <!-- 收藏夹展示 -->
        <div v-else-if="currentNav.startsWith('fav-')" class="flex flex-col gap-4">
          <div class="flex items-center justify-between pb-3 border-b border-surface-border">
            <div class="flex items-center gap-2">
              <FolderHeart class="w-5 h-5 text-bili-hot" />
              <h1 class="text-base font-bold text-white">{{ currentFavTitle }}</h1>
            </div>
            <span class="text-xs text-neutral-400">共 {{ favFolderVideos.length }} 首歌曲</span>
          </div>
          <div v-if="isSearching" class="py-12 flex flex-col items-center justify-center gap-2 text-neutral-400">
            <Loader2 class="w-6 h-6 animate-spin text-bili-hot" />
            <span class="text-xs">正在拉取收藏夹歌曲...</span>
          </div>
          <TrackTable
            v-else
            :videos="favFolderVideos"
            @play="handlePlay"
            @open-pages="handleOpenPages"
          />
        </div>
      </main>

      <!-- 播放队列抽屉面板 -->
      <QueueDrawer />
    </div>

    <!-- 底部吸底播放条 -->
    <PlayerBar @open-pages="handleOpenPages(player.currentVideo || searchVideos[0])" />

    <!-- 多P选择弹窗 -->
    <PageSelectModal
      :video="selectedVideoForPages"
      :is-open="isPageModalOpen"
      @close="isPageModalOpen = false"
      @select-page="handleSelectPage"
    />

    <!-- 账号登录弹窗 -->
    <AccountModal
      :is-open="isAccountOpen"
      :user-info="userInfo"
      :session-path="sessionPath"
      @close="isAccountOpen = false"
      @login-success="handleLoginSuccess"
      @logout-success="handleLogoutSuccess"
    />
  </div>
</template>
