//! 定义前后端传输模型与纯标题处理逻辑，不依赖Tauri或界面运行时。

use serde::{Deserialize, Serialize};

/// 表示一首分P曲目，cid与时长均属于该分P。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Page {
    pub cid: i64,
    pub page: i32,
    pub part: String,
    pub duration: i64,
}

/// 表示视频元数据及详情接口补全的分P列表。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Video {
    pub bvid: String,
    pub aid: i64,
    pub title: String,
    pub pic: String,
    pub owner_name: String,
    pub owner_mid: i64,
    pub duration: i64,
    pub pages: Vec<Page>,
}

/// 表示playurl返回的一档音频流与编码信息。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioTrack {
    pub id: i32, // quality id: 30280, 30232, 30216 等
    pub bandwidth: i64,
    pub codecs: String,
    pub base_url: String,
}

/// 保存指定视频与cid最近一次播放的展示信息。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlayHistoryItem {
    pub bvid: String,
    pub cid: i64,
    pub title: String,
    pub part: String,
    pub artist: String,
    pub cover: String,
    pub duration: i64,
    pub played_at: i64,
}

/// 保存指定分P与音质的缓存索引及本地路径。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CachedTrackItem {
    pub bvid: String,
    pub cid: i64,
    pub title: String,
    pub part: String,
    pub artist: String,
    pub cover: String,
    pub duration: i64,
    pub quality_id: i32,
    pub size_bytes: i64,
    pub cached_at: i64,
    pub file_path: String,
}

/// 表示只读收藏夹的标识、名称与条目数。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FavFolder {
    pub id: i64,
    pub title: String,
    pub media_count: i32,
}

/// 表示账号登录状态及可展示的公开资料，不携带凭据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserInfo {
    pub is_login: bool,
    pub uname: String,
    pub mid: i64,
    pub avatar: String,
}

// 清除 B 站标题中的 HTML 高亮标签 <em class="keyword">...</em>
/// 移除标题中的HTML标签并还原常见实体，避免搜索高亮混入曲名。
pub fn clean_html_tags(input: &str) -> String {
    let mut result = String::with_capacity(input.len());
    let mut in_tag = false;

    for ch in input.chars() {
        if ch == '<' {
            in_tag = true;
        } else if ch == '>' {
            in_tag = false;
        } else if !in_tag {
            result.push(ch);
        }
    }

    result
        .replace("&quot;", "\"")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
}

#[cfg(test)]
mod tests {
    //! 验证纯标题处理逻辑，不创建桌面应用或请求网络。

    use super::clean_html_tags;

    /// 搜索关键词的高亮标签必须去掉，曲名中的中文保留。
    #[test]
    fn removes_search_highlight_tags() {
        assert_eq!(
            clean_html_tags("<em class=\"keyword\">晴天</em>现场"),
            "晴天现场"
        );
    }

    /// 常见HTML实体应还原为可读字符。
    #[test]
    fn decodes_title_entities() {
        assert_eq!(
            clean_html_tags("&quot;歌&amp;曲&quot; &lt;现场&gt; &#39;"),
            "\"歌&曲\" <现场> '"
        );
    }

    /// 普通标题和空字符串不应被增删内容。
    #[test]
    fn preserves_plain_and_empty_titles() {
        assert_eq!(clean_html_tags("普通曲名"), "普通曲名");
        assert_eq!(clean_html_tags(""), "");
    }
}
