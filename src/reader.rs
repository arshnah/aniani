use eframe::egui;
use scraper::node::Node as HtmlNode;
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::mangadex;
use crate::state;
use crate::theme;
use crate::worker;

const IMAGE_EXTS: &[&str] = &["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "ico", "tga", "qoi", "pnm", "dds"];
const BOOKCOVER_API: &str = "https://bookcovers.arshnah.in/bookcover";

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum BookKind {
    Cbz,
    Cbr,
    Pdf,
    Epub,
    MangaDex,
}

impl BookKind {
    fn from_ext(ext: &str) -> Option<Self> {
        match ext.to_ascii_lowercase().as_str() {
            "cbz" | "zip" => Some(BookKind::Cbz),
            "cbr" | "rar" => Some(BookKind::Cbr),
            "pdf" => Some(BookKind::Pdf),
            "epub" => Some(BookKind::Epub),
            _ => None,
        }
    }

    fn label(&self) -> &'static str {
        match self {
            BookKind::Cbz => "cbz",
            BookKind::Cbr => "cbr",
            BookKind::Pdf => "pdf",
            BookKind::Epub => "epub",
            BookKind::MangaDex => "mangadex",
        }
    }

    fn supported(&self) -> bool {
        matches!(self, BookKind::Cbz | BookKind::Cbr | BookKind::Pdf | BookKind::Epub | BookKind::MangaDex)
    }
}

#[derive(Clone)]
pub struct LocalBook {
    pub path: PathBuf,
    pub title: String,
    pub author: Option<String>,
    pub kind: BookKind,
    pub cover: Option<String>,
}

#[derive(Clone)]
enum CoverState {
    Pending,
    Found(String),
    NotFound,
}

#[derive(Clone)]
enum DlState {
    Downloading,
    Done,
    Failed(String),
}

fn manga_download_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_default().join("Manga/aniani")
}

fn safe(name: &str) -> String {
    name.chars().map(|c| if "<>:\"/\\|?*".contains(c) { '_' } else { c }).collect::<String>().trim().to_string()
}

fn manga_chapter_dest(manga_title: &str, chapter_number: &str) -> PathBuf {
    manga_download_dir().join(safe(manga_title)).join(format!("ch {chapter_number}.cbz"))
}

#[derive(Clone)]
enum ChapterBlock {
    Heading(String),
    Text(String),
    Image(Vec<u8>),
}

#[derive(Clone)]
struct Chapter {
    title: String,
    blocks: Vec<ChapterBlock>,
}

#[derive(Clone)]
struct Page {
    blocks: Vec<ChapterBlock>,
}

enum Content {
    Pages(Vec<Vec<u8>>),
    RemotePages(Vec<String>),
    Book(Vec<Chapter>),
}

struct OpenBook {
    book: LocalBook,
    content: Content,
    page: usize,
    chapter: usize,
    paginated_cache: Option<((usize, i32, i32, i32), Vec<Page>)>,
}

pub struct ReaderState {
    prefs: state::ReaderPrefs,
    books: Vec<LocalBook>,
    scan_error: Option<String>,
    open: Option<OpenBook>,
    open_error: Option<String>,
    show_chapters: bool,
    http: reqwest::Client,
    rt: tokio::runtime::Handle,
    player: worker::PlayerHandle,
    covers: Arc<Mutex<HashMap<String, CoverState>>>,
    last_presence: Option<String>,
    online_mode: bool,
    md_query: String,
    md_searching: Arc<Mutex<bool>>,
    md_results: Arc<Mutex<Vec<mangadex::MangaResult>>>,
    md_error: Arc<Mutex<Option<String>>>,
    md_selected: Option<mangadex::MangaResult>,
    md_chapters: Arc<Mutex<Vec<mangadex::ChapterResult>>>,
    md_loading_chapter: Arc<Mutex<Option<String>>>,
    pending_mangadex_open: Option<(LocalBook, Arc<Mutex<Option<anyhow::Result<Vec<String>>>>>)>,
    md_downloads: Arc<Mutex<HashMap<String, DlState>>>,
}

impl ReaderState {
    pub fn new(http: reqwest::Client, rt: tokio::runtime::Handle, player: worker::PlayerHandle) -> Self {
        let prefs = state::load_reader_prefs();
        let mut s = ReaderState {
            prefs,
            books: vec![],
            scan_error: None,
            open: None,
            open_error: None,
            show_chapters: false,
            http,
            rt,
            player,
            covers: Arc::new(Mutex::new(HashMap::new())),
            last_presence: None,
            online_mode: false,
            md_query: String::new(),
            md_searching: Arc::new(Mutex::new(false)),
            md_results: Arc::new(Mutex::new(vec![])),
            md_error: Arc::new(Mutex::new(None)),
            md_selected: None,
            md_chapters: Arc::new(Mutex::new(vec![])),
            md_loading_chapter: Arc::new(Mutex::new(None)),
            pending_mangadex_open: None,
            md_downloads: Arc::new(Mutex::new(HashMap::new())),
        };
        s.rescan();
        s
    }

    fn rescan(&mut self) {
        self.books.clear();
        self.scan_error = None;
        if self.prefs.library_dir.is_empty() {
            return;
        }
        let root = PathBuf::from(&self.prefs.library_dir);
        if !root.is_dir() {
            self.scan_error = Some("that folder doesn't exist".into());
            return;
        }
        let mut found = vec![];
        scan_dir(&root, 0, &mut found);
        found.sort_by(|a, b| a.title.to_lowercase().cmp(&b.title.to_lowercase()));
        self.books = found;
    }

