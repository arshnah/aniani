use serde::Deserialize;
use serde_json::json;

#[derive(Clone, Debug, Default)]
pub struct Anime {
    pub title: String,
    pub episodes: Option<i64>,
    pub score: Option<i64>,
    pub status: Option<String>,
    pub genres: Vec<String>,
    pub description: String,
    pub cover: Option<String>,
}

#[derive(Clone, Debug)]
pub struct SearchResult {
    pub id: String,
    pub title: String,
}

#[derive(Clone, Debug)]
pub struct Episode {
    pub ep_ref: String,
    pub ep_no: String,
}

pub struct WatchLink {
    pub url: String,
    pub referer: Option<String>,
}

const ANILIST_URL: &str = "https://graphql.anilist.co";

const QUERY: &str = r#"
query ($sort: [MediaSort], $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(type: ANIME, sort: $sort) {
      title { romaji english }
      episodes
      averageScore
      status
      genres
      description(asHtml: false)
      coverImage { medium large extraLarge }
    }
  }
}
"#;

#[derive(Deserialize)]
struct GraphQlEnvelope {
    data: Option<PageData>,
}
#[derive(Deserialize)]
struct PageData {
    #[serde(rename = "Page")]
    page: MediaPage,
}
#[derive(Deserialize)]
struct MediaPage {
    media: Vec<MediaRaw>,
}
#[derive(Deserialize)]
struct MediaRaw {
    title: TitleRaw,
    episodes: Option<i64>,
    #[serde(rename = "averageScore")]
    average_score: Option<i64>,
    status: Option<String>,
    genres: Option<Vec<String>>,
    description: Option<String>,
    #[serde(rename = "coverImage")]
    cover_image: Option<CoverRaw>,
}
#[derive(Deserialize)]
struct TitleRaw {
    romaji: Option<String>,
    english: Option<String>,
}
#[derive(Deserialize)]
struct CoverRaw {
    large: Option<String>,
}

pub async fn anilist_fetch(client: &reqwest::Client, sort: &str, per_page: i32) -> anyhow::Result<Vec<Anime>> {
    let body = json!({
        "query": QUERY,
        "variables": {"sort": [sort], "perPage": per_page}
    });
    let resp: GraphQlEnvelope = client.post(ANILIST_URL).json(&body).send().await?.json().await?;
    let media = resp.data.map(|d| d.page.media).unwrap_or_default();
    Ok(media
        .into_iter()
        .filter_map(|m| {
            let title = m.title.english.or(m.title.romaji)?;
            Some(Anime {
                title,
                episodes: m.episodes,
                score: m.average_score,
                status: m.status,
                genres: m.genres.unwrap_or_default(),
                description: m.description.unwrap_or_default().split("<br>").next().unwrap_or("").to_string(),
                cover: m.cover_image.and_then(|c| c.large),
            })
        })
        .collect())
}

pub async fn trending(client: &reqwest::Client) -> anyhow::Result<Vec<Anime>> {
    anilist_fetch(client, "TRENDING_DESC", 25).await
}

pub async fn popular(client: &reqwest::Client) -> anyhow::Result<Vec<Anime>> {
    anilist_fetch(client, "POPULARITY_DESC", 25).await
}

const JIKAN_URL: &str = "https://api.jikan.moe/v4";

#[derive(Deserialize)]
struct JikanTopResponse {
    data: Vec<JikanEntry>,
}
#[derive(Deserialize)]
struct JikanEntry {
    title: Option<String>,
    title_english: Option<String>,
    episodes: Option<i64>,
    score: Option<f64>,
    status: Option<String>,
    genres: Option<Vec<JikanGenre>>,
    synopsis: Option<String>,
    images: Option<JikanImages>,
}
#[derive(Deserialize)]
struct JikanGenre {
    name: String,
}
#[derive(Deserialize)]
struct JikanImages {
    jpg: Option<JikanImageSet>,
}
#[derive(Deserialize)]
struct JikanImageSet {
    large_image_url: Option<String>,
}

