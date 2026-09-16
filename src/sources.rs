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
query ($sort: [MediaSort], $perPage: Int, $genre: String) {
  Page(page: 1, perPage: $perPage) {
    media(type: ANIME, sort: $sort, genre: $genre) {
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

pub async fn anilist_fetch(client: &reqwest::Client, sort: &str, per_page: i32, genre: Option<&str>) -> anyhow::Result<Vec<Anime>> {
    let body = json!({
        "query": QUERY,
        "variables": {"sort": [sort], "perPage": per_page, "genre": genre}
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
    anilist_fetch(client, "TRENDING_DESC", 25, None).await
}

pub async fn popular(client: &reqwest::Client) -> anyhow::Result<Vec<Anime>> {
    anilist_fetch(client, "POPULARITY_DESC", 25, None).await
}

pub async fn discover_filtered(client: &reqwest::Client, sort: &str, genre: Option<&str>) -> anyhow::Result<Vec<Anime>> {
    anilist_fetch(client, sort, 25, genre).await
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
    if !resp.status().is_success() {
        crate::platform::debug_log(&format!("jikan_cover_for_title({title}): jikan returned {} (likely rate-limited or MAL is unreachable)", resp.status()));
        return None;
    }
    let body: SearchResp = match resp.json().await {
        Ok(b) => b,
        Err(e) => {
            crate::platform::debug_log(&format!("jikan_cover_for_title({title}): body parse failed: {e}"));
            return None;
        }
    };
    body.data.into_iter().next()?.images?.jpg?.large_image_url
}

async fn cover_for_title_uncached(client: &reqwest::Client, title: &str) -> Option<String> {
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
    if !resp.status().is_success() {
        crate::platform::debug_log(&format!("cover_for_title({title}): anilist returned {}", resp.status()));
        return None;
    }
    let body: serde_json::Value = match resp.json().await {
        Ok(b) => b,
        Err(e) => {
            crate::platform::debug_log(&format!("cover_for_title({title}): anilist body parse failed: {e}"));
            return None;
        }
    };
    body.get("data")?.get("Media")?.get("coverImage")?.get("large")?.as_str().map(|s| s.to_string())
}

fn cover_cache_path() -> std::path::PathBuf {
    crate::platform::state_dir("aniani").join("cover_cache.json")
}

fn load_cover_cache() -> std::collections::HashMap<String, Option<String>> {
    std::fs::read_to_string(cover_cache_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_cover_cache(cache: &std::collections::HashMap<String, Option<String>>) {
    let _ = std::fs::create_dir_all(crate::platform::state_dir("aniani"));
    if let Ok(s) = serde_json::to_string(cache) {
        let _ = std::fs::write(cover_cache_path(), s);
    }
}

pub async fn cover_for_title(client: &reqwest::Client, title: &str) -> Option<String> {
    let cache = load_cover_cache();
    if let Some(cached) = cache.get(title) {
        return cached.clone();
    }
    let result = cover_for_title_uncached(client, title).await;
    let mut cache = load_cover_cache();
    cache.insert(title.to_string(), result.clone());
    save_cover_cache(&cache);
    result
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
    streamingEpisodes { title }
  }
}
"#;

pub async fn discord_media_for(client: &reqwest::Client, title: &str) -> crate::discord::MediaInfo {
    let empty = crate::discord::MediaInfo { image: None, url: None, episode_titles: Default::default() };
    let resp = match client
        .post(ANILIST_URL)
        .json(&json!({"query": DISCORD_MEDIA_QUERY, "variables": {"search": title}}))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => {
            crate::platform::debug_log(&format!("discord_media_for({title}): request failed: {e}"));
            return empty;
        }
    };
    if !resp.status().is_success() {
        crate::platform::debug_log(&format!("discord_media_for({title}): anilist returned {}", resp.status()));
        return empty;
    }
    let body: serde_json::Value = match resp.json().await {
        Ok(b) => b,
        Err(e) => {
            crate::platform::debug_log(&format!("discord_media_for({title}): body parse failed: {e}"));
            return empty;
        }
    };
    let Some(media) = body.get("data").and_then(|d| d.get("Media")) else {
        crate::platform::debug_log(&format!("discord_media_for({title}): no match on anilist"));
        return empty;
    };
    let image = media
        .get("coverImage")
        .and_then(|c| c.get("extraLarge").or_else(|| c.get("large")))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let url = media.get("siteUrl").and_then(|v| v.as_str()).map(|s| s.to_string());

    // AniList's streamingEpisodes titles look like "Episode 9 - Emperor Dragon" for
    // sites (Crunchyroll etc) that publish per-episode subtitles. Pull the number and
    // the subtitle out so we can show "Emperor Dragon" instead of just "Episode 9".
    let mut episode_titles = std::collections::HashMap::new();
    if let Some(eps) = media.get("streamingEpisodes").and_then(|v| v.as_array()) {
        for ep in eps {
            let Some(raw) = ep.get("title").and_then(|v| v.as_str()) else { continue };
            let Some(rest) = raw.strip_prefix("Episode ") else { continue };
            let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
            if digits.is_empty() {
                continue;
            }
            let subtitle = rest[digits.len()..].trim_start_matches(|c: char| c == '-' || c == ' ').trim();
            if !subtitle.is_empty() {
                episode_titles.insert(digits, subtitle.to_string());
            }
        }
    }

    crate::discord::MediaInfo { image, url, episode_titles }
}

// Some ISPs (India, notably) hijack DNS for themoviedb.org to a walled-garden
// page instead of returning NXDOMAIN or refusing the connection, so a normal
// request just hangs/fails against a bogus IP. Cloudflare's DNS-over-HTTPS
// resolver sees the real address, so fall back to it and pin the connection
// to that IP directly, bypassing the system resolver for just this host.
async fn doh_resolve(client: &reqwest::Client, host: &str) -> Option<std::net::IpAddr> {
    let resp = client
        .get("https://1.1.1.1/dns-query")
        .header("accept", "application/dns-json")
        .query(&[("name", host), ("type", "A")])
        .send()
        .await
        .ok()?;
    let data: serde_json::Value = resp.json().await.ok()?;
    let ip_str = data.get("Answer")?.as_array()?.iter().find_map(|a| a.get("data")?.as_str())?;
    ip_str.parse().ok()
}

async fn tmdb_client_bypassing_dns_block(fallback: &reqwest::Client) -> Option<reqwest::Client> {
    let ip = doh_resolve(fallback, "api.themoviedb.org").await?;
    reqwest::Client::builder()
        .resolve("api.themoviedb.org", std::net::SocketAddr::new(ip, 443))
        .build()
        .ok()
}

async fn tmdb_get_json(client: &reqwest::Client, path: &str, params: &[(&str, &str)]) -> Option<serde_json::Value> {
    let url = format!("https://api.themoviedb.org/3/{path}");
    let resp = match client.get(&url).query(params).send().await {
        Ok(r) => r,
        Err(e) => {
            crate::platform::debug_log(&format!("tmdb {path}: request failed ({e}), retrying via DoH-resolved IP"));
            let bypass = tmdb_client_bypassing_dns_block(client).await?;
            bypass.get(&url).query(params).send().await.ok()?
        }
    };
    if !resp.status().is_success() {
        return None;
    }
    resp.json().await.ok()
}

pub async fn tmdb_id_for(client: &reqwest::Client, api_key: &str, title: &str, is_movie: bool) -> Option<i64> {
    if api_key.trim().is_empty() {
        return None;
    }
    let kind = if is_movie { "movie" } else { "tv" };
    let data = tmdb_get_json(client, &format!("search/{kind}"), &[("api_key", api_key), ("query", title)]).await?;
    data.get("results")?.as_array()?.first()?.get("id")?.as_i64()
}

#[derive(Clone, Debug)]
pub struct TmdbSearchResult {
    pub id: i64,
    pub title: String,
    pub year: String,
    pub is_movie: bool,
}

pub async fn tmdb_search(client: &reqwest::Client, api_key: &str, query: &str) -> Vec<TmdbSearchResult> {
    if api_key.trim().is_empty() || query.trim().is_empty() {
        return vec![];
    }
    let Some(data) = tmdb_get_json(client, "search/multi", &[("api_key", api_key), ("query", query)]).await else {
        return vec![];
    };
    let Some(results) = data.get("results").and_then(|v| v.as_array()) else {
        return vec![];
    };
    results
        .iter()
        .filter_map(|r| {
            let media_type = r.get("media_type")?.as_str()?;
            let is_movie = match media_type {
                "movie" => true,
                "tv" => false,
                _ => return None,
            };
            let title = r.get(if is_movie { "title" } else { "name" })?.as_str()?.to_string();
            let date = r.get(if is_movie { "release_date" } else { "first_air_date" }).and_then(|v| v.as_str()).unwrap_or("");
            let year = date.get(..4).unwrap_or("").to_string();
            let id = r.get("id")?.as_i64()?;
            Some(TmdbSearchResult { id, title, year, is_movie })
        })
        .collect()
}

pub fn rivestream_embed_url(tmdb_id: i64, is_movie: bool, season: &str, episode: &str) -> String {
    if is_movie {
        format!("https://www.rivestream.app/embed?type=movie&id={tmdb_id}")
    } else {
        let season = if season.trim().is_empty() { "1" } else { season.trim() };
        let episode = if episode.trim().is_empty() { "1" } else { episode.trim() };
        format!("https://www.rivestream.app/embed?type=tv&id={tmdb_id}&season={season}&episode={episode}")
    }
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

pub fn anidb_mal_id(anime_id: &str) -> anyhow::Result<Option<String>> {
    let page = curl_impersonate(&format!("{ANIDB_BASE}/anime/{anime_id}"))?;
    let re = regex::Regex::new(r"https://myanimelist\.net/anime/(\d+)/")?;
    Ok(re.captures(&page).map(|c| c[1].to_string()))
}

pub struct SkipTimes {
    pub op: Option<(f64, f64)>,
    pub ed: Option<(f64, f64)>,
}

pub fn ani_skip_times(mal_id: &str, canonical_episode: i64) -> anyhow::Result<SkipTimes> {
    let bin = crate::platform::find_ani_skip().unwrap_or_else(|| "ani-skip".to_string());
    let out = std::process::Command::new(bin)
        .args(["-i", mal_id, "-e", &canonical_episode.to_string()])
        .output()?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    let re = regex::Regex::new(r"skip-(op_start|op_end|ed_start|ed_end)=([\d.]+)")?;
    let mut vals = std::collections::HashMap::new();
    for cap in re.captures_iter(&stdout) {
        vals.insert(cap[1].to_string(), cap[2].parse::<f64>().unwrap_or(0.0));
    }
    let op = match (vals.get("op_start"), vals.get("op_end")) {
        (Some(&s), Some(&e)) => Some((s, e)),
        _ => None,
    };
    let ed = match (vals.get("ed_start"), vals.get("ed_end")) {
        (Some(&s), Some(&e)) => Some((s, e)),
        _ => None,
    };
    Ok(SkipTimes { op, ed })
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

pub async fn latest_release_tag(client: &reqwest::Client) -> Option<String> {
    let resp = client
        .get("https://api.github.com/repos/arshnah/aniani/releases/latest")
        .header("User-Agent", "aniani")
        .send()
        .await
        .ok()?;
    let body: serde_json::Value = resp.json().await.ok()?;
    body.get("tag_name").and_then(|v| v.as_str()).map(|s| s.trim_start_matches('v').to_string())
}

pub fn is_newer_version(latest: &str, current: &str) -> bool {
    fn parts(v: &str) -> Vec<u64> {
        v.split('.').map(|p| p.parse().unwrap_or(0)).collect()
    }
    let (l, c) = (parts(latest), parts(current));
    for i in 0..l.len().max(c.len()) {
        let lv = l.get(i).copied().unwrap_or(0);
        let cv = c.get(i).copied().unwrap_or(0);
        if lv != cv {
            return lv > cv;
        }
    }
    false
}