    fn poll_mangadex_open(&mut self) {
        let Some((_, pending)) = &self.pending_mangadex_open else { return };
        let result = pending.lock().unwrap().take();
        let Some(result) = result else { return };
        let (book, _) = self.pending_mangadex_open.take().unwrap();
        match result {
            Ok(urls) => {
                let title = book.title.clone();
                let cover = book.cover.clone();
                self.open = Some(OpenBook { book, content: Content::RemotePages(urls), page: 0, chapter: 0, paginated_cache: None });
                self.set_presence(&format!("Reading {title}"), cover);
            }
            Err(e) => self.open_error = Some(format!("couldn't load that chapter: {e}")),
        }
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        self.poll_mangadex_open();
        if let Some(err) = self.open_error.clone() {
            let mut dismiss = false;
            ui.horizontal(|ui| {
                ui.colored_label(theme::ERROR, &err);
                if ui.button("dismiss").clicked() {
                    dismiss = true;
                }
            });
            if dismiss {
                self.open_error = None;
            }
            ui.separator();
        }
        if self.open.is_some() {
            self.book_ui(ui);
            return;
        }

        ui.horizontal(|ui| {
            if ui.selectable_label(!self.online_mode, "local files").clicked() {
                self.online_mode = false;
            }
            if ui.selectable_label(self.online_mode, "mangadex").clicked() {
                self.online_mode = true;
            }
        });
        ui.separator();

        if self.online_mode {
            self.online_ui(ui);
        } else {
            self.library_ui(ui);
        }
    }

