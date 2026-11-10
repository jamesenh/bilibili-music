//! 用sqlite保存缓存索引与历史记录，通过共享连接串行访问本地数据库。

use anyhow::{anyhow, Result};
use rusqlite::{params, Connection};
use std::sync::{Arc, Mutex};

use crate::models::{CachedTrackItem, PlayHistoryItem};

/// 通过互斥锁共享sqlite连接的本地曲库存储。
#[derive(Clone)]
pub struct LibraryDb {
    conn: Arc<Mutex<Connection>>,
}

impl LibraryDb {
    /// 打开应用数据目录下的数据库，启用WAL并初始化表结构。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn new() -> Result<Self> {
        let config_dir = dirs::config_dir()
            .ok_or_else(|| anyhow!("无法获取用户配置目录"))?
            .join("BiliMusic");

        if !config_dir.exists() {
            std::fs::create_dir_all(&config_dir)?;
        }

        let db_path = config_dir.join("library.db");
        let conn = Connection::open(&db_path)?;

        // WAL 模式提升并发与性能
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;

        let db = Self {
            conn: Arc::new(Mutex::new(conn)),
        };

        db.init_tables()?;
        Ok(db)
    }

    /// 幂等创建缓存、播放历史与搜索历史表。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    fn init_tables(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();

        // 1. 已缓存曲目索引表
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cached_tracks (
                bvid TEXT NOT NULL,
                cid INTEGER NOT NULL,
                title TEXT NOT NULL,
                part TEXT NOT NULL,
                artist TEXT NOT NULL,
                cover TEXT NOT NULL,
                duration INTEGER NOT NULL,
                quality_id INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                cached_at INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                PRIMARY KEY (bvid, cid, quality_id)
            )",
            [],
        )?;

        // 2. 最近播放历史表
        conn.execute(
            "CREATE TABLE IF NOT EXISTS play_history (
                bvid TEXT NOT NULL,
                cid INTEGER NOT NULL,
                title TEXT NOT NULL,
                part TEXT NOT NULL,
                artist TEXT NOT NULL,
                cover TEXT NOT NULL,
                duration INTEGER NOT NULL,
                played_at INTEGER NOT NULL,
                PRIMARY KEY (bvid, cid)
            )",
            [],
        )?;

        // 3. 搜索历史表
        conn.execute(
            "CREATE TABLE IF NOT EXISTS search_history (
                keyword TEXT PRIMARY KEY NOT NULL,
                searched_at INTEGER NOT NULL
            )",
            [],
        )?;

        Ok(())
    }

    // --- 播放历史 ---
    /// 按视频与cid更新最近播放记录，并只保留最近200条。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn record_play_history(&self, item: &PlayHistoryItem) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO play_history (bvid, cid, title, part, artist, cover, duration, played_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
             ON CONFLICT(bvid, cid) DO UPDATE SET
                played_at = excluded.played_at,
                title = excluded.title,
                part = excluded.part",
            params![
                item.bvid,
                item.cid,
                item.title,
                item.part,
                item.artist,
                item.cover,
                item.duration,
                item.played_at
            ],
        )?;

        // 裁剪超出 200 条的历史记录
        conn.execute(
            "DELETE FROM play_history WHERE (bvid, cid) NOT IN (
                SELECT bvid, cid FROM play_history ORDER BY played_at DESC LIMIT 200
            )",
            [],
        )?;

        Ok(())
    }

    /// 按时间倒序读取最多200条播放记录。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn get_play_history(&self) -> Result<Vec<PlayHistoryItem>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT bvid, cid, title, part, artist, cover, duration, played_at
             FROM play_history ORDER BY played_at DESC LIMIT 200",
        )?;

        let rows = stmt.query_map([], |row| {
            Ok(PlayHistoryItem {
                bvid: row.get(0)?,
                cid: row.get(1)?,
                title: row.get(2)?,
                part: row.get(3)?,
                artist: row.get(4)?,
                cover: row.get(5)?,
                duration: row.get(6)?,
                played_at: row.get(7)?,
            })
        })?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r?);
        }
        Ok(list)
    }

    /// 仅删除播放历史表中的记录。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn clear_play_history(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute("DELETE FROM play_history", [])?;
        Ok(())
    }

    // --- 搜索历史 ---
    /// 更新关键词的搜索时间，并只保留最近50条。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn record_search(&self, keyword: &str) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        let now = chrono_now_ms();
        conn.execute(
            "INSERT INTO search_history (keyword, searched_at) VALUES (?1, ?2)
             ON CONFLICT(keyword) DO UPDATE SET searched_at = excluded.searched_at",
            params![keyword, now],
        )?;

        // 最多保留 50 条
        conn.execute(
            "DELETE FROM search_history WHERE keyword NOT IN (
                SELECT keyword FROM search_history ORDER BY searched_at DESC LIMIT 50
            )",
            [],
        )?;

        Ok(())
    }

    /// 按时间倒序读取最多50个搜索关键词。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn get_search_history(&self) -> Result<Vec<String>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt =
            conn.prepare("SELECT keyword FROM search_history ORDER BY searched_at DESC LIMIT 50")?;
        let rows = stmt.query_map([], |row| row.get(0))?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r?);
        }
        Ok(list)
    }

    /// 仅删除关键词历史表中的记录。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn clear_search_history(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute("DELETE FROM search_history", [])?;
        Ok(())
    }

    // --- 缓存索引 ---
    /// 按视频、cid和音质写入或更新缓存索引。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn record_cache_index(&self, item: &CachedTrackItem) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO cached_tracks (bvid, cid, title, part, artist, cover, duration, quality_id, size_bytes, cached_at, file_path)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
             ON CONFLICT(bvid, cid, quality_id) DO UPDATE SET
                cached_at = excluded.cached_at,
                size_bytes = excluded.size_bytes",
            params![
                item.bvid,
                item.cid,
                item.title,
                item.part,
                item.artist,
                item.cover,
                item.duration,
                item.quality_id,
                item.size_bytes,
                item.cached_at,
                item.file_path
            ],
        )?;
        Ok(())
    }

    /// 按缓存时间倒序读取缓存曲目及本地路径。
    ///
    /// # Errors
    /// 配置目录不可用、数据库连接失败或SQL执行、结果解码失败时返回错误。
    pub fn get_cached_tracks(&self) -> Result<Vec<CachedTrackItem>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT bvid, cid, title, part, artist, cover, duration, quality_id, size_bytes, cached_at, file_path
             FROM cached_tracks ORDER BY cached_at DESC",
        )?;

        let rows = stmt.query_map([], |row| {
            Ok(CachedTrackItem {
                bvid: row.get(0)?,
                cid: row.get(1)?,
                title: row.get(2)?,
                part: row.get(3)?,
                artist: row.get(4)?,
                cover: row.get(5)?,
                duration: row.get(6)?,
                quality_id: row.get(7)?,
                size_bytes: row.get(8)?,
                cached_at: row.get(9)?,
                file_path: row.get(10)?,
            })
        })?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r?);
        }
        Ok(list)
    }
}

/// 取得Unix毫秒时间戳，系统时钟早于纪元时返回零。
fn chrono_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
