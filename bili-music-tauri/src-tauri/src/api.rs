//! 封装B站接口并组装领域模型，HTTP会话与限速统一交给BiliClient。

use anyhow::{anyhow, Result};
use serde_json::Value;

use crate::client::BiliClient;
use crate::models::{clean_html_tags, AudioTrack, FavFolder, Page, Video};

/// 复用HTTP客户端的B站只读接口入口。
pub struct BiliApi {
    client: BiliClient,
}

impl BiliApi {
    /// 注入共享客户端，使各接口使用同一会话与限速状态。
    pub fn new(client: BiliClient) -> Self {
        Self { client }
    }

    /// 搜索视频
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn search(&self, keyword: &str, page: i32) -> Result<Vec<Video>> {
        let encoded_kw = urlencoding::encode(keyword);
        let url = format!(
            "https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword={}&page={}&search_type=video",
            encoded_kw, page
        );

        let root: Value = self.client.get_json(&url).await?;
        let code = root.get("code").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(anyhow!("B站搜索接口返回错误码: {}", code));
        }

        let mut list = Vec::new();
        if let Some(results) = root
            .get("data")
            .and_then(|d| d.get("result"))
            .and_then(|r| r.as_array())
        {
            for item in results {
                let bvid = item.get("bvid").and_then(|v| v.as_str()).unwrap_or("");
                let aid = item.get("aid").and_then(|v| v.as_i64()).unwrap_or(0);
                let raw_title = item.get("title").and_then(|v| v.as_str()).unwrap_or("");
                let mut pic = item
                    .get("pic")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if pic.starts_with("//") {
                    pic = format!("https:{}", pic);
                }

                let owner_name = item.get("author").and_then(|v| v.as_str()).unwrap_or("");
                let owner_mid = item.get("mid").and_then(|v| v.as_i64()).unwrap_or(0);

                // 时长处理：搜索结果中常常是 "mm:ss" 字符串
                let duration = if let Some(dur_str) = item.get("duration").and_then(|v| v.as_str())
                {
                    parse_duration_str(dur_str)
                } else {
                    item.get("duration").and_then(|v| v.as_i64()).unwrap_or(0)
                };

                if !bvid.is_empty() {
                    list.push(Video {
                        bvid: bvid.to_string(),
                        aid,
                        title: clean_html_tags(raw_title),
                        pic,
                        owner_name: owner_name.to_string(),
                        owner_mid,
                        duration,
                        pages: Vec::new(), // 搜索接口不含分P列表，点播时补齐
                    });
                }
            }
        }