    fn library_ui(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label("library folder:");
            let resp = ui.text_edit_singleline(&mut self.prefs.library_dir);
            if resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                state::save_reader_prefs(&self.prefs);
                self.rescan();
            }
            if ui.button("browse…").clicked() {
                if let Some(dir) = rfd::FileDialog::new().pick_folder() {
                    self.prefs.library_dir = dir.to_string_lossy().to_string();
                    state::save_reader_prefs(&self.prefs);
                    self.rescan();
                }
            }
            if ui.button("rescan").clicked() {
                state::save_reader_prefs(&self.prefs);
                self.rescan();
            }
        });
        ui.label(egui::RichText::new("cbz, cbr, pdf (jpeg-scanned pages) and epub all read directly, with covers, chapters and saved position").weak());

        if let Some(err) = &self.scan_error {
            ui.colored_label(theme::ERROR, err);
            return;
        }

        ui.separator();

        if self.books.is_empty() {
            ui.label("no cbz/cbr/pdf/epub files found in that folder");
            return;
        }

        let positions = state::load_reader_positions();
        let mut to_open: Option<LocalBook> = None;
        let columns = ((ui.available_width() / 180.0).floor() as usize).max(1);
        egui::Grid::new("reader_library_grid").num_columns(columns).spacing(egui::vec2(14.0, 14.0)).show(ui, |ui| {
            for (i, book) in self.books.iter().enumerate() {
                let key = book.path.to_string_lossy().to_string();
                let resumed = positions.get(&key).map(|p| p.chapter > 0 || p.page > 0).unwrap_or(false);
                let cover_url = self.ensure_cover(book, &key);
                ui.vertical(|ui| {
                    ui.set_width(164.0);
                    egui::Frame::group(ui.style()).rounding(6.0).inner_margin(10.0).show(ui, |ui| {
                        ui.set_width(144.0);
                        if let Some(url) = &cover_url {
                            ui.add(egui::Image::from_uri(url).max_height(160.0).fit_to_exact_size(egui::vec2(124.0, 160.0)).rounding(4.0));
                            ui.add_space(6.0);
                        }
                        ui.horizontal(|ui| {
                            ui.label(egui::RichText::new(format!("[{}]", book.kind.label())).weak().size(11.0));
                            if resumed {
                                ui.label(egui::RichText::new("· in progress").color(theme::ACCENT).size(11.0));
                            }
                        });
                        ui.add_space(4.0);
                        ui.label(egui::RichText::new(&book.title).strong());
                        ui.add_space(6.0);
                        ui.add_enabled_ui(book.kind.supported(), |ui| {
                            let label = if resumed { "continue" } else { "open" };
                            if ui.add_sized([124.0, 24.0], egui::Button::new(label)).clicked() {
                                to_open = Some(book.clone());
                            }
                        });
                    });
                });
                if (i + 1) % columns == 0 {
                    ui.end_row();
                }
            }
        });

        if let Some(book) = to_open {
            self.open_book(book);
        }
    }

    /// Kicks off (once) an async lookup against the user's bookcovers.arshnah.in
    /// service for an epub with known title/author, returning any cover already found.
    fn ensure_cover(&self, book: &LocalBook, key: &str) -> Option<String> {
        if book.kind != BookKind::Epub {
            return None;
        }
        let author = book.author.as_ref()?;
        let mut covers = self.covers.lock().unwrap();
        match covers.get(key) {
            Some(CoverState::Found(url)) => return Some(url.clone()),
            Some(_) => return None,
            None => {}
        }
        covers.insert(key.to_string(), CoverState::Pending);
        drop(covers);

        let http = self.http.clone();
        let title = book.title.clone();
        let author = author.clone();
        let key = key.to_string();
        let covers = self.covers.clone();
        self.rt.spawn(async move {
            let found = fetch_bookcover(&http, &title, &author).await;
            let state = match found {
                Some(url) => CoverState::Found(url),
                None => CoverState::NotFound,
            };
            covers.lock().unwrap().insert(key, state);
        });
        None
    }

    fn open_book(&mut self, mut book: LocalBook) {
        self.open_error = None;
        let key = book.path.to_string_lossy().to_string();
        let saved = state::load_reader_positions().get(&key).copied().unwrap_or_default();
        if let Some(CoverState::Found(url)) = self.covers.lock().unwrap().get(&key) {
            book.cover = Some(url.clone());
        }
        let title = book.title.clone();
        let cover = book.cover.clone();
        match book.kind {
            BookKind::Cbz => match load_cbz(&book.path) {
                Ok(pages) => {
                    let page = saved.page.min(pages.len().saturating_sub(1));
                    self.open = Some(OpenBook { book, content: Content::Pages(pages), page, chapter: 0, paginated_cache: None });
                    self.set_presence(&format!("Reading {title}"), cover);
                }
                Err(e) => self.open_error = Some(format!("couldn't open {}: {e}", book.title)),
            },
            BookKind::Cbr => match load_cbr(&book.path) {
                Ok(pages) => {
                    let page = saved.page.min(pages.len().saturating_sub(1));
                    self.open = Some(OpenBook { book, content: Content::Pages(pages), page, chapter: 0, paginated_cache: None });
                    self.set_presence(&format!("Reading {title}"), cover);
                }
                Err(e) => self.open_error = Some(format!("couldn't open {}: {e}", book.title)),
            },
            BookKind::Pdf => match load_pdf(&book.path) {
                Ok(pages) => {
                    let page = saved.page.min(pages.len().saturating_sub(1));
                    self.open = Some(OpenBook { book, content: Content::Pages(pages), page, chapter: 0, paginated_cache: None });
                    self.set_presence(&format!("Reading {title}"), cover);
                }
                Err(e) => self.open_error = Some(format!("couldn't open {}: {e}", book.title)),
            },
            BookKind::Epub => match load_epub(&book.path) {
                Ok(chapters) => {
                    let chapter = saved.chapter.min(chapters.len().saturating_sub(1));
                    self.open = Some(OpenBook { book, content: Content::Book(chapters), page: saved.page, chapter, paginated_cache: None });
                    self.show_chapters = false;
                    self.set_presence(&format!("Reading {title}"), cover);
                }
                Err(e) => self.open_error = Some(format!("couldn't open {}: {e}", book.title)),
            },
            _ => self.open_error = Some(format!("{} isn't supported yet", book.kind.label())),
        }
    }

    fn set_presence(&mut self, detail: &str, cover: Option<String>) {
        if self.last_presence.as_deref() != Some(detail) {
            self.player.reading(detail, cover);
            self.last_presence = Some(detail.to_string());
        }
    }

    fn online_ui(&mut self, ui: &mut egui::Ui) {
        ui.label(
            egui::RichText::new("live search against MangaDex's public API (api.mangadex.org) -- credit belongs to the scanlation groups shown per chapter")
                .weak(),
        );
        ui.horizontal(|ui| {
            let resp = ui.text_edit_singleline(&mut self.md_query);
            let go = ui.button("search").clicked() || (resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)));
            if go && !self.md_query.trim().is_empty() {
                self.search_mangadex();
            }
        });

        if *self.md_searching.lock().unwrap() {
            ui.label(egui::RichText::new("searching…").color(theme::MUTED));
        }
        if let Some(err) = self.md_error.lock().unwrap().clone() {
            ui.colored_label(theme::ERROR, &err);
        }
        ui.separator();

        if let Some(selected) = self.md_selected.clone() {
            self.mangadex_chapter_list(ui, &selected);
            return;
        }

        let results = self.md_results.lock().unwrap().clone();
        let mut pick: Option<mangadex::MangaResult> = None;
        let columns = ((ui.available_width() / 180.0).floor() as usize).max(1);
        egui::Grid::new("mangadex_results_grid").num_columns(columns).spacing(egui::vec2(14.0, 14.0)).show(ui, |ui| {
            for (i, m) in results.iter().enumerate() {
                ui.vertical(|ui| {
                    ui.set_width(164.0);
                    egui::Frame::group(ui.style()).rounding(6.0).inner_margin(10.0).show(ui, |ui| {
                        ui.set_width(144.0);
                        if let Some(cover) = &m.cover_url {
                            ui.add(egui::Image::from_uri(cover).fit_to_exact_size(egui::vec2(124.0, 160.0)).rounding(4.0));
                            ui.add_space(6.0);
                        }
                        ui.label(egui::RichText::new(&m.title).strong());
                        ui.add_space(6.0);
                        if ui.add_sized([124.0, 24.0], egui::Button::new("chapters")).clicked() {
                            pick = Some(m.clone());
                        }
                    });
                });
                if (i + 1) % columns == 0 {
                    ui.end_row();
                }
            }
        });
        if let Some(m) = pick {
            self.md_selected = Some(m.clone());
            self.md_chapters.lock().unwrap().clear();
            let http = self.http.clone();
            let out = self.md_chapters.clone();
            let err = self.md_error.clone();
            self.rt.spawn(async move {
                match mangadex::chapters(&http, &m.id).await {
                    Ok(chs) => *out.lock().unwrap() = chs,
                    Err(e) => *err.lock().unwrap() = Some(format!("couldn't load chapters: {e}")),
                }
            });
        }
    }

    fn mangadex_chapter_list(&mut self, ui: &mut egui::Ui, manga: &mangadex::MangaResult) {
        ui.horizontal(|ui| {
            if ui.button("back").clicked() {
                self.md_selected = None;
                self.md_chapters.lock().unwrap().clear();
                return;
            }
            ui.heading(&manga.title);
        });
        if self.md_selected.is_none() {
            return;
        }
        ui.separator();

        let loading = self.md_loading_chapter.lock().unwrap().clone();
        let chapters = self.md_chapters.lock().unwrap().clone();
        if chapters.is_empty() && loading.is_none() {
            ui.label(egui::RichText::new("loading chapters…").color(theme::MUTED));
        }

        let downloads = self.md_downloads.lock().unwrap().clone();
        let mut open_chapter: Option<mangadex::ChapterResult> = None;
        let mut download_chapter: Option<mangadex::ChapterResult> = None;
        egui::ScrollArea::vertical().show(ui, |ui| {
            for ch in &chapters {
                ui.horizontal(|ui| {
                    let label = if ch.title.is_empty() {
                        format!("chapter {}", ch.number)
                    } else {
                        format!("chapter {} · {}", ch.number, ch.title)
                    };
                    if let Some(url) = &ch.external_url {
                        if ui.button("open official site").clicked() {
                            let _ = open::that(url);
                        }
                    } else {
                        let busy = loading.as_deref() == Some(ch.id.as_str());
                        ui.add_enabled_ui(!busy, |ui| {
                            if ui.button(if busy { "loading…" } else { "read" }).clicked() {
                                open_chapter = Some(ch.clone());
                            }
                        });

                        let dest_exists = manga_chapter_dest(&manga.title, &ch.number).exists();
                        match downloads.get(&ch.id) {
                            Some(DlState::Downloading) => {
                                ui.label(egui::RichText::new("downloading…").color(theme::MUTED));
                            }
                            Some(DlState::Failed(e)) => {
                                if ui.button("retry download").on_hover_text(e).clicked() {
                                    download_chapter = Some(ch.clone());
                                }
                            }
                            _ if dest_exists => {
                                ui.label(egui::RichText::new("downloaded").color(theme::ACCENT));
                            }
                            _ => {
                                if ui.button("download").clicked() {
                                    download_chapter = Some(ch.clone());
                                }
                            }
                        }
                    }
                    ui.label(label);
                    if let Some(group) = &ch.group {
                        ui.label(egui::RichText::new(format!("· scanlation: {group}")).weak().size(11.0));
                    }
                    if ch.external_url.is_some() {
                        ui.label(egui::RichText::new("· licensed, not hosted by mangadex").weak().size(11.0));
                    }
                });
            }
        });

        if let Some(ch) = open_chapter {
            self.open_mangadex_chapter(manga, &ch);
        }
        if let Some(ch) = download_chapter {
            self.download_mangadex_chapter(manga, &ch);
        }
    }

    fn download_mangadex_chapter(&mut self, manga: &mangadex::MangaResult, chapter: &mangadex::ChapterResult) {
        self.md_downloads.lock().unwrap().insert(chapter.id.clone(), DlState::Downloading);
        let http = self.http.clone();
        let chapter_id = chapter.id.clone();
        let dest = manga_chapter_dest(&manga.title, &chapter.number);
        let downloads = self.md_downloads.clone();
        self.rt.spawn(async move {
            let result = match mangadex::page_urls(&http, &chapter_id).await {
                Ok(urls) => mangadex::download_chapter_cbz(&http, &urls, &dest).await,
                Err(e) => Err(e),
            };
            let state = match result {
                Ok(()) => DlState::Done,
                Err(e) => DlState::Failed(e.to_string()),
            };
            downloads.lock().unwrap().insert(chapter_id, state);
        });
    }

    fn search_mangadex(&mut self) {
        *self.md_error.lock().unwrap() = None;
        self.md_results.lock().unwrap().clear();
        *self.md_searching.lock().unwrap() = true;
        let http = self.http.clone();
        let query = self.md_query.clone();
        let out = self.md_results.clone();
        let err = self.md_error.clone();
        let searching = self.md_searching.clone();
        self.rt.spawn(async move {
            match mangadex::search(&http, &query).await {
                Ok(results) => *out.lock().unwrap() = results,
                Err(e) => *err.lock().unwrap() = Some(format!("search failed: {e}")),
            }
            *searching.lock().unwrap() = false;
        });
    }

    fn open_mangadex_chapter(&mut self, manga: &mangadex::MangaResult, chapter: &mangadex::ChapterResult) {
        *self.md_loading_chapter.lock().unwrap() = Some(chapter.id.clone());
        let http = self.http.clone();
        let chapter_id = chapter.id.clone();
        let book = LocalBook {
            path: PathBuf::from(format!("mangadex://{chapter_id}")),
            title: format!("{} · chapter {}", manga.title, chapter.number),
            author: None,
            kind: BookKind::MangaDex,
            cover: manga.cover_url.clone(),
        };
        let loading = self.md_loading_chapter.clone();
        let pending: Arc<Mutex<Option<anyhow::Result<Vec<String>>>>> = Arc::new(Mutex::new(None));
        let pending_out = pending.clone();
        self.rt.spawn(async move {
            let result = mangadex::page_urls(&http, &chapter_id).await;
            *pending_out.lock().unwrap() = Some(result);
            *loading.lock().unwrap() = None;
        });
        self.pending_mangadex_open = Some((book, pending));
    }

    fn save_position(&self) {
        let Some(open) = &self.open else { return };
        let key = open.book.path.to_string_lossy().to_string();
        state::save_reader_position(&key, state::ReaderPosition { chapter: open.chapter, page: open.page });
    }

    fn book_ui(&mut self, ui: &mut egui::Ui) {
        let title = self.open.as_ref().unwrap().book.title.clone();
        let mut back_clicked = false;
        ui.horizontal(|ui| {
            if ui.button("back").clicked() {
                back_clicked = true;
            }
            ui.heading(&title);
        });
        if back_clicked {
            self.save_position();
            self.open = None;
            self.set_presence("Browsing the reader library", None);
            return;
        }

        let prefs = &mut self.prefs;
        let open = self.open.as_mut().unwrap();
        let mut prefs_changed = false;
        let mut position_changed = false;

        ui.horizontal(|ui| {
            let is_book = matches!(open.content, Content::Book(_));
            if ui.selectable_label(!prefs.scroll_mode, "paginated").clicked() {
                prefs.scroll_mode = false;
                prefs_changed = true;
            }
            if ui.selectable_label(prefs.scroll_mode, "scroll").clicked() {
                prefs.scroll_mode = true;
                prefs_changed = true;
            }
            if !is_book {
                ui.separator();
                if ui.selectable_label(!prefs.right_to_left, "left-to-right").clicked() {
                    prefs.right_to_left = false;
                    prefs_changed = true;
                }
                if ui.selectable_label(prefs.right_to_left, "right-to-left (manga)").clicked() {
                    prefs.right_to_left = true;
                    prefs_changed = true;
                }
            }
            if is_book {
                ui.separator();
                if ui.button(if self.show_chapters { "hide chapters" } else { "chapters" }).clicked() {
                    self.show_chapters = !self.show_chapters;
                }
                ui.separator();
                ui.label("aA");
                if ui.add(egui::Slider::new(&mut prefs.font_size, 12.0..=32.0).show_value(false)).changed() {
                    prefs_changed = true;
                }
            }
            if let Some(n) = image_page_count(&open.content) {
                if !prefs.scroll_mode {
                    ui.separator();
                    ui.label(format!("page {} / {}", open.page + 1, n));
                }
            }
        });
        ui.separator();

        let show_chapters = self.show_chapters;
        let is_book = matches!(open.content, Content::Book(_));
        if is_book {
            render_book(ui, prefs, open, show_chapters, &mut position_changed);
        } else {
            render_image_pages(ui, prefs, open, &mut position_changed);
        }

        if prefs_changed {
            state::save_reader_prefs(prefs);
        }
        if position_changed {
            self.save_position();
        }

        if let Some(open) = &self.open {
            let detail = match &open.content {
                Content::Book(chapters) => chapters
                    .get(open.chapter)
                    .map(|c| format!("Reading {} · {}", open.book.title, c.title))
                    .unwrap_or_else(|| format!("Reading {}", open.book.title)),
                Content::Pages(_) | Content::RemotePages(_) => format!("Reading {}", open.book.title),
            };
            let cover = open.book.cover.clone();
            self.set_presence(&detail, cover);
        }
    }
}

