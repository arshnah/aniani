use scraper::{Html, Selector};

use crate::sources::{Episode, SearchResult};

const BASE: &str = "https://aniwatchtv.to";

fn sel(s: &str) -> Selector {
    Selector::parse(s).unwrap()
}

pub fn search(query: &str) -> anyhow::Result<Vec<SearchResult>> {
    let url = format!("{BASE}/search?keyword={}", urlencoding::encode(query));
    let body = reqwest::blocking::get(&url)?.text()?;
    let doc = Html::parse_document(&body);
    let card_sel = sel("div.flw-item");
    let title_sel = sel("h3.film-name a");

    let mut out = vec![];
    for card in doc.select(&card_sel) {
        let Some(title_el) = card.select(&title_sel).next() else { continue };
        let Some(href) = title_el.value().attr("href") else { continue };
        let id = href.trim_matches('/').replace("?ref=search", "");
        let title = title_el.value().attr("title").unwrap_or_default().to_string();
        out.push(SearchResult { id, title });
    }
    Ok(out)
}

pub fn episodes(anime_id: &str) -> anyhow::Result<Vec<Episode>> {
    let anime_url = format!("{BASE}/{anime_id}");
    let client = reqwest::blocking::Client::new();
    let body = client.get(&anime_url).send()?.text()?;
    let doc = Html::parse_document(&body);

    let wrapper_sel = sel("div#wrapper");
    let ajax_id = doc
        .select(&wrapper_sel)
        .next()
        .and_then(|e| e.value().attr("data-id"))
        .map(|s| s.to_string())
        .unwrap_or_else(|| anime_id.rsplit('-').next().unwrap_or(anime_id).to_string());

    let ajax_url = format!("{BASE}/ajax/v2/episode/list/{ajax_id}");
    let resp: serde_json::Value = client
        .get(&ajax_url)
        .header("X-Requested-With", "XMLHttpRequest")
        .header("Referer", &anime_url)
        .send()?
        .json()?;
    let Some(html) = resp.get("html").and_then(|v| v.as_str()) else { return Ok(vec![]) };

    let ep_doc = Html::parse_document(html);
    let ep_sel = sel("div.ss-list a.ssl-item.ep-item");
    let mut out = vec![];
    for ep in ep_doc.select(&ep_sel) {
        let Some(href) = ep.value().attr("href") else { continue };
        let Some(ep_num) = href.split("ep=").nth(1).and_then(|s| s.split('&').next()) else { continue };
        out.push(Episode {
            ep_ref: format!("{anime_id}$episode${ep_num}"),
            ep_no: ep.value().attr("data-number").unwrap_or(ep_num).to_string(),
        });
    }
    Ok(out)
}

pub fn watch(_ep_ref: &str, _dub: bool) -> anyhow::Result<Option<crate::sources::WatchLink>> {
    anyhow::bail!("yuma stream resolution not implemented, see TODO.txt")
}