fn jikan_to_anime(m: JikanEntry) -> Option<Anime> {
    let title = m.title_english.or(m.title)?;
    Some(Anime {
        title,
        episodes: m.episodes,
        score: m.score.map(|s| (s * 10.0) as i64),
        status: m.status,
        genres: m.genres.unwrap_or_default().into_iter().map(|g| g.name).collect(),
        description: m.synopsis.unwrap_or_default().lines().next().unwrap_or("").trim().to_string(),
        cover: m.images.and_then(|i| i.jpg).and_then(|j| j.large_image_url),
    })
}

async fn jikan_top(client: &reqwest::Client, page: i32) -> anyhow::Result<Vec<Anime>> {
    let resp: JikanTopResponse = client
        .get(format!("{JIKAN_URL}/top/anime"))
        .query(&[("page", page), ("limit", 25)])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    Ok(resp.data.into_iter().filter_map(jikan_to_anime).collect())
}

pub async fn discover_trending(client: &reqwest::Client) -> anyhow::Result<Vec<Anime>> {
    if let Ok(v) = jikan_top(client, 1).await {
        if !v.is_empty() {
            return Ok(v);
        }
    }
    trending(client).await
}

pub async fn discover_popular(client: &reqwest::Client) -> anyhow::Result<Vec<Anime>> {
    use rand::Rng;
    let page = rand::thread_rng().gen_range(2..=15);
    if let Ok(v) = jikan_top(client, page).await {
        if !v.is_empty() {
            return Ok(v);
        }
    }
    popular(client).await
}

pub async fn jikan_cover_for_title(client: &reqwest::Client, title: &str) -> Option<String> {
    #[derive(Deserialize)]
    struct SearchResp {
        data: Vec<JikanEntry>,
    }
    let resp = match client.get(format!("{JIKAN_URL}/anime")).query(&[("q", title), ("limit", "1")]).send().await {
        Ok(r) => r,
        Err(e) => {
            crate::platform::debug_log(&format!("jikan_cover_for_title({title}): request failed: {e}"));
            return None;
        }
    };
    let body: SearchResp = match resp.json().await {
        Ok(b) => b,
        Err(e) => {
            crate::platform::debug_log(&format!("jikan_cover_for_title({title}): body parse failed: {e}"));
            return None;
        }
    };
    body.data.into_iter().next()?.images?.jpg?.large_image_url
}

pub async fn cover_for_title(client: &reqwest::Client, title: &str) -> Option<String> {
    if let Some(c) = jikan_cover_for_title(client, title).await {
        return Some(c);
    }
    let resp = match client
        .post(ANILIST_URL)
        .json(&json!({"query": COVER_QUERY, "variables": {"search": title}}))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => {
            crate::platform::debug_log(&format!("cover_for_title({title}): anilist request failed: {e}"));
            return None;
        }
    };
    let body: serde_json::Value = match resp.json().await {
        Ok(b) => b,
        Err(e) => {
            crate::platform::debug_log(&format!("cover_for_title({title}): anilist body parse failed: {e}"));
            return None;
        }
    };
    body.get("data")?.get("Media")?.get("coverImage")?.get("large")?.as_str().map(|s| s.to_string())
}

const COVER_QUERY: &str = r#"
query ($search: String) {
  Media(search: $search, type: ANIME) {
    coverImage { large }
  }
}
"#;

const DISCORD_MEDIA_QUERY: &str = r#"
query ($search: String) {
  Media(search: $search, type: ANIME) {
    coverImage { extraLarge large }
    siteUrl
  }
}
"#;

pub async fn discord_media_for(client: &reqwest::Client, title: &str) -> crate::discord::MediaInfo {
    let empty = crate::discord::MediaInfo { image: None, url: None };
    let Ok(resp) = client
        .post(ANILIST_URL)
        .json(&json!({"query": DISCORD_MEDIA_QUERY, "variables": {"search": title}}))
        .send()
        .await
    else {
        return empty;
    };
    let Ok(body) = resp.json::<serde_json::Value>().await else { return empty };
    let Some(media) = body.get("data").and_then(|d| d.get("Media")) else { return empty };
    let image = media
        .get("coverImage")
        .and_then(|c| c.get("extraLarge").or_else(|| c.get("large")))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let url = media.get("siteUrl").and_then(|v| v.as_str()).map(|s| s.to_string());
    crate::discord::MediaInfo { image, url }
}

