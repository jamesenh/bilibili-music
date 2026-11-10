/** 集中封装Tauri调用，浏览器预览回退到演示数据。 */
import { invoke } from '@tauri-apps/api/core'
import type { Video, PlayHistoryItem, CachedTrackItem, FavFolder, UserInfo } from '@/types/music'
import { MOCK_VIDEOS } from '@/stores/mockData'

/**
 * 最小化窗口
 */
export async function apiWindowMinimize(): Promise<void> {
  if (isTauri()) {
    await invoke('window_minimize')
  }
}

/**
 * 最大化或还原窗口
 */
export async function apiWindowToggleMaximize(): Promise<void> {
  if (isTauri()) {
    await invoke('window_toggle_maximize')
  }
}

/**
 * 关闭窗口
 */
export async function apiWindowClose(): Promise<void> {
  if (isTauri()) {
    await invoke('window_close')
  }
}

/**
 * 查询窗口是否最大化
 */
export async function apiWindowIsMaximized(): Promise<boolean> {
  if (isTauri()) {
    return await invoke<boolean>('window_is_maximized')
  }
  return false
}

/**
 * 判断当前是否运行在 Tauri 桌面容器内
 */
export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

/**
 * 搜索视频
 */
export async function apiSearch(keyword: string, page = 1): Promise<Video[]> {
  if (isTauri()) {
    return await invoke<Video[]>('search_videos', { keyword, page })
  }
  // 浏览器 Mock 退守逻辑
  await new Promise(r => setTimeout(r, 200))
  const q = keyword.toLowerCase()
  return MOCK_VIDEOS.filter(
    v => v.title.toLowerCase().includes(q) || v.owner_name.toLowerCase().includes(q)
  )
}

/**
 * 获取视频详情（补全分P列表）
 */
export async function apiGetVideoDetail(bvid: string): Promise<Video> {
  if (isTauri()) {
    return await invoke<Video>('get_video_detail', { bvid })
  }
  const found = MOCK_VIDEOS.find(v => v.bvid === bvid)
  if (found) return found
  return MOCK_VIDEOS[0]
}

/**
 * 解析并缓存指定音频
 * 返回本地文件路径（在 Tauri 中可直接使用 asset:// 协议转化为可播放 URL）
 */
export async function apiResolveAudio(
  video: Video,
  pageIndex: number,
  qualityId: number
): Promise<string> {
  if (isTauri()) {
    return await invoke<string>('resolve_and_cache_audio', {
      video,
      pageIndex,
      qualityId,
    })
  }
  // 浏览器开发环境返回空串（使用内置计时器模拟播放）
  return ''
}

/**
 * 获取最近播放历史
 */
export async function apiGetHistory(): Promise<PlayHistoryItem[]> {
  if (isTauri()) {
    return await invoke<PlayHistoryItem[]>('get_play_history')
  }
  return []
}

/**
 * 记录播放历史
 */
export async function apiRecordHistory(item: PlayHistoryItem): Promise<void> {
  if (isTauri()) {
    await invoke('record_play_history', { item })
  }
}

/**
 * 清空播放历史
 */
export async function apiClearPlayHistory(): Promise<void> {
  if (isTauri()) {
    await invoke('clear_play_history')
  }
}

/**
 * 获取本地缓存曲目列表
 */
export async function apiGetCachedTracks(): Promise<CachedTrackItem[]> {
  if (isTauri()) {
    return await invoke<CachedTrackItem[]>('get_cached_tracks')
  }
  return []
}

/**
 * 获取搜索历史
 */
export async function apiGetSearchHistory(): Promise<string[]> {
  if (isTauri()) {
    return await invoke<string[]>('get_search_history')
  }
  return ['周杰伦 经典合集', 'Only My Railgun', '告白气球', '富士山下']
}

/**
 * 获取当前账号信息
 */
export async function apiGetUserInfo(): Promise<UserInfo> {
  if (isTauri()) {
    return await invoke<UserInfo>('get_user_info')
  }
  return {
    is_login: false,
    uname: '',
    mid: 0,
    avatar: '',
  }
}

/**
 * 保存用户 Cookie（返回校验后的用户信息）
 */
export async function apiSaveCookie(cookieStr: string): Promise<UserInfo> {
  if (isTauri()) {
    return await invoke<UserInfo>('save_cookie', { cookieStr })
  }
  return {
    is_login: true,
    uname: '网页端用户',
    mid: 10001,
    avatar: '',
  }
}

/**
 * 登出
 */
export async function apiLogout(): Promise<void> {
  if (isTauri()) {
    await invoke('logout')
  }
}

/**
 * 获取凭据文件路径
 */
export async function apiGetSessionPath(): Promise<string> {
  if (isTauri()) {
    return await invoke<string>('get_session_path')
  }
  return '%APPDATA%\\BiliMusic\\session.json'
}

/**
 * 获取当前登录用户的收藏夹列表
 */
export async function apiGetUserFavFolders(): Promise<FavFolder[]> {
  if (isTauri()) {
    return await invoke<FavFolder[]>('get_user_fav_folders')
  }
  return [
    { id: 101, title: '最爱听的ACG/燃曲', media_count: 18 },
    { id: 102, title: '粤语老歌金曲精选', media_count: 42 },
  ]
}

/**
 * 获取指定收藏夹内的视频列表
 */
export async function apiGetFavResources(mediaId: number, page = 1): Promise<Video[]> {
  if (isTauri()) {
    return await invoke<Video[]>('get_fav_resources', { mediaId, page })
  }
  return []
}