        Ok(list)
    }

    /// 获取视频详情及完整分P列表 (/x/web-interface/view)
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn get_video_detail(&self, bvid: &str) -> Result<Video> {
        let url = format!(
            "https://api.bilibili.com/x/web-interface/view?bvid={}",
            bvid
        );
        let root: Value = self.client.get_json(&url).await?;
        let code = root.get("code").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(anyhow!("获取视频详情失败, code: {}", code));
        }

        let data = root.get("data").ok_or_else(|| anyhow!("缺少 data 字段"))?;
        let aid = data.get("aid").and_then(|v| v.as_i64()).unwrap_or(0);
        let raw_title = data.get("title").and_then(|v| v.as_str()).unwrap_or("");
        let mut pic = data
            .get("pic")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if pic.starts_with("//") {
            pic = format!("https:{}", pic);
        }

        let owner = data.get("owner");
        let owner_name = owner
            .and_then(|o| o.get("name"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let owner_mid = owner
            .and_then(|o| o.get("mid"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let duration = data.get("duration").and_then(|v| v.as_i64()).unwrap_or(0);

        let mut pages = Vec::new();
        if let Some(pages_arr) = data.get("pages").and_then(|p| p.as_array()) {
            for (idx, p) in pages_arr.iter().enumerate() {
                let cid = p.get("cid").and_then(|v| v.as_i64()).unwrap_or(0);
                let page_no = p
                    .get("page")
                    .and_then(|v| v.as_i64())
                    .unwrap_or((idx + 1) as i64) as i32;
                let part = p.get("part").and_then(|v| v.as_str()).unwrap_or("");
                let dur = p.get("duration").and_then(|v| v.as_i64()).unwrap_or(0);

                pages.push(Page {
                    cid,
                    page: page_no,
                    part: if part.is_empty() {
                        format!("第 {} P", page_no)
                    } else {
                        part.to_string()
                    },
                    duration: dur,
                });
            }
        }

        Ok(Video {
            bvid: bvid.to_string(),
            aid,
            title: clean_html_tags(raw_title),
            pic,
            owner_name: owner_name.to_string(),
            owner_mid,
            duration,
            pages,
        })
    }

    /// 获取音频 DASH 格式音轨列表 (/x/player/wbi/playurl)
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn get_audio_tracks(&self, bvid: &str, cid: i64) -> Result<Vec<AudioTrack>> {
        let url = format!(
            "https://api.bilibili.com/x/player/wbi/playurl?bvid={}&cid={}&fnval=16",
            bvid, cid
        );

        let root: Value = self.client.get_json(&url).await?;
        let code = root.get("code").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(anyhow!("获取音频 playurl 失败, code: {}", code));
        }

        let mut tracks = Vec::new();
        if let Some(audio_arr) = root
            .get("data")
            .and_then(|d| d.get("dash"))
            .and_then(|dash| dash.get("audio"))
            .and_then(|a| a.as_array())
        {
            for a in audio_arr {
                let id = a.get("id").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let bandwidth = a.get("bandwidth").and_then(|v| v.as_i64()).unwrap_or(0);
                let codecs = a.get("codecs").and_then(|v| v.as_str()).unwrap_or("");
                let base_url = a
                    .get("baseUrl")
                    .or_else(|| a.get("base_url"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                if !base_url.is_empty() {
                    tracks.push(AudioTrack {
                        id,
                        bandwidth,
                        codecs: codecs.to_string(),
                        base_url: base_url.to_string(),
                    });
                }
            }
        }

        // 按音质码率从高到低排序 (30280 -> 30232 -> 30216)
        tracks.sort_by(|a, b| b.id.cmp(&a.id));

        Ok(tracks)
    }

    /// 获取当前登录用户信息 (/x/web-interface/nav)
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn get_user_info(&self) -> Result<crate::models::UserInfo> {
        let url = "https://api.bilibili.com/x/web-interface/nav";
        let root: Value = self.client.get_json(url).await?;
        let data = root.get("data");
        let is_login = data
            .and_then(|d| d.get("isLogin"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let uname = data
            .and_then(|d| d.get("uname"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mid = data
            .and_then(|d| d.get("mid"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut avatar = data
            .and_then(|d| d.get("face"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if avatar.starts_with("//") {
            avatar = format!("https:{}", avatar);
        }

        Ok(crate::models::UserInfo {
            is_login,
            uname,
            mid,
            avatar,
        })
    }

    /// 获取收藏夹列表
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn get_fav_folders(&self, mid: i64) -> Result<Vec<FavFolder>> {
        let url = format!(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={}",
            mid
        );
        let root: Value = self.client.get_json(&url).await?;
        let mut list = Vec::new();
        if let Some(arr) = root
            .get("data")
            .and_then(|d| d.get("list"))
            .and_then(|l| l.as_array())
        {
            for f in arr {
                let id = f.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let title = f.get("title").and_then(|v| v.as_str()).unwrap_or("");
                let media_count = f.get("media_count").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                list.push(FavFolder {
                    id,
                    title: title.to_string(),
                    media_count,
                });
            }
        }
        Ok(list)
    }

    /// 获取指定收藏夹内的视频列表 (/x/v3/fav/resource/list)
    ///
    /// # Errors
    /// HTTP请求失败、响应JSON无法解析或接口返回不可用状态时返回错误。
    pub async fn get_fav_resources(&self, media_id: i64, page: i32) -> Result<Vec<Video>> {
        let url = format!(
            "https://api.bilibili.com/x/v3/fav/resource/list?media_id={}&pn={}&ps=20",
            media_id, page
        );
        let root: Value = self.client.get_json(&url).await?;
        let code = root.get("code").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(anyhow!("获取收藏夹内容失败, code: {}", code));
        }

        let mut list = Vec::new();
        if let Some(medias) = root
            .get("data")
            .and_then(|d| d.get("medias"))
            .and_then(|m| m.as_array())
        {
            for item in medias {
                let bvid = item.get("bvid").and_then(|v| v.as_str()).unwrap_or("");
                let aid = item.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let raw_title = item.get("title").and_then(|v| v.as_str()).unwrap_or("");
                let mut pic = item
                    .get("cover")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if pic.starts_with("//") {
                    pic = format!("https:{}", pic);
                }

                let upper = item.get("upper");
                let owner_name = upper
                    .and_then(|u| u.get("name"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let owner_mid = upper
                    .and_then(|u| u.get("mid"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);

                let duration = item.get("duration").and_then(|v| v.as_i64()).unwrap_or(0);

                if !bvid.is_empty() {
                    list.push(Video {
                        bvid: bvid.to_string(),
                        aid,
                        title: clean_html_tags(raw_title),
                        pic,
                        owner_name: owner_name.to_string(),
                        owner_mid,
                        duration,
                        pages: Vec::new(),
                    });
                }
            }
        }

        Ok(list)
    }
}

/// 把搜索时长转换为秒，支持分秒与时分秒，无法解析的段按零处理。
fn parse_duration_str(s: &str) -> i64 {
    let parts: Vec<&str> = s.split(':').collect();
    if parts.len() == 2 {
        let m: i64 = parts[0].parse().unwrap_or(0);
        let sec: i64 = parts[1].parse().unwrap_or(0);
        m * 60 + sec
    } else if parts.len() == 3 {
        let h: i64 = parts[0].parse().unwrap_or(0);
        let m: i64 = parts[1].parse().unwrap_or(0);
        let sec: i64 = parts[2].parse().unwrap_or(0);
        h * 3600 + m * 60 + sec
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    //! 验证搜索结果时长转换，不触发B站请求。

    use super::parse_duration_str;

    /// 分秒格式必须转换成总秒数。
    #[test]
    fn parses_minutes_and_seconds() {
        assert_eq!(parse_duration_str("04:32"), 272);
    }

    /// 长合集的时分秒格式必须保留小时部分。
    #[test]
    fn parses_hours_minutes_and_seconds() {
        assert_eq!(parse_duration_str("1:02:03"), 3723);
    }

    /// 不支持的结构按零时长处理。
    #[test]
    fn unsupported_shapes_return_zero() {
        assert_eq!(parse_duration_str(""), 0);
        assert_eq!(parse_duration_str("123"), 0);
        assert_eq!(parse_duration_str("1:2:3:4"), 0);
    }

    /// 无法解析的单段按零处理，保留其他有效段。
    #[test]
    fn invalid_segment_keeps_valid_segments() {
        assert_eq!(parse_duration_str("invalid:15"), 15);
    }
}
