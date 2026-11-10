/** 提供浏览器预览数据，使界面预览无需真实账号与网络请求。 */
import type { Video } from '@/types/music'

export const MOCK_VIDEOS: Video[] = [
  {
    bvid: 'BV1GJ411x7h7',
    aid: 170001,
    title: '【周杰伦】出道以来的全部经典歌曲精选MV合集【高音质】',
    pic: 'https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500&auto=format&fit=crop&q=60',
    owner_name: '音乐留声机',
    owner_mid: 10001,
    duration: 1850,
    pages: [
      { cid: 1001, page: 1, part: '01. 晴天 - 周杰伦 (叶惠美)', duration: 269 },
      { cid: 1002, page: 2, part: '02. 七里香 - 周杰伦 (七里香)', duration: 299 },
      { cid: 1003, page: 3, part: '03. 稻香 - 周杰伦 (魔杰座)', duration: 223 },
      { cid: 1004, page: 4, part: '04. 夜曲 - 周杰伦 (十一月的萧邦)', duration: 226 },
      { cid: 1005, page: 5, part: '05. 花海 - 周杰伦 (魔杰座)', duration: 264 },
    ]
  },
  {
    bvid: 'BV1ab411c7M2',
    aid: 170002,
    title: '【ACG精选】那些一听就让人热血沸腾的经典神级动漫燃曲',
    pic: 'https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=500&auto=format&fit=crop&q=60',
    owner_name: '动漫二次元音乐馆',
    owner_mid: 10002,
    duration: 1220,
    pages: [
      { cid: 2001, page: 1, part: '01. Only My Railgun - fripSide (超电磁炮OP)', duration: 257 },
      { cid: 2002, page: 2, part: '02. 红莲华 - LiSA (鬼灭之刃OP)', duration: 236 },
      { cid: 2003, page: 3, part: '03. 青鸟 - 生物股长 (火影忍者OP)', duration: 215 },
    ]
  },
  {
    bvid: 'BV1xx411c798',
    aid: 170003,
    title: '【4K60帧】告白气球 官方完整版MV 4K超清画质修复',
    pic: 'https://images.unsplash.com/photo-1470225620780-dba8ba36b745?w=500&auto=format&fit=crop&q=60',
    owner_name: '杰威尔音乐官方',
    owner_mid: 10003,
    duration: 215,
    pages: [
      { cid: 3001, page: 1, part: '告白气球 官方超清完整MV', duration: 215 }
    ]
  },
  {
    bvid: 'BV1fW411E7wE',
    aid: 170004,
    title: '【车载慢摇】适合雨夜独自开车听的温柔男声粤语经典',
    pic: 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=500&auto=format&fit=crop&q=60',
    owner_name: '深夜电台',
    owner_mid: 10004,
    duration: 1540,
    pages: [
      { cid: 4001, page: 1, part: '01. 富士山下 - 陈奕迅', duration: 260 },
      { cid: 4002, page: 2, part: '02. 喜帖街 - 谢安琪', duration: 210 },
      { cid: 4003, page: 3, part: '03. 钟无艳 - 谢安琪', duration: 245 },
    ]
  }
]
