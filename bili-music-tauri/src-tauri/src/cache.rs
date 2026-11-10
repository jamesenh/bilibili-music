//! 管理本地音频缓存，以包含cid的路径隔离分P并先写临时文件再重命名。

use anyhow::{anyhow, Result};
use futures_util::StreamExt;
use reqwest::header::{HeaderMap, HeaderValue, REFERER, USER_AGENT};
use std::path::PathBuf;
use tokio::fs::{self, File};
use tokio::io::AsyncWriteExt;

/// 模拟桌面浏览器的请求标识，用于B站接口与CDN请求。
const DEFAULT_USER_AGENT: &str =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

/// 保存缓存根目录并执行音频文件的本地查找与下载。
pub struct AudioCache {
    cache_dir: PathBuf,
}

impl AudioCache {
    /// 定位并创建系统缓存目录，目录不可用时返回错误。
    ///
    /// # Errors
    /// 系统缓存目录不可用、HTTP下载失败或文件创建、写入、重命名失败时返回错误。
    pub fn new() -> Result<Self> {
        let base_dir = dirs::cache_dir()
            .or_else(|| dirs::data_local_dir())
            .ok_or_else(|| anyhow!("无法获取系统本地缓存目录"))?;

        let cache_dir = base_dir.join("BiliMusic").join("Cache");
        if !cache_dir.exists() {
            std::fs::create_dir_all(&cache_dir)?;
        }

        Ok(Self { cache_dir })
    }

    /// 按视频、分P和音质构造缓存路径，避免不同cid串歌。
    pub fn get_cache_path(&self, bvid: &str, cid: i64, quality_id: i32) -> PathBuf {
        let file_name = format!("{}_{}_{}.m4a", bvid, cid, quality_id);
        self.cache_dir.join(file_name)
    }

    /// 检查指定分P与音质是否已在本地完整缓存
    pub fn is_cached(&self, bvid: &str, cid: i64, quality_id: i32) -> bool {
        let path = self.get_cache_path(bvid, cid, quality_id);
        path.exists() && path.metadata().map(|m| m.len() > 1024).unwrap_or(false)
    }

    /// 下载音频流并原子写入本地缓存文件
    ///
    /// # Errors
    /// 系统缓存目录不可用、HTTP下载失败或文件创建、写入、重命名失败时返回错误。
    pub async fn download_and_cache(
        &self,
        url: &str,
        bvid: &str,
        cid: i64,
        quality_id: i32,
    ) -> Result<PathBuf> {
        let final_path = self.get_cache_path(bvid, cid, quality_id);
        if final_path.exists() {
            return Ok(final_path);
        }

        let part_path = final_path.with_extension("m4a.part");

        // 构造专门针对 B 站音频 CDN 的请求头 (摘掉 Origin, 保留 Referer)
        let mut headers = HeaderMap::new();
        headers.insert(USER_AGENT, HeaderValue::from_static(DEFAULT_USER_AGENT));
        headers.insert(
            REFERER,
            HeaderValue::from_static("https://www.bilibili.com/"),
        );

        let client = reqwest::Client::builder()
            .default_headers(headers)
            .build()?;

        let resp = client.get(url).send().await?.error_for_status()?;

        // 打开 .part 文件原子写入
        let mut file = File::create(&part_path).await?;
        let mut stream = resp.bytes_stream();

        while let Some(chunk_res) = stream.next().await {
            match chunk_res {
                Ok(chunk) => {
                    file.write_all(&chunk).await?;
                }
                Err(e) => {
                    // 出错时清理半截 .part 文件
                    drop(file);
                    let _ = fs::remove_file(&part_path).await;
                    return Err(anyhow!("流式下载中断: {}", e));
                }
            }
        }

        file.flush().await?;
        drop(file);

        // 原子重命名为目标文件
        fs::rename(&part_path, &final_path).await?;

        Ok(final_path)
    }

    /// 返回缓存根目录，供上层展示或读取目录信息。
    pub fn get_cache_dir(&self) -> &PathBuf {
        &self.cache_dir
    }
}