fn image_page_count(content: &Content) -> Option<usize> {
    match content {
        Content::Pages(p) => Some(p.len()),
        Content::RemotePages(p) => Some(p.len()),
        Content::Book(_) => None,
    }
}

fn page_image(open: &OpenBook, i: usize) -> Option<egui::Image<'static>> {
    match &open.content {
        Content::Pages(pages) => {
            let bytes = pages.get(i)?.clone();
            let uri = format!("bytes://{}/{i}", open.book.path.display());
            Some(egui::Image::from_bytes(uri, bytes))
        }
        Content::RemotePages(urls) => {
            let url = urls.get(i)?.clone();
            Some(egui::Image::from_uri(url))
        }
        Content::Book(_) => None,
    }
}

fn render_image_pages(ui: &mut egui::Ui, prefs: &state::ReaderPrefs, open: &mut OpenBook, position_changed: &mut bool) {
    let Some(n) = image_page_count(&open.content) else { return };

    if prefs.scroll_mode {
        egui::ScrollArea::vertical().id_salt("reader_scroll").show(ui, |ui| {
            for i in 0..n {
                if let Some(img) = page_image(open, i) {
                    ui.add(img.fit_to_exact_size(egui::vec2(ui.available_width(), ui.available_width() * 1.4)).shrink_to_fit());
                }
                ui.add_space(2.0);
            }
        });
        return;
    }

    let (prev_key, next_key) = if prefs.right_to_left {
        (egui::Key::ArrowRight, egui::Key::ArrowLeft)
    } else {
        (egui::Key::ArrowLeft, egui::Key::ArrowRight)
    };
    if ui.input(|i| i.key_pressed(prev_key)) && open.page > 0 {
        open.page -= 1;
        *position_changed = true;
    }
    if ui.input(|i| i.key_pressed(next_key)) && open.page + 1 < n {
        open.page += 1;
        *position_changed = true;
    }
    ui.horizontal(|ui| {
        let (prev_label, next_label) = if prefs.right_to_left { ("next", "prev") } else { ("prev", "next") };
        let (first_enabled, first_delta, second_enabled, second_delta): (bool, i64, bool, i64) = if prefs.right_to_left {
            (open.page + 1 < n, 1, open.page > 0, -1)
        } else {
            (open.page > 0, -1, open.page + 1 < n, 1)
        };
        if ui.add_enabled(first_enabled, egui::Button::new(prev_label)).clicked() {
            open.page = (open.page as i64 + first_delta) as usize;
            *position_changed = true;
        }
        if ui.add_enabled(second_enabled, egui::Button::new(next_label)).clicked() {
            open.page = (open.page as i64 + second_delta) as usize;
            *position_changed = true;
        }
        ui.add_space(8.0);
        let mut slider_page = open.page as f32 + 1.0;
        if ui.add(egui::Slider::new(&mut slider_page, 1.0..=(n.max(1) as f32))).changed() {
            open.page = (slider_page.round() as usize).saturating_sub(1).min(n.saturating_sub(1));
            *position_changed = true;
        }
    });

    let avail = ui.available_size();
    if let Some(img) = page_image(open, open.page) {
        ui.centered_and_justified(|ui| {
            ui.add(img.max_size(avail).shrink_to_fit());
        });
    }
}

