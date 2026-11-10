//! 管理HTTP会话、请求头和异步限速，客户端克隆共享请求间隔与登录态。

use anyhow::Result;
use reqwest::header::{HeaderMap, HeaderValue, REFERER, USER_AGENT};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

use crate::session::{format_cookie_header, parse_cookie_header, Session, SessionStore};

/// 模拟桌面浏览器的请求标识，用于B站接口与CDN请求。
const DEFAULT_USER_AGENT: &str =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
/// 延续防风控约束的800毫秒请求间隔，不为提速而调小。
const MIN_REQUEST_INTERVAL: Duration = Duration::from_millis(800);

/// 共享会话、Cookie与请求时间的异步HTTP客户端。
#[derive(Clone)]
pub struct BiliClient {
    client: reqwest::Client,
    last_request_time: Arc<Mutex<Option<Instant>>>,
    cookies: Arc<Mutex<Option<String>>>,
    session_store: Arc<SessionStore>,
}

impl BiliClient {
    /// 创建带Referer和gzip支持的客户端，并尝试读取本地登录态。
    ///
    /// # Errors
    /// HTTP客户端创建、请求发送、响应解析或会话存储失败时返回错误。
    pub fn new() -> Result<Self> {
        let mut default_headers = HeaderMap::new();
        default_headers.insert(USER_AGENT, HeaderValue::from_static(DEFAULT_USER_AGENT));
        default_headers.insert(
            REFERER,
            HeaderValue::from_static("https://www.bilibili.com/"),
        );

        let client = reqwest::Client::builder()
            .default_headers(default_headers)
            .cookie_store(true)
            .gzip(true)
            .timeout(Duration::from_secs(15))
            .build()?;

        let session_store = SessionStore::new()?;
        let initial_cookies = session_store
            .load()
            .map(|s| format_cookie_header(&s.cookies));

        Ok(Self {
            client,
            last_request_time: Arc::new(Mutex::new(None)),
            cookies: Arc::new(Mutex::new(initial_cookies)),
            session_store: Arc::new(session_store),
        })
    }

    /// 限速器：严格保证请求间隔不低于 800ms，防风控 412/429
    pub async fn rate_limit(&self) {
        let mut last_time = self.last_request_time.lock().await;
        if let Some(t) = *last_time {
            let elapsed = t.elapsed();
            if elapsed < MIN_REQUEST_INTERVAL {
                tokio::time::sleep(MIN_REQUEST_INTERVAL - elapsed).await;
            }
        }
        *last_time = Some(Instant::now());
    }

    /// 主页预热：下发 buvid3 与 session 初始化
    ///
    /// # Errors
    /// HTTP客户端创建、请求发送、响应解析或会话存储失败时返回错误。
    pub async fn warm_up(&self) -> Result<()> {
        self.rate_limit().await;
        let _ = self.client.get("https://www.bilibili.com/").send().await?;
        Ok(())
    }

    /// 保存凭据：同时更新内存与磁盘 session.json
    ///
    /// # Errors
    /// HTTP客户端创建、请求发送、响应解析或会话存储失败时返回错误。
    pub async fn set_cookie(&self, cookie_str: String, uname: String, mid: i64) -> Result<()> {
        let cookie_map = parse_cookie_header(&cookie_str);
        let header_str = format_cookie_header(&cookie_map);

        let session = Session {
            cookies: cookie_map,
            uname,
            mid,
            saved_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0),
        };
        self.session_store.save(&session)?;

        let mut c = self.cookies.lock().await;
        *c = Some(header_str);
        Ok(())
    }

    /// 清理凭据：同时清理内存与磁盘 session.json
    ///
    /// # Errors
    /// HTTP客户端创建、请求发送、响应解析或会话存储失败时返回错误。
    pub async fn clear_cookie(&self) -> Result<()> {
        let _ = self.session_store.clear();
        let mut c = self.cookies.lock().await;
        *c = None;
        Ok(())
    }

    /// 获取会话文件存储路径
    pub fn session_file_path(&self) -> String {
        self.session_store.file_path().to_string_lossy().to_string()
    }

    /// 当前内存中是否挂载了凭据
    pub async fn has_cookie(&self) -> bool {
        let c = self.cookies.lock().await;
        c.is_some()
    }

    /// 发起带限速和防盗链的 GET 请求
    ///
    /// # Errors
    /// HTTP客户端创建、请求发送、响应解析或会话存储失败时返回错误。
    pub async fn get_json<T: serde::de::DeserializeOwned>(&self, url: &str) -> Result<T> {
        self.rate_limit().await;

        let mut req = self.client.get(url);
        {
            let c = self.cookies.lock().await;
            if let Some(ref cookie) = *c {
                req = req.header("Cookie", cookie);
            }
        }

        let resp = req.send().await?.error_for_status()?;
        let data = resp.json::<T>().await?;
        Ok(data)
    }

    /// 借用底层客户端；调用方需自行遵守共享限速约束。
    pub fn raw_client(&self) -> &reqwest::Client {
        &self.client
    }
}
