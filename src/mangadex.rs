const BASE: &str = "https://api.mangadex.org";
const COVERS: &str = "https://uploads.mangadex.org/covers";

#[derive(Clone)]
pub struct MangaResult {
    pub id: String,
    pub title: String,
    pub cover_url: Option<String>,
}

#[derive(Clone)]
pub struct ChapterResult {
    pub id: String,
    pub number: String,
    pub title: String,
    pub group: Option<String>,
    pub external_url: Option<String>,
}

fn first_title(titles: &serde_json::Value) -> String {
    titles
        .as_object()
        .and_then(|m| m.get("en").or_else(|| m.values().next()))
        .and_then(|v| v.as_str())
        .unwrap_or("untitled")
        .to_string()
}

fn relationship<'a>(rels: &'a serde_json::Value, kind: &str) -> Option<&'a serde_json::Value> {
    rels.as_array()?.iter().find(|r| r["type"].as_str() == Some(kind))
}

async fn send(req: reqwest::RequestBuilder) -> anyhow::Result<reqwest::Response> {
    req.send().await.map_err(|e| {
        if e.is_connect() || e.is_timeout() {
            anyhow::anyhow!("couldn't reach mangadex -- check your internet connection")
        } else {
            anyhow::anyhow!("mangadex request failed: {e}")
        }
    })
}

async fn check_status(resp: reqwest::Response) -> anyhow::Result<reqwest::Response> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let body = resp.text().await.unwrap_or_default();
    let detail = serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|v| v["errors"][0]["detail"].as_str().map(|s| s.to_string()));
    match detail {
        Some(detail) => anyhow::bail!("mangadex: {status} -- {detail}"),
        None => anyhow::bail!("mangadex: {status}"),
    }
}

pub async fn search(http: &reqwest::Client, query: &str) -> anyhow::Result<Vec<MangaResult>> {
    let req = http
        .get(format!("{BASE}/manga"))
        .query(&[("title", query), ("limit", "24")])
        .query(&[("includes[]", "cover_art")])
        .query(&[("contentRating[]", "safe"), ("contentRating[]", "suggestive"), ("contentRating[]", "erotica")]);
    let resp = check_status(send(req).await?).await?;
    let body: serde_json::Value = resp.json().await?;
    let mut out = vec![];
    for item in body["data"].as_array().cloned().unwrap_or_default() {
        let id = item["id"].as_str().unwrap_or_default().to_string();
        let title = first_title(&item["attributes"]["title"]);
        let cover_file = relationship(&item["relationships"], "cover_art").and_then(|r| r["attributes"]["fileName"].as_str());
        let cover_url = cover_file.map(|f| format!("{COVERS}/{id}/{f}.256.jpg"));
        out.push(MangaResult { id, title, cover_url });
    }
    Ok(out)
}

pub async fn chapters(http: &reqwest::Client, manga_id: &str) -> anyhow::Result<Vec<ChapterResult>> {
    let req = http
        .get(format!("{BASE}/manga/{manga_id}/feed"))
        .query(&[("translatedLanguage[]", "en"), ("order[chapter]", "asc"), ("limit", "500")])
        .query(&[("includes[]", "scanlation_group")])
        .query(&[("contentRating[]", "safe"), ("contentRating[]", "suggestive"), ("contentRating[]", "erotica")]);
    let resp = check_status(send(req).await?).await?;
    let body: serde_json::Value = resp.json().await?;
    let mut out = vec![];
    for item in body["data"].as_array().cloned().unwrap_or_default() {
        let id = item["id"].as_str().unwrap_or_default().to_string();
        let attrs = &item["attributes"];
        let number = attrs["chapter"].as_str().unwrap_or("?").to_string();
        let title = attrs["title"].as_str().unwrap_or("").to_string();
        let group = relationship(&item["relationships"], "scanlation_group")
            .and_then(|r| r["attributes"]["name"].as_str())
            .map(|s| s.to_string());
        let pages = attrs["pages"].as_i64().unwrap_or(0);
        let external_url = if pages == 0 { attrs["externalUrl"].as_str().map(|s| s.to_string()) } else { None };
        out.push(ChapterResult { id, number, title, group, external_url });
    }
    Ok(out)
}

pub async fn download_chapter_cbz(http: &reqwest::Client, urls: &[String], dest: &std::path::Path) -> anyhow::Result<()> {
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = dest.with_extension("cbz.part");
    let file = std::fs::File::create(&tmp)?;
    let mut zip = zip::ZipWriter::new(file);
    let options = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored);

    for (i, url) in urls.iter().enumerate() {
        let resp = check_status(send(http.get(url)).await?).await?;
        let bytes = resp.bytes().await?;
        let ext = url.rsplit('.').next().filter(|e| e.len() <= 4).unwrap_or("jpg");
        zip.start_file(format!("{i:04}.{ext}"), options)?;
        std::io::Write::write_all(&mut zip, &bytes)?;
    }
    zip.finish()?;
    std::fs::rename(&tmp, dest)?;
    Ok(())
}

pub async fn page_urls(http: &reqwest::Client, chapter_id: &str) -> anyhow::Result<Vec<String>> {
    let req = http.get(format!("{BASE}/at-home/server/{chapter_id}"));
    let resp = check_status(send(req).await?).await?;
    let body: serde_json::Value = resp.json().await?;
    let base = body["baseUrl"].as_str().unwrap_or_default().to_string();
    let hash = body["chapter"]["hash"].as_str().unwrap_or_default().to_string();
    let files = body["chapter"]["data"].as_array().cloned().unwrap_or_default();
    let urls = files.iter().filter_map(|f| f.as_str()).map(|f| format!("{base}/data/{hash}/{f}")).collect::<Vec<_>>();
    if urls.is_empty() {
        anyhow::bail!("mangadex returned no pages for this chapter");
    }
    Ok(urls)
}