fn render_book(ui: &mut egui::Ui, prefs: &mut state::ReaderPrefs, open: &mut OpenBook, show_chapters: bool, position_changed: &mut bool) {
    let num_chapters = match &open.content {
        Content::Book(chs) => chs.len(),
        _ => return,
    };

    ui.horizontal(|ui| {
        if show_chapters {
            let mut clicked: Option<usize> = None;
            egui::SidePanel::left("reader_chapters")
                .resizable(true)
                .default_width(220.0)
                .show_inside(ui, |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        let Content::Book(chapters) = &open.content else { return };
                        for (i, ch) in chapters.iter().enumerate() {
                            let selected = i == open.chapter;
                            if ui.selectable_label(selected, format!("{}. {}", i + 1, ch.title)).clicked() {
                                clicked = Some(i);
                            }
                        }
                    });
                });
            if let Some(i) = clicked {
                open.chapter = i;
                open.page = 0;
                open.paginated_cache = None;
                *position_changed = true;
            }
        }

        ui.vertical(|ui| {
            let title = match &open.content {
                Content::Book(chs) => chs.get(open.chapter).map(|c| c.title.clone()),
                _ => None,
            };
            let Some(title) = title else { return };

            let mut chapter_delta: i64 = 0;
            ui.horizontal(|ui| {
                let at_first = open.chapter == 0;
                let at_last = open.chapter + 1 >= num_chapters;
                if ui.add_enabled(!at_first, egui::Button::new("prev chapter")).clicked() {
                    chapter_delta = -1;
                }
                ui.label(egui::RichText::new(&title).strong());
                if ui.add_enabled(!at_last, egui::Button::new("next chapter")).clicked() {
                    chapter_delta = 1;
                }
            });
            if chapter_delta != 0 {
                open.chapter = (open.chapter as i64 + chapter_delta) as usize;
                open.page = 0;
                open.paginated_cache = None;
                *position_changed = true;
            }
            ui.separator();

            if prefs.scroll_mode {
                let Content::Book(chapters) = &open.content else { return };
                let Some(chapter) = chapters.get(open.chapter) else { return };
                egui::ScrollArea::vertical().id_salt(("reader_book_scroll", open.chapter)).show(ui, |ui| {
                    ui.set_max_width(760.0);
                    for block in &chapter.blocks {
                        render_block(ui, block, prefs.font_size, &open.book.path, open.chapter);
                    }
                });
                return;
            }

            let wrap_width = ui.available_width().min(760.0);
            let page_height = (ui.available_height() - 70.0).max(200.0);
            let cache_key = (open.chapter, (wrap_width / 10.0).round() as i32, (page_height / 10.0).round() as i32, prefs.font_size.round() as i32);
            if open.paginated_cache.as_ref().map(|(key, _)| *key) != Some(cache_key) {
                let pages = match &open.content {
                    Content::Book(chapters) => chapters
                        .get(open.chapter)
                        .map(|c| paginate_chapter(ui, &c.blocks, prefs.font_size, wrap_width, page_height))
                        .unwrap_or_default(),
                    _ => vec![],
                };
                open.paginated_cache = Some((cache_key, pages));
            }
            let n = open.paginated_cache.as_ref().map(|(_, p)| p.len()).unwrap_or(0);
            open.page = open.page.min(n.saturating_sub(1));

            if ui.input(|i| i.key_pressed(egui::Key::ArrowLeft)) && open.page > 0 {
                open.page -= 1;
                *position_changed = true;
            }
            if ui.input(|i| i.key_pressed(egui::Key::ArrowRight)) && open.page + 1 < n {
                open.page += 1;
                *position_changed = true;
            }

            egui::ScrollArea::vertical().id_salt(("reader_book_page", open.chapter, open.page)).show(ui, |ui| {
                ui.set_max_width(760.0);
                if let Some((_, pages)) = &open.paginated_cache {
                    if let Some(page) = pages.get(open.page) {
                        for block in &page.blocks {
                            render_block(ui, block, prefs.font_size, &open.book.path, open.chapter);
                        }
                    }
                }
            });

            ui.separator();
            ui.horizontal(|ui| {
                if ui.add_enabled(open.page > 0, egui::Button::new("prev page")).clicked() {
                    open.page -= 1;
                    *position_changed = true;
                }
                ui.label(format!("page {} / {}", open.page + 1, n));
                if ui.add_enabled(open.page + 1 < n, egui::Button::new("next page")).clicked() {
                    open.page += 1;
                    *position_changed = true;
                }
            });
        });
    });
}

fn render_block(ui: &mut egui::Ui, block: &ChapterBlock, font_size: f32, book_path: &Path, chapter: usize) {
    match block {
        ChapterBlock::Heading(text) => {
            ui.add_space(6.0);
            ui.label(egui::RichText::new(text).size(font_size * 1.3).strong());
            ui.add_space(4.0);
        }
        ChapterBlock::Text(text) => {
            ui.label(egui::RichText::new(text).size(font_size));
            ui.add_space(font_size * 0.6);
        }
        ChapterBlock::Image(bytes) => {
            let hash = bytes.len();
            let uri = format!("bytes://{}/{chapter}/{hash}", book_path.display());
            ui.vertical_centered(|ui| {
                ui.add(egui::Image::from_bytes(uri, bytes.clone()).max_width(ui.available_width().min(500.0)).shrink_to_fit());
            });
            ui.add_space(8.0);
        }
    }
}

fn measure_height(ui: &egui::Ui, text: &str, font: egui::FontId, wrap_width: f32) -> f32 {
    ui.fonts(|f| f.layout(text.to_string(), font, egui::Color32::WHITE, wrap_width).size().y)
}

fn paginate_chapter(ui: &egui::Ui, blocks: &[ChapterBlock], font_size: f32, wrap_width: f32, max_height: f32) -> Vec<Page> {
    let heading_font = egui::FontId::proportional(font_size * 1.3);
    let text_font = egui::FontId::proportional(font_size);

    let mut pages = vec![];
    let mut cur: Vec<ChapterBlock> = vec![];
    let mut cur_height = 0.0f32;
    for block in blocks {
        match block {
            ChapterBlock::Image(_) => {
                if !cur.is_empty() {
                    pages.push(Page { blocks: std::mem::take(&mut cur) });
                    cur_height = 0.0;
                }
                pages.push(Page { blocks: vec![block.clone()] });
            }
            ChapterBlock::Heading(text) => {
                let h = measure_height(ui, text, heading_font.clone(), wrap_width) + 10.0;
                if !cur.is_empty() && cur_height + h > max_height {
                    pages.push(Page { blocks: std::mem::take(&mut cur) });
                    cur_height = 0.0;
                }
                cur_height += h;
                cur.push(block.clone());
            }
            ChapterBlock::Text(text) => {
                let h = measure_height(ui, text, text_font.clone(), wrap_width) + font_size * 0.6;
                if !cur.is_empty() && cur_height + h > max_height {
                    pages.push(Page { blocks: std::mem::take(&mut cur) });
                    cur_height = 0.0;
                }
                cur_height += h;
                cur.push(block.clone());
            }
        }
    }
    if !cur.is_empty() {
        pages.push(Page { blocks: cur });
    }
    if pages.is_empty() {
        pages.push(Page { blocks: vec![] });
    }
    pages
}

fn scan_dir(dir: &Path, depth: u32, out: &mut Vec<LocalBook>) {
    if depth > 4 {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            scan_dir(&path, depth + 1, out);
        } else if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            if let Some(kind) = BookKind::from_ext(ext) {
                let fallback_title = path.file_stem().map(|s| humanize_filename_title(&s.to_string_lossy())).unwrap_or_default();
                let (title, author) = if kind == BookKind::Epub {
                    epub_meta(&path).unwrap_or((fallback_title, None))
                } else {
                    (fallback_title, None)
                };
                out.push(LocalBook { path: path.clone(), title, author, kind, cover: None });
            }
        }
    }
}

fn load_cbz(path: &Path) -> anyhow::Result<Vec<Vec<u8>>> {
    let file = fs::File::open(path)?;
    let mut archive = zip::ZipArchive::new(file)?;
    let mut names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            let ext = Path::new(&name).extension()?.to_str()?.to_ascii_lowercase();
            IMAGE_EXTS.contains(&ext.as_str()).then_some(name)
        })
        .collect();
    names.sort();

    let mut pages = Vec::with_capacity(names.len());
    for name in names {
        let mut entry = archive.by_name(&name)?;
        let mut buf = Vec::with_capacity(entry.size() as usize);
        std::io::copy(&mut entry, &mut buf)?;
        pages.push(buf);
    }
    if pages.is_empty() {
        anyhow::bail!("no image pages found in archive");
    }
    Ok(pages)
}

fn load_cbr(path: &Path) -> anyhow::Result<Vec<Vec<u8>>> {
    let archive = unrar::Archive::new(path);
    let mut cursor = archive.open_for_processing().map_err(|e| anyhow::anyhow!("{e}"))?;
    let mut entries: Vec<(String, Vec<u8>)> = vec![];
    loop {
        let Some(with_header) = cursor.read_header().map_err(|e| anyhow::anyhow!("{e}"))? else { break };
        let name = with_header.entry().filename.to_string_lossy().to_string();
        let is_image = Path::new(&name)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| IMAGE_EXTS.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false);
        if is_image {
            let (bytes, next) = with_header.read().map_err(|e| anyhow::anyhow!("{e}"))?;
            entries.push((name, bytes));
            cursor = next;
        } else {
            cursor = with_header.skip().map_err(|e| anyhow::anyhow!("{e}"))?;
        }
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    let pages: Vec<Vec<u8>> = entries.into_iter().map(|(_, bytes)| bytes).collect();
    if pages.is_empty() {
        anyhow::bail!("no image pages found in archive");
    }
    Ok(pages)
}

