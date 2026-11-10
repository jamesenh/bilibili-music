/**
 * 通用格式化工具函数
 */

/**
 * 格式化秒数为 mm:ss 或 hh:mm:ss
 */
export function formatDuration(seconds: number): string {
  if (isNaN(seconds) || seconds < 0) return '00:00'
  const s = Math.floor(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const restS = s % 60

  const mm = m.toString().padStart(2, '0')
  const ss = restS.toString().padStart(2, '0')

  if (h > 0) {
    return `${h}:${mm}:${ss}`
  }
  return `${mm}:${ss}`
}

/**
 * 格式化字节大小为可读文本 (KB / MB / GB)
 */
export function formatSize(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = bytes
  let unitIndex = 0

  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex++
  }

  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`
}

/**
 * 格式化相对时间 (刚刚 / 5分钟前 / 昨天 / 日期)
 */
export function formatRelativeTime(timestampMs: number): string {
  const diff = Date.now() - timestampMs
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour

  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`

  const date = new Date(timestampMs)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}
