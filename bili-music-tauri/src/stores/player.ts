/** 集中管理队列与本地音频播放，浏览器预览使用演示状态。 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { convertFileSrc } from '@tauri-apps/api/core'
import type { Video, Page, PlayMode, AudioQuality, QueueItem } from '@/types/music'
import {
  isTauri,
  apiGetVideoDetail,
  apiResolveAudio,
  apiRecordHistory,
} from '@/services/tauriBridge'

export const usePlayerStore = defineStore('player', () => {
  // 当前播放
  const currentVideo = ref<Video | null>(null)
  const currentPageIndex = ref<number>(0)
  const quality = ref<AudioQuality>(30280)
  const isResolving = ref<boolean>(false)

  // 播放队列
  const queue = ref<QueueItem[]>([])
  const currentIndex = ref<number>(-1)
  const isQueueOpen = ref<boolean>(false)

  // 播放状态
  const isPlaying = ref<boolean>(false)
  const currentTime = ref<number>(0)
  const duration = ref<number>(0)
  const volume = ref<number>(0.8)
  const isMuted = ref<boolean>(false)
  const playMode = ref<PlayMode>('list_loop')

  // HTML5 Audio 原生实例 (在 Tauri 中原生硬解 AAC/FLAC)
  const audioElement = typeof Audio !== 'undefined' ? new Audio() : null

  if (audioElement) {
    audioElement.volume = volume.value

    audioElement.addEventListener('timeupdate', () => {
      currentTime.value = audioElement.currentTime
    })

    audioElement.addEventListener('loadedmetadata', () => {
      if (audioElement.duration && !isNaN(audioElement.duration)) {
        duration.value = audioElement.duration
      }
    })

    audioElement.addEventListener('ended', () => {
      nextTrack()
    })

    audioElement.addEventListener('play', () => {
      isPlaying.value = true
    })

    audioElement.addEventListener('pause', () => {
      isPlaying.value = false
    })
  }

  // 当前分P对象（分P铁律：时长和cid一律取分P自身）
  const currentPage = computed<Page | null>(() => {
    if (!currentVideo.value || !currentVideo.value.pages.length) return null
    return currentVideo.value.pages[currentPageIndex.value] || currentVideo.value.pages[0]
  })

  // 显示标题
  const displayTitle = computed(() => {
    if (!currentVideo.value) return '未播放'
    const page = currentPage.value
    if (page && currentVideo.value.pages.length > 1) {
      return page.part || currentVideo.value.title
    }
    return currentVideo.value.title
  })

  // 显示UP主
  const displayArtist = computed(() => {
    return currentVideo.value?.owner_name || 'Bilibili Music'
  })

  // 封面
  const displayCover = computed(() => {
    return currentVideo.value?.pic || ''
  })

  // 控制动作
  /** 补全分P、更新队列并解析本地音频；浏览器预览只更新演示状态。 */
  async function playVideo(video: Video, pageIndex = 0) {
    let targetVideo = video

    // 1. 如果分P未获取（如搜索结果直接点击），先补全分P
    if (!targetVideo.pages || targetVideo.pages.length === 0) {
      isResolving.value = true
      try {
        targetVideo = await apiGetVideoDetail(video.bvid)
      } catch (e) {
        console.error('获取分P失败', e)
      } finally {
        isResolving.value = false
      }
    }

    currentVideo.value = targetVideo
    currentPageIndex.value = pageIndex
    duration.value = targetVideo.pages[pageIndex]?.duration || 240
    currentTime.value = 0

    // 加入队列
    const itemId = `${targetVideo.bvid}_${targetVideo.pages[pageIndex]?.cid || 0}`
    const existingIdx = queue.value.findIndex((item: QueueItem) => item.id === itemId)
    if (existingIdx === -1) {
      queue.value.unshift({
        id: itemId,
        video: targetVideo,
        pageIndex,
      })
      currentIndex.value = 0
    } else {
      currentIndex.value = existingIdx
    }

    // 2. 解析/下载本地缓存并播放
    if (isTauri() && audioElement) {
      isResolving.value = true
      try {
        const localPath = await apiResolveAudio(targetVideo, pageIndex, quality.value)
        if (localPath) {
          const assetUrl = convertFileSrc(localPath)
          audioElement.src = assetUrl
          await audioElement.play()
          isPlaying.value = true

          // 记录播放历史
          const curPage = targetVideo.pages[pageIndex]
          await apiRecordHistory({
            bvid: targetVideo.bvid,
            cid: curPage?.cid || 0,
            title: targetVideo.title,
            part: curPage?.part || '',
            artist: targetVideo.owner_name,
            cover: targetVideo.pic,
            duration: curPage?.duration || duration.value,
            playedAt: Date.now(),
          })
        }
      } catch (err) {
        console.error('解析/播放失败', err)
      } finally {
        isResolving.value = false
      }
    } else {
      // 浏览器预览开发模式
      isPlaying.value = true
    }
  }

  /** 切换播放状态，尚未选歌时尝试播放队列首项。 */
  function togglePlay() {
    if (!currentVideo.value && queue.value.length > 0) {
      const first = queue.value[0]
      playVideo(first.video, first.pageIndex)
      return
    }

    if (audioElement && isTauri()) {
      if (audioElement.paused) {
        audioElement.play()
      } else {
        audioElement.pause()
      }
    } else {
      isPlaying.value = !isPlaying.value
    }
  }

  /** 在当前视频中选择零起始的分P索引并开始播放。 */
  function setPage(index: number) {
    if (!currentVideo.value) return
    playVideo(currentVideo.value, index)
  }

  /** 优先前进到视频的下一分P，再按队列模式选择后续项。 */
  function nextTrack() {
    if (queue.value.length === 0) return

    // 多P视频内自动播下一P
    if (currentVideo.value && currentPageIndex.value < currentVideo.value.pages.length - 1) {
      setPage(currentPageIndex.value + 1)
      return
    }

    if (playMode.value === 'shuffle') {
      const nextIdx = Math.floor(Math.random() * queue.value.length)
      const target = queue.value[nextIdx]
      playVideo(target.video, target.pageIndex)
      return
    }

    const nextIdx = (currentIndex.value + 1) % queue.value.length
    const target = queue.value[nextIdx]
    playVideo(target.video, target.pageIndex)
  }

  /** 已播放超过三秒时回到开头，否则回退分P或队列项。 */
  function prevTrack() {
    if (queue.value.length === 0) return

    if (currentTime.value > 3) {
      seek(0)
      return
    }

    if (currentVideo.value && currentPageIndex.value > 0) {
      setPage(currentPageIndex.value - 1)
      return
    }

    const prevIdx = (currentIndex.value - 1 + queue.value.length) % queue.value.length
    const target = queue.value[prevIdx]
    playVideo(target.video, target.pageIndex)
  }

  /** 在顺序、列表循环、单曲循环和随机选项间切换。 */
  function togglePlayMode() {
    const modes: PlayMode[] = ['sequence', 'list_loop', 'single_loop', 'shuffle']
    const curIdx = modes.indexOf(playMode.value)
    playMode.value = modes[(curIdx + 1) % modes.length]
  }

  /** 将播放位置限制在当前曲目时长内并同步音频元素。 */
  function seek(time: number) {
    currentTime.value = Math.max(0, Math.min(time, duration.value))
    if (audioElement && isTauri()) {
      audioElement.currentTime = currentTime.value
    }
  }

  /** 按视频与cid去重后把指定分P追加到队列。 */
  function addToQueue(video: Video, pageIndex = 0) {
    const itemId = `${video.bvid}_${video.pages[pageIndex]?.cid || 0}`
    if (queue.value.some((i: QueueItem) => i.id === itemId)) return
    queue.value.push({ id: itemId, video, pageIndex })
  }

  /** 清空队列并暂停、释放当前音频来源。 */
  function clearQueue() {
    queue.value = []
    currentIndex.value = -1
    isPlaying.value = false
    if (audioElement && isTauri()) {
      audioElement.pause()
      audioElement.src = ''
    }
  }

  return {
    currentVideo,
    currentPageIndex,
    currentPage,
    quality,
    queue,
    currentIndex,
    isQueueOpen,
    isPlaying,
    isResolving,
    currentTime,
    duration,
    volume,
    isMuted,
    playMode,
    displayTitle,
    displayArtist,
    displayCover,
    playVideo,
    togglePlay,
    setPage,
    nextTrack,
    prevTrack,
    togglePlayMode,
    seek,
    addToQueue,
    clearQueue,
  }
})
