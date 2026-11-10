/**
 * 音乐与 B 站数据模型契约
 * 沿用原Python版的分P语义，迁移中的前后端字段契约仍需逐项验证。
 */

/** 表示一首分P曲目，时长单位为秒。 */
export interface Page {
  cid: number
  page: number
  part: string
  duration: number // 秒
}

/** 表示视频元数据，pages由详情接口补全。 */
export interface Video {
  bvid: string
  aid: number
  title: string
  pic: string
  owner_name: string
  owner_mid: number
  duration: number // 所有分P总秒数
  pages: Page[]
}

export type PlayMode = 'sequence' | 'list_loop' | 'single_loop' | 'shuffle'

export type AudioQuality = 30280 | 30232 | 30216 | 30250 | 30251

/** 定义音质选择器的标识与中文展示文案。 */
export interface QualityOption {
  id: AudioQuality
  label: string
  desc: string
}

/** 常规音质选项；具体可用档位以音轨接口为准。 */
export const QUALITY_OPTIONS: QualityOption[] = [
  { id: 30280, label: '192K 高品质', desc: '常规最高音质' },
  { id: 30232, label: '132K 标准', desc: '标准音质' },
  { id: 30216, label: '64K 流畅', desc: '省流模式' },
]

/** 保存队列项及零起始分P索引，标识包含cid。 */
export interface QueueItem {
  id: string // `${video.bvid}_${page.cid}`
  video: Video
  pageIndex: number // 当前分P索引 (0-based)
}

/** 保存最近播放分P的展示数据与毫秒时间戳。 */
export interface PlayHistoryItem {
  bvid: string
  cid: number
  title: string
  part: string
  artist: string
  cover: string
  duration: number
  playedAt: number // 时间戳 (毫秒)
}

/** 保存本地缓存列表展示所需的分P与音质信息。 */
export interface CachedTrackItem {
  bvid: string
  cid: number
  title: string
  part: string
  artist: string
  cover: string
  duration: number
  qualityId: number
  sizeBytes: number
  cachedAt: number
}

/** 表示账号可读取的收藏夹条目。 */
export interface FavFolder {
  id: number
  title: string
  media_count: number
}

/** 保存公开账号信息与登录标志，不包含Cookie。 */
export interface UserInfo {
  is_login: boolean
  uname: string
  mid: number
  avatar: string
}