fn load_pdf(path: &Path) -> anyhow::Result<Vec<Vec<u8>>> {
    let doc = lopdf::Document::load(path).map_err(|e| anyhow::anyhow!("{e}"))?;
    let mut pages = Vec::new();
    for (_, page_id) in doc.get_pages() {
        let images = doc.get_page_images(page_id).map_err(|e| anyhow::anyhow!("{e}"))?;
        let jpeg = images
            .into_iter()
            .find(|img| img.filters.as_ref().map(|f| f.iter().any(|f| f == "DCTDecode")).unwrap_or(false));
        if let Some(img) = jpeg {
            pages.push(img.content.to_vec());
        }
    }
    if pages.is_empty() {
        anyhow::bail!("no JPEG page images found -- only scanned/JPEG-based PDFs are supported, not vector or text PDFs");
    }
    Ok(pages)
}

fn normalize_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn load_epub(path: &Path) -> anyhow::Result<Vec<Chapter>> {
    let mut doc = epub::doc::EpubDoc::new(path).map_err(|e| anyhow::anyhow!("{e}"))?;
    let mut chapters = Vec::new();

    for i in 0..doc.spine.len() {
        if !doc.set_current_chapter(i) {
            continue;
        }
        let base_dir = doc.get_current_path().and_then(|p| p.parent().map(|p| p.to_path_buf())).unwrap_or_default();
        let Some((content, _mime)) = doc.get_current_str() else { continue };

        let fragment = scraper::Html::parse_fragment(&content);

        let heading_text = heading_selector()
            .and_then(|sel| fragment.select(&sel).next())
            .map(|el| el.text().collect::<Vec<_>>().join(" ").split_whitespace().collect::<Vec<_>>().join(" "))
            .filter(|s| !s.is_empty());

        let mut blocks: Vec<ChapterBlock> = vec![];
        if let Some(h) = &heading_text {
            blocks.push(ChapterBlock::Heading(h.clone()));
        }

        let mut text_buf = String::new();
        for node_ref in fragment.tree.root().descendants() {
            match node_ref.value() {
                HtmlNode::Text(t) => {
                    let s = &**t;
                    if !s.trim().is_empty() {
                        text_buf.push_str(s);
                    }
                }
                HtmlNode::Element(el) => {
                    let name = el.name();
                    if name == "img" || name == "image" {
                        let src = el.attr("src").or_else(|| el.attr("xlink:href")).or_else(|| el.attr("href"));
                        if let Some(src) = src {
                            let resolved = normalize_path(&base_dir.join(src));
                            if let Some(bytes) = doc.get_resource_by_path(&resolved) {
                                let pending = text_buf.trim();
                                if !pending.is_empty() {
                                    blocks.push(ChapterBlock::Text(pending.to_string()));
                                }
                                text_buf.clear();
                                blocks.push(ChapterBlock::Image(bytes));
                            }
                        }
                    } else if matches!(name, "p" | "div" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "br" | "li") {
                        let pending = text_buf.trim();
                        if !pending.is_empty() {
                            blocks.push(ChapterBlock::Text(pending.to_string()));
                        }
                        text_buf.clear();
                    }
                }
                _ => {}
            }
        }
        let pending = text_buf.trim();
        if !pending.is_empty() {
            blocks.push(ChapterBlock::Text(pending.to_string()));
        }

        if blocks.is_empty() {
            continue;
        }

        let title = heading_text.unwrap_or_else(|| format!("Chapter {}", chapters.len() + 1));
        chapters.push(Chapter { title, blocks });
    }

    if chapters.is_empty() {
        anyhow::bail!("no readable content found in epub");
    }
    Ok(chapters)
}

async fn fetch_bookcover(http: &reqwest::Client, title: &str, author: &str) -> Option<String> {
    let resp = http
        .get(BOOKCOVER_API)
        .query(&[("book_title", title), ("author_name", author)])
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    body.get("url").and_then(|v| v.as_str()).map(|s| s.to_string())
}

fn humanize_filename_title(stem: &str) -> String {
    let mut s = stem.to_string();
    if let Some(pos) = s.rfind('_') {
        let suffix = &s[pos + 1..];
        if suffix.len() >= 2
            && suffix.len() <= 8
            && suffix.chars().any(|c| c.is_ascii_digit())
            && suffix.chars().any(|c| c.is_ascii_alphabetic())
        {
            s.truncate(pos);
        }
    }
    s.chars()
        .map(|c| if c == '_' || c == '-' { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn epub_meta(path: &Path) -> Option<(String, Option<String>)> {
    let doc = epub::doc::EpubDoc::new(path).ok()?;
    let title = doc.get_title().filter(|t| !t.trim().is_empty())?;
    let author = doc.mdata("creator").map(|m| m.value.clone()).filter(|a| !a.trim().is_empty());
    Some((title, author))
}

fn heading_selector() -> Option<scraper::Selector> {
    scraper::Selector::parse("h1, h2, h3").ok()
}
