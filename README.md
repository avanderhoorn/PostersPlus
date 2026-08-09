# PostersPlus

A self-hosted poster generation service that composites extensive metadata onto TMDB posters - ratings, award sashes, quality badges, and title logos - served as ready-to-use JPEGs. PostersPlus is compatible with AIOMetadata, Bingecat, Plex, Jellyfin, and really any application that can pass IMDb/TMDB IDs and type.

Those not self-hosting can [visit the public instance.](https://postersplus.elfhosted.com)

---

## Showcase
<p align="center">
  <img src="Showcase/frosted_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/clean_sash.png?raw=true" width="23%"/>
  <img src="Showcase/rating_bar.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_sash.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/clean_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/rating_bar_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_original_art.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch_silver.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch_gold.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_sash_original_art.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_lowlogo_cinema.jpg?raw=true" width="23%"/>
</p>
<p align="center">
  Client featured is a slightly modified Stremio Kai by allecsc
</p>
<p align="center">
  <img src="Showcase/frosted_kai_large.png?raw=true" width="92%"/>
</p>

---

## Features

- **Ratings overlay** - weighted composite score from Letterboxd, Trakt, Rotten Tomatoes, IMDb, Metacritic, TMDb, MyAnimeList, and more. Three display modes (Score Bar, Clean, Minimalist, Bar) with many sub-modes. Three colour palettes, poster-aware overlays for a frosted look and optional glow on high scores.

- **Award sashes** - Oscar Best Picture, Golden Globe (film and TV, five major categories), Emmy Outstanding Series (Drama, Comedy, Limited), festival winners, notable studios/directors/cast, trending titles, newly streaming (release-date recency plus r/movieleaks tracking), cult classics, true stories, and Metacritic Must-See. Priority order is fully configurable and any sash can be disabled. Can also be rendered as a notch, for those that prefer a more modern look.

- **Quality badges** - six display modes: Quality Notch (vertical tier-coloured accent pill), Quality + Age Rating (age numeral tinted by 4K/Remux/HDR tier), Badge Row (PNG icons for 4K, 1080p, Remux, Web, DV, HDR10+, HDR10), Combined Text Badge, Age Rating Only, or hidden. A minimum quality threshold (`badge_min_score`) can suppress the badge when stream quality falls below a configurable bar. Sourced from an AIOStreams integration, Torrentio or Comet and fetched in the background on first request. Plex and Jellyfin will use your local files' actual quality for completely accurate data.

- **Title logos** - TMDB/Metahub logos composited over the poster with configurable size and position. Many options for language preference, including native, original or fully textless.

- **Art fallback chain** - when a title has no textless poster on TMDB the landscape backdrop is cropped to portrait using face and visual-saliency detection. If no poster exists at all, you'll get either a minimalist gradient background or photorealistic fallback plus your usual ratings and info sash. Replace `static/genre_bg/<style>/<Genre>.png` with your own 500×750 PNG to use custom art.

- **Web configurator** - browser-based UI to tune every parameter and generate a ready-to-paste URL template. Tabbed layout covering Core, Rating, Logo, Sash, Quality, and Weights. Per-section info modals, URL import (paste any `/poster` URL to hydrate every control), persistent settings, a preset gallery with ready-made styles, light/dark mode toggle, and a mobile-optimised expanded preview.

- **Plex and Jellyfin sync** - companion scripts (`plex_sync.py` / `jellyfin_sync.py`) that read your media library, derive quality tokens from each title's actual file metadata, and push PostersPlus-generated posters back as library covers. Includes an `--inspect` mode for auditing token derivation without writing anything.

- **Composite poster cache** - fully rendered posters are cached by config hash and served directly on repeat requests, with configurable TTL and max-entry cap.

- **Operator overrides** - drop a `discovery_overrides.json` into the cache volume to replace or merge the built-in notable-studio / director / cast lists without editing source. A huge optional env list to choose your own preferences about how the application runs, rather than having them forced on you.

- **OCR detection** - automatically detects posters marked on TMDB as textless to avoid printing a logo for a more consistent experience.

- **Cinema greyscale** - greyscales content still in cinemas, as well as the ability to force the info sash to prioritize cinema for these examples.

- **Original art mode** - if you don't like textless posters with logo overlays, swap back to original art and use the bar or minimalist modes with the award sash for an overlay that still works with almost all posters by staying out of the way.

---

## Self-Hosted Requirements

- Docker
- A free [TMDB API key](https://www.themoviedb.org/settings/api) for posters, logos and metadata.
- A free [MDBList API key](https://mdblist.com/) for ratings and keywords.
- An [AIOMetadata](https://github.com/cedya77/aiometadata) config. Self hosted or public instance are both fine. Plex, Jellyfin or Bingecat don't need this.
- Optionally, a quality source for quality badges (choose one):
  - An [AIOStreams](https://github.com/Viren070/AIOStreams) self hosted instance (set `AIOSTREAMS_URL` + `AIOSTREAMS_AUTH`), **or**
  - Any standalone Stremio stream addon such as [Torrentio](https://torrentio.strem.fun) or [Comet](https://comet.elfhosted.com) (set `QUALITY_SOURCE=scraper` + `SCRAPER_URL` to the addon's base URL, e.g. `https://torrentio.strem.fun/`). Note: Stremthru Torz requires authentication and won't work standalone; use it via AIOStreams instead.

---

## Quick Start

> **HTTPS or AIOMetadata's proxy option is required for production use.**
> If going HTTPS route ensure the access_key env is set to protect your instance.
> Good reverse proxy choices are [Traefik](https://traefik.io/) which has great support from Viren's templates or [Caddy](https://caddyserver.com/) which is very simple.
> If going for AIOMetadata's proxy you don't expose PostersPlus to the internet. Use http://postersplus:8000 in the URL instead of a domain to have them communicate via Docker's internal network. The proxy route is slightly slower but maximizes security.

### Using the pre-built image (recommended)

Pre-built images for `amd64` and `arm64` are published to the GitHub Container Registry on every release.

Create a `compose.yaml` with the following content, substituting your own values:

```yaml
services:
  postersplus:
    image: ghcr.io/umbraprojects/postersplus:latest
    ports:
      - "8000:8000"    # change the left side if port 8000 is already in use
    restart: unless-stopped
    volumes:
      - ./postersplus-cache:/app/cache
    environment:
      - TMDB_API_KEY=your_tmdb_key
      - MDBLIST_API_KEY=your_mdblist_key
      - WORKERS=1
      - TEXTLESS_DETECTION_CONCURRENCY=2
      - TEXTLESS_DETECTION_MAX_VOTES=3000
      - ACCESS_KEY=youraccesskey # Highly suggested if exposing to the internet.*
      # See .env.example for all available options
```

Then start it:

```bash
docker compose up -d
```

Once your reverse proxy is set up, open the configurator at your public HTTPS domain to tune your settings and generate a URL template for AIOMetadata. The URL it generates is based on the domain you access it from.

### Building from source

```bash
git clone https://github.com/UmbraProjects/PostersPlus.git
cd PostersPlus
cp .env.example .env   # fill in your keys
docker compose up -d --build
```

---

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and fill in your values. Every variable is optional - API keys can be omitted from the server and passed per-request as URL parameters instead.

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | - | TMDB API key for poster/metadata fetching |
| `MDBLIST_API_KEY` | - | MDBList API key for ratings and award data |
| `MDBLIST_API_KEY_2` | - | Optional second MDBList key. Retried in the same request when the primary key is rate-limited |
| `MDBLIST_CONCURRENCY` | `3` | Maximum concurrent outbound MDBList requests per worker |
| `ACCESS_KEY` | - | Shared secret accepted in `X-Jellyfin-Artwork-Key`; legacy routes also accept `access_key` in the query unless header-only mode is enabled |
| `HEADER_ONLY_AUTH` | `false` | Require header authentication globally. `/health` and static configurator assets remain anonymous; `/render/selected` is always header-only |
| `RENDER_PROFILE_PATH` | - | Path to one read-only mounted YAML render profile (see `profile.example.yml`) |
| `POSTERSPLUS_SOURCE_REVISION` | `unknown` | Exact source commit reported by `/ready`; release images set this automatically |
| `SELECTED_MAX_BYTES` | `10485760` | Maximum encoded request-body size for `/render/selected` |
| `SELECTED_MAX_WIDTH` / `SELECTED_MAX_HEIGHT` | `8000` | Maximum decoded selected-image dimensions |
| `SELECTED_MAX_PIXELS` | `20000000` | Maximum decoded selected-image pixel count |
| `WORKERS` | `1` | Uvicorn worker processes. One worker avoids duplicate uncached renders, scans, and API work across processes |
| `AIOSTREAMS_URL` | - | Base URL of your AIOStreams instance (used when `QUALITY_SOURCE=aiostreams`) |
| `AIOSTREAMS_AUTH` | - | AIOStreams credentials as Base64 `user:password` |
| `QUALITY_SOURCE` | `aiostreams` | Quality data source: `aiostreams` or `scraper`. Set to `scraper` to use any Stremio stream addon instead of AIOStreams |
| `SCRAPER_URL` | - | Base URL of a Stremio stream addon (e.g. `https://torrentio.strem.fun/`). Only used when `QUALITY_SOURCE=scraper`. Standalone addons like Torrentio and Comet work best; Stremthru Torz requires auth and should be used via AIOStreams instead |
| `QUALITY_OLD_CACHE_DURATION` | `90` | Days to cache quality data for titles older than 2 weeks |
| `QUALITY_BG_CONCURRENCY` | `5` | Max concurrent background quality fetches |
| `QUALITY_WAIT_TIMEOUT` | `30` | Maximum seconds to wait when a request enables synchronous quality fetching |
| `CDN_CACHE_TTL` | `0` | Adds `Cache-Control: public, max-age=N` to poster responses. Set to `0` to disable |
| `JPEG_QUALITY` | `85` | JPEG output quality for composited posters (70–95). Raise to `92` for higher fidelity; lower to reduce file size |
| `COMPOSITE_CACHE_TTL` | `604800` | Seconds to keep a rendered poster before re-rendering (default 7 days) |
| `COMPOSITE_MAX_ENTRIES` | `0` | Cap on composite cache entries. `0` = no cap |
| `DISABLE_COMPOSITE_CACHE` | - | Set to `true` to skip composite cache reads and writes entirely. Every request re-renders from scratch. For development only |
| `LOGO_CONTRAST_RESCUE` | `false` | Recolour a flat logo (white/black/accent) when it blends into the poster background. Multi-colour/outline logos are never touched. Experimental, off by default while tested; set `true` to enable |
| `LOGO_STRETCH_DISABLED` | `true` | Fill-stretch is off by default; every logo is kept at its true clamped size. Set `false` to enable the stretch below |
| `LOGO_STRETCH_FACTOR` | `1.2` | When stretching is enabled, a slim logo is enlarged toward its size cap by up to this factor (one axis only). `1.0` = no enlargement |
| `DEBUG_LOGO_SIZING` | `false` | Log per-logo sizing telemetry at INFO level. For tuning only |
| `TMDB_POSTER_MIN_VOTES` | `3` | Prefer textless posters with at least this many votes when they remain competitively rated |
| `TMDB_POSTER_MAX_SCORE_DROP` | `1.0` | Maximum rating downgrade allowed when preferring a textless poster that meets the vote minimum |
| `RATING_MIN_VOTES` | `10` | Ignore provider ratings below this vote count. Roger Ebert is exempt |
| `TEXTLESS_TEXT_DETECTION` | `true` | Detect burned-in title text on posters TMDB mislabelled as "textless" and skip our own logo so the title isn't doubled. Set `false` to opt out |
| `TEXTLESS_DETECTION_MAX_VOTES` | `3000` | Foreground OCR vote limit. Higher-vote assets render without waiting, skip composite caching, and enter the idle background scan queue. Raise for foreground accuracy; lower for faster stale-cache bursts |
| `TEXTLESS_FAKE_REPORT` | `true` | Record OCR-rejected TMDB posters in a deduplicated human-review report |
| `TEXTLESS_FAKE_REPORT_PATH` | `/app/cache/fake_textless_posters.txt` | Report location. The default persists in the existing cache volume |
| `PPOCR_BOX_THRESHOLD` | `0.70` | Minimum PP-OCR text-box confidence. Higher is stricter; changing it invalidates cached detections and composites |
| `PPOCR_WIDE_BOX_THRESHOLD` | `0.30` | Lower confidence accepted for wide, title-shaped text regions |
| `PPOCR_WIDE_MIN_ASPECT` | `3.0` | Minimum width-to-height ratio for the lower-confidence title fallback |
| `PPOCR_WIDE_MIN_AREA` | `0.01` | Minimum fraction of image area occupied by a lower-confidence title box |
| `PPOCR_WIDE_MIN_Y` | `0.55` | Minimum vertical centre for the poster-only geometric fallback when OCR cannot read a centred title block |
| `TEXTLESS_DETECTION_CONCURRENCY` | `2` | Independent PP-OCR sessions in a dedicated executor. Use `1` on small hosts; each extra session uses roughly 25-40 MB; capped at 4 and CPU count |
| `TEXTLESS_SCAN_TOP` | `0.08` | Fraction of poster height skipped from the top before counting text (covers top/middle/bottom titles; ignores top-edge logos) |
| `BAKE_PPOCR_MODEL` | `true` | Build-time only. Bake the ~4.6MB PP-OCRv5 Mobile model into the image |
| `DEFAULT_LOGO_LANGUAGE` | `en` | ISO 639-1 language code for title logos |
| `DISCOVERY_OVERRIDES_PATH` | `/app/cache/discovery_overrides.json` | Optional custom path for discovery list overrides |

> CPU guidance: keep `WORKERS × TEXTLESS_DETECTION_CONCURRENCY` at or below the CPU cores available to the container. Larger values can oversubscribe CPU, duplicate uncached work across workers, and reduce sustained throughput.

> The ~4.6 MB PP-OCRv5 Mobile model is baked into the image by default. Set `BAKE_PPOCR_MODEL=false` to download it into the cache volume on first use.

When OCR rejects a TMDB poster marked as textless, Posters Plus records it in
`/app/cache/fake_textless_posters.txt`. Each image appears once, with direct
TMDB and image links for manual review. The report is advisory only and never
edits TMDB automatically; delete it at any time to start a fresh review list.

---

## Plex and Jellyfin Sync

`plex_sync.py` and `jellyfin_sync.py` are companion scripts that read your media library, derive quality tokens from each title's own media-file metadata, and push PostersPlus-generated posters back as library covers. This keeps your Plex or Jellyfin art consistent with the same quality-badge logic used by the Stremio-facing poster endpoint, without relying on AIOStreams or a scraper addon for quality data.

### Requirements

```bash
# Plex
pip install -r requirements-plex.txt

# Jellyfin (httpx only, likely already installed)
pip install -r requirements-jellyfin.txt
```

### Configuration

Set the following environment variables before running, or edit the `_DEFAULT` constants near the top of each script:

**Plex**

| Variable | Description |
|---|---|
| `PLEX_BASE_URL` | Base URL of your Plex server, e.g. `http://192.168.1.50:32400` |
| `PLEX_TOKEN` | Your Plex auth token (sign in at plex.tv → Account → XML → `X-Plex-Token`) |
| `POSTERSPLUS_URL` | Full PostersPlus URL template including your preferred query parameters |

**Jellyfin**

| Variable | Description |
|---|---|
| `JELLYFIN_BASE_URL` | Base URL of your Jellyfin server, e.g. `http://192.168.1.50:8096` |
| `JELLYFIN_API_KEY` | API key from Jellyfin Dashboard → Advanced → API Keys |
| `POSTERSPLUS_URL` | Full PostersPlus URL template including your preferred query parameters |

The `POSTERSPLUS_URL` value should be the full URL template you'd normally give AIOMetadata. Copy it straight from the configurator's output box, replacing the `{tmdb_id}`, `{imdb_id}`, and `{type}` placeholders. Both scripts fill these in automatically from library metadata.

### Usage

Run with `--inspect` first. It logs every library title with the quality tokens that would be derived from its media streams, without writing any posters:

```bash
python plex_sync.py --inspect
python jellyfin_sync.py --inspect
```

Once the output looks correct, run without the flag to fetch and push posters:

```bash
python plex_sync.py
python jellyfin_sync.py
```

Both scripts process Movies and TV Shows. TV quality tokens are derived from a representative episode selected by watch progress, air date, and episode count. Titles where no quality can be determined (unmatched files, virtual library entries from stream plugins) produce no quality badge and are skipped without error.

---

## URL Structure

Posters are served at `/poster` with parameters controlling every aspect of rendering:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&imdb_id={imdb_id}&type={type}
```

Append `&debug=1` to any poster URL to receive a JSON response with all computed metadata (score, genre, sash label, quality tokens, award data, matched cast/directors) instead of rendering the image. Useful for diagnosing unexpected sashes or missing ratings.

Append `&nocache=1` (requires `ACCESS_KEY` to be set and valid) to force a fresh render of a single title, bypassing the composite cache read and re-caching the result. Lets you refresh one poster without flushing the whole cache.

### Private selected-image renderer

`POST /render/selected` composites against the exact JPEG, PNG, or WebP bytes
selected by the caller instead of downloading base artwork. It accepts no source
URL and requires `X-Jellyfin-Artwork-Key`, a loaded named profile, `tmdb_id`,
`imdb_id`, and `type`; `quality`, season/episode, and `output_format`
(`jpeg`, `png`, or `webp`) are optional per-item context.

```bash
curl --fail-with-body \
  -H "X-Jellyfin-Artwork-Key: $ACCESS_KEY" \
  -H "Content-Type: image/jpeg" \
  --data-binary @primary.jpg \
  "http://postersplus:8000/render/selected?profile=homestack-default&type=movie&tmdb_id=123&imdb_id=tt1234567"
```

The response includes an accurate `Content-Type`, standard `Digest`,
hexadecimal `X-Image-SHA256`, `X-Render-Profile-SHA256`, and
`X-Renderer-Revision`. The service rejects oversized, malformed,
MIME-mismatched, or decompression-bomb inputs. Profile defaults use the same
validated request-configuration path as `/poster`; visual query overrides are
not accepted on this endpoint. Mount the profile read-only and set
`RENDER_PROFILE_PATH=/app/profile.yml`.

### Operator endpoints

These are gated by the configured access key:

- `GET /stats`: cache row counts / sizes plus live runtime state (in-flight renders, background fetches, MDBList key cooldowns). Handy for spotting issues before they surface.
- `GET /ready`: readiness plus the loaded profile name/content digest and exact renderer source revision. It never returns credentials.
- `GET /debug/fallback-gallery`: a gallery of every genre's no-art fallback card (mascot + genre font), also reachable via the **Preview fallback art** button in the configurator's Logo section.

---

## Award Sashes

Sashes display contextual metadata about a title - awards, festival recognition, notable cast or crew, and more. The first matching sash in the priority list is shown.

| Sash | Triggers on |
|---|---|
| Best Picture / Emmy Win | Oscar Best Picture winner, Emmy Outstanding Drama/Comedy/Limited winner |
| Golden Globe Win | Golden Globe winner (film drama/comedy, TV drama/comedy/limited) |
| Festival Winner | Cannes, Venice, Sundance, TIFF, and other major festivals |
| Best Picture / Emmy Nom | Oscar Best Picture nominee, Emmy Outstanding nominee |
| Golden Globe Nom | Golden Globe nominee (same categories as above) |
| Notable Studio | A24, Neon, Pixar, and other curated studios |
| Notable Director | Curated list of notable directors |
| Notable Cast | Curated list of notable cast members |
| Trending | Currently in TMDB's trending top 40 |
| Cult Classic | Curated list of cult classics |
| Foreign Language | Non-English language title |
| Newly Streaming | Recently added to streaming |
| Metacritic Must-See | High Metacritic score |
| True Story | Based on a true story |
| Short / Mini / Binge | Short film, miniseries, or bingeable series |

Sash priority order is configurable in the web configurator via drag-and-drop. The Primary Client selector sets recommended edge insets: Stremio TV, Nuvio, Plex, and Jellyfin use `0` for both bar and notch; Stremio Desktop/Web use `0.007` for the bar and `0.004` for the notch. Both sliders remain manually adjustable, and loading a preset preserves them. Existing URLs can override the notch with `sash_badge_inset` and the bar with `bar_bottom_inset`. Individual sashes can be disabled entirely with the ✕ button - disabled sashes are serialised as `-slot_name` in the URL (e.g. `&sash_priority=wins,cast,-trending`).

### Customising Directors, Studios, and Cast

**Source editors** can modify the lists directly in `discovery.py`.

**Docker operators** can override them without editing source by placing a JSON file at `/app/cache/discovery_overrides.json` inside the cache volume. See `discovery_overrides.example.json` for the format.

---

## Ratings

Scores from multiple providers are normalised to a 0–100 scale and combined using configurable weights. Default weights use Letterboxd with Trakt fallback for movies, and Trakt (80%) and Rotten Tomatoes (20%) for TV. Weights are fully adjustable in the web configurator.

---

## Poster Translations

Text rendered onto posters (genre labels and info-sash labels) can be localised. The language follows the request's **poster/logo language** setting.

To add a language, copy `languages/en.json` to `languages/<code>.json` (e.g. `fr.json`) and translate the **values** only; the keys are the canonical English strings and must stay unchanged. Translation is display-only with per-key English fallback: any missing key, malformed file, or language with no JSON falls back to English, so partial translations are safe.

> Note: contributed languages must be **Latin-script**. The bundled font has no CJK/Arabic glyphs and no right-to-left shaping, so those scripts will not render correctly.

---

## Caching

PostersPlus uses SQLite (WAL mode) for metadata and rendered-poster caching, plus filesystem caches for TMDB images. The cache volume is mounted at `/app/cache` and persists across container restarts. Expired database rows and image files are pruned automatically; render-affecting server settings and bundled assets are included in the composite cache signature.

| Cache | Default TTL |
|---|---|
| TMDB posters | 60 days |
| TMDB logos | 60 days |
| TMDB metadata | 7 days |
| Ratings (new titles) | 1 day |
| Ratings (older titles) | 14 days |
| Quality badges (new) | 1 day |
| Quality badges (older) | 90 days |
| Composite posters | 7 days |

---

## Donate & Discord

If you'd like to support development, I'd appreciate it: https://ko-fi.com/umbraprojects

Join the discord here to request features, follow development or report bugs: https://discord.com/invite/wEgTPNXUMU

---

## License

This project and any associated forks should remain open source under the [GNU Affero General Public License v3.0](LICENSE)

Fork images published as `ghcr.io/avanderhoorn/postersplus` are built from this
corresponding source tree. Their exact commit is embedded at build time and
reported by authenticated `GET /ready`; retain the image digest and revision
together when recording provenance. Upstream PostersPlus is maintained at
<https://github.com/UmbraProjects/PostersPlus>.