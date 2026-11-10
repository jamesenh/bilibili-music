//! 登录会话(账号凭据)的管理与落盘。
//!
//! 与 Python 版的 `core/session.py` 保持一致：
//! * 落盘位置在 `%APPDATA%\BiliMusic\session.json` (或 Linux 对应的 config 目录)；
//! * 写入时严格采用原子写入（先写 `.part` 临时文件再 `rename` 覆盖）；
//! * 退出登录时直接物理删除 `session.json`；
//! * 遵守凭据纪律：绝对禁止在日志、异常或控制台中输出 SESSDATA 等敏感字段。

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

/// 与Python版本共用的本地会话文件名，不得纳入版本控制。
const SESSION_FILE_NAME: &str = "session.json";

/// 会话数据结构，与 Python 版 `core.session.Session` 完全兼容
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    #[serde(default)]
    pub cookies: HashMap<String, String>,
    #[serde(default)]
    pub uname: String,
    #[serde(default)]
    pub mid: i64,
    #[serde(default)]
    pub saved_at: f64,
}

/// 会话存储管理器
pub struct SessionStore {
    file_path: PathBuf,
}

impl SessionStore {
    /// 初始化会话存储，定位到 `%APPDATA%\BiliMusic\session.json`
    ///
    /// # Errors
    /// 配置目录不可用、会话JSON序列化失败或会话文件操作失败时返回错误。
    pub fn new() -> Result<Self> {
        let base_dir = dirs::config_dir()
            .or_else(|| dirs::data_local_dir())
            .ok_or_else(|| anyhow!("无法定位系统配置目录"))?;

        let dir = base_dir.join("BiliMusic");
        if !dir.exists() {
            let _ = std::fs::create_dir_all(&dir);
        }

        Ok(Self {
            file_path: dir.join(SESSION_FILE_NAME),
        })
    }

    /// 获取会话文件所在完整路径（供界面明示使用）
    pub fn file_path(&self) -> &PathBuf {
        &self.file_path
    }

    /// 尝试加载既有凭据；文件缺失、不可读、JSON无效或缺少登录字段时返回None。
    pub fn load(&self) -> Option<Session> {
        if !self.file_path.exists() {
            return None;
        }

        let content = std::fs::read_to_string(&self.file_path).ok()?;
        let session: Session = serde_json::from_str(&content).ok()?;
        if session.cookies.contains_key("SESSDATA") {
            Some(session)
        } else {
            None
        }
    }

    /// 原子写入会话凭据到磁盘
    ///
    /// # Errors
    /// 配置目录不可用、会话JSON序列化失败或会话文件操作失败时返回错误。
    pub fn save(&self, session: &Session) -> Result<()> {
        let part_path = self.file_path.with_extension("json.part");
        let json_text = serde_json::to_string_pretty(session)?;

        // 写入临时文件并刷盘
        std::fs::write(&part_path, json_text)?;

        // 原子重命名替换
        std::fs::rename(&part_path, &self.file_path)?;
        Ok(())
    }

    /// 清除会话（退出登录时直接物理删除文件）
    ///
    /// # Errors
    /// 配置目录不可用、会话JSON序列化失败或会话文件操作失败时返回错误。
    pub fn clear(&self) -> Result<()> {
        if self.file_path.exists() {
            std::fs::remove_file(&self.file_path)?;
        }
        Ok(())
    }
}

/// 将原始 Cookie 文本（形如 `SESSDATA=xxx; bili_jct=yyy`）解析为键值映射字典
pub fn parse_cookie_header(raw: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for part in raw.replace('\n', ";").split(';') {
        let trimmed = part.trim();
        if trimmed.is_empty() || !trimmed.contains('=') {
            continue;
        }
        if let Some((name, value)) = trimmed.split_once('=') {
            let n = name.trim().to_string();
            let v = value.trim().to_string();
            if !n.is_empty() && !v.is_empty() {
                map.insert(n, v);
            }
        }
    }
    map
}

/// 将 Cookie 键值映射字典格式化为可直接放在 HTTP 请求头中的字符串
pub fn format_cookie_header(cookies: &HashMap<String, String>) -> String {
    let mut parts = Vec::with_capacity(cookies.len());
    for (k, v) in cookies {
        parts.push(format!("{}={}", k, v));
    }
    parts.join("; ")
}