const ANIDB_BASE: &str = "https://anidb.app";
const CHROME_UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

fn curl_impersonate(url: &str) -> anyhow::Result<String> {
    let bin = crate::platform::find_curl_impersonate()
        .ok_or_else(|| anyhow::anyhow!("curl_chrome136 (curl-impersonate) not found on PATH, required for anidb.app"))?;
    let out = std::process::Command::new(bin)
        .args(["-sL", "-A", CHROME_UA, "--max-time", "12", url])
        .output()?;
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

fn unescape(s: &str) -> String {
    s.replace("&#039;", "'").replace("&quot;", "\"").replace("\\/", "/")
}

pub fn anidb_search(query: &str) -> anyhow::Result<Vec<SearchResult>> {
    let q = query.trim().replace(' ', "+");
    if q.is_empty() {
        return Ok(vec![]);
    }
    let page = curl_impersonate(&format!("{ANIDB_BASE}/browse?q={q}"))?.replace('\n', " ");
    let re = regex::Regex::new(r#"anime/([a-z0-9-]+-[0-9]+)".*?alt="([^"]+)""#)?;
    let mut results = vec![];
    for cap in re.captures_iter(&page) {
        results.push(SearchResult {
            id: cap[1].to_string(),
            title: unescape(&cap[2]),
        });
    }
    Ok(results)
}

pub fn anidb_episodes(anime_id: &str) -> anyhow::Result<Vec<Episode>> {
    let numeric_id = anime_id.rsplit('-').next().unwrap_or(anime_id);
    let page = curl_impersonate(&format!("{ANIDB_BASE}/api/frontend/anime/{numeric_id}/episodes"))?;
    let re = regex::Regex::new(r#""id":(\d+).*?"number":(\d+)"#)?;
    Ok(re
        .captures_iter(&page)
        .map(|c| Episode { ep_ref: c[1].to_string(), ep_no: c[2].to_string() })
        .collect())
}

pub fn anidb_watch(ep_ref: &str, dub: bool) -> anyhow::Result<Option<WatchLink>> {
    let lang = if dub { "eng" } else { "jpn" };
    let page = curl_impersonate(&format!("{ANIDB_BASE}/api/frontend/episode/{ep_ref}/languages"))?;
    let embed_re = regex::Regex::new(r#""embed_url":"([^"]+)""#)?;
    let mut embed = None;
    for line in page.split("},{") {
        if line.contains(lang) {
            if let Some(cap) = embed_re.captures(line) {
                embed = Some(unescape(&cap[1]));
                break;
            }
        }
    }
    let Some(embed_url) = embed else { return Ok(None) };

    let embed_page = curl_impersonate(&embed_url)?;
    let file_re = regex::Regex::new(r"file: '([^']*)'")?;
    let Some(master_url) = file_re.captures(&embed_page).map(|c| c[1].to_string()) else {
        return Ok(None);
    };

    let master = curl_impersonate(&master_url)?;
    let mut best: Option<(i64, String)> = None;
    let mut pending_quality: Option<i64> = None;
    let quality_re = regex::Regex::new(r"x(\d+)")?;
    for line in master.replace('\r', "").lines() {
        if line.starts_with("#EXT-X-STREAM") {
            if line.contains("EXT-X-I-FRAME") {
                pending_quality = None;
                continue;
            }
            pending_quality = quality_re.captures(line).and_then(|c| c[1].parse().ok()).or(Some(0));
        } else if !line.is_empty() && !line.starts_with('#') {
            if let Some(q) = pending_quality.take() {
                if best.as_ref().map(|(bq, _)| q > *bq).unwrap_or(true) {
                    best = Some((q, line.trim().to_string()));
                }
            }
        }
    }
    Ok(best.map(|(_, url)| WatchLink { url, referer: None }))
}
