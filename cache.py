#cache.py
import hashlib
import logging
import os
import sqlite3
import threading
import tempfile
import time
import json
from collections import OrderedDict
from datetime import datetime

logger = logging.getLogger(__name__)

from config import (
    DB_PATH,
    DAYS_CONSIDERED_NEW,
    NEW_CACHE_DURATION,
    OLD_CACHE_DURATION,
    TRENDING_CACHE_DURATION,
    TMDB_POSTER_CACHE_DIR,
    TMDB_POSTER_CACHE_DURATION,
    TMDB_LOGO_CACHE_DIR,
    TMDB_LOGO_CACHE_DURATION,
    TMDB_IMAGE_CACHE_JITTER_DAYS,
    TMDB_METADATA_CACHE_DURATION,
    COMPOSITE_CACHE_TTL,
    COMPOSITE_CACHE_TTL_JITTER,
    COMPOSITE_MAX_ENTRIES,
    COMPOSITE_MEM_ENTRIES,
    QUALITY_OLD_CACHE_DURATION,
    DIGITAL_RELEASE_MAX_AGE_DAYS,
    RATING_MIN_VOTES,
)


def _ttl_jitter(cache_key: str, window: float) -> float:
    """Deterministic +/- window/2 offset derived from cache_key, so the same
    key always gets the same jitter (stable across reads and cache-warm
    cycles) while spreading expiry times across a batch of keys."""
    if window <= 0:
        return 0.0
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:8]
    return (int(digest, 16) / 0xFFFFFFFF) * window - window / 2

# One SQLite connection PER THREAD (thread-local).  A single shared connection
# serialises every statement — reads included — on its internal mutex, so under
# load reads queue behind one another and behind writes.  Per-thread connections
# let WAL's concurrent readers actually run in parallel; writes are still
# serialised within this process by _db_lock, and across worker processes by
# SQLite plus the busy timeout below.
_local = threading.local()
_db_lock = threading.Lock()     # serialises writes within this process
_initialised = False


def _apply_conn_pragmas(conn: sqlite3.Connection) -> None:
    """Connection-level PRAGMAs, applied to every connection.  (journal_mode=WAL
    and auto_vacuum are DB-level and persist in the file, so they're set once in
    init_db.)"""
    conn.execute("PRAGMA synchronous=NORMAL")       # safe with WAL; avoids unnecessary fsyncs
    conn.execute("PRAGMA cache_size=-32000")        # 32 MB in-process page cache
    conn.execute("PRAGMA temp_store=MEMORY")        # temp tables/indices stay in RAM
    conn.execute("PRAGMA busy_timeout=15000")       # wait up to 15s if another worker holds the write lock
    conn.execute("PRAGMA wal_autocheckpoint=1000")  # fold WAL back into main DB at 1000 pages (~4 MB)


def _enable_wal_with_retry(conn: sqlite3.Connection) -> None:
    """Enable WAL despite simultaneous worker startup on the same database."""
    for attempt in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 19:
                raise
            time.sleep(0.1)


def get_db() -> sqlite3.Connection:
    if not _initialised:
        raise RuntimeError("Database not initialized")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _apply_conn_pragmas(conn)
        _local.conn = conn
    return conn

def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    """Apply an additive migration safely when multiple workers start together."""
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        # Another worker may have added the column after our PRAGMA snapshot.
        if "duplicate column name" not in str(exc).lower():
            raise


def init_db() -> None:
    global _initialised
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)
    _initialised = True
    conn = get_db()   # this thread's connection, with the per-connection PRAGMAs

    # Enable incremental auto-vacuum so prune_caches' PRAGMA incremental_vacuum
    # can actually return freed pages to the OS.  auto_vacuum can only be set
    # before the first table is created; an existing DB is converted lazily by a
    # one-time VACUUM in prune_caches (off the event loop).  So we only enable it
    # here on a brand-new database.  Must run before any table is created.
    _is_new_db = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0
    if _is_new_db:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    _enable_wal_with_retry(conn)
    # Serialize all schema creation and additive migrations across workers.
    conn.execute("BEGIN IMMEDIATE")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS rating_cache (
        imdb_id        TEXT PRIMARY KEY,
        ratings_json   TEXT,
        genre          TEXT,
        cached_at      INTEGER,
        release_date   TEXT,
        award_wins     TEXT,
        award_noms     TEXT,
        awards_fetched INTEGER NOT NULL DEFAULT 0,
        festival_label TEXT,
        age_rating     INTEGER,
        is_cult        INTEGER NOT NULL DEFAULT 0,
        is_true_story  INTEGER NOT NULL DEFAULT 0,
        is_metacritic  INTEGER NOT NULL DEFAULT 0,
        rating_min_votes INTEGER
    )
    """)

    for col, definition in (
        ("award_wins",     "TEXT NOT NULL DEFAULT ''"),
        ("award_noms",     "TEXT NOT NULL DEFAULT ''"),
        ("awards_fetched", "INTEGER NOT NULL DEFAULT 0"),
        ("festival_label", "TEXT"),
        ("age_rating",     "INTEGER"),
        ("is_cult",        "INTEGER NOT NULL DEFAULT 0"),
        ("is_true_story",  "INTEGER NOT NULL DEFAULT 0"),
        ("is_metacritic",  "INTEGER NOT NULL DEFAULT 0"),
        ("rating_min_votes", "INTEGER"),
    ):
        _add_column_if_missing(conn, "rating_cache", col, definition)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_cache (
            imdb_id      TEXT PRIMARY KEY,
            tokens       TEXT,
            cached_at    INTEGER,
            release_date TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trending_cache (
            media_type    TEXT PRIMARY KEY,
            rankings_json TEXT,
            cached_at     INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tmdb_metadata_cache (
            cache_key           TEXT PRIMARY KEY,
            title               TEXT,
            release_year        TEXT,
            genre_ids           TEXT,
            is_textless         INTEGER,
            poster_path         TEXT,
            logos_json          TEXT,
            cached_at           INTEGER,
            credits_json        TEXT,
            production_cos_json TEXT,
            runtime             INTEGER,
            number_of_seasons   INTEGER,
            number_of_episodes  INTEGER,
            original_language   TEXT,
            backdrop_path       TEXT
        )
    """)

    # Final composite poster cache.
    # Stores the fully composited JPEG so warm requests skip the entire pipeline.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS final_poster_cache (
            cache_key  TEXT PRIMARY KEY,
            jpeg_bytes BLOB    NOT NULL,
            cached_at  INTEGER NOT NULL,
            request_params TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE final_poster_cache ADD COLUMN request_params TEXT")
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_final_poster_cached_at "
        "ON final_poster_cache(cached_at)"
    )

    # Digital release cache.
    # Populated by the r/movieleaks poller; one row per IMDB ID.
    # posted_at is the Reddit post's created_utc (used for expiry).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS digital_release_cache (
            imdb_id   TEXT PRIMARY KEY,
            posted_at INTEGER NOT NULL
        )
    """)

    # Release status cache — populated on demand when the "release_status"
    # sash slot is enabled.  Stored separately from the main metadata cache
    # so users who don't enable the feature never pay the extra API call.
    # cache_key = "{media_type}_{tmdb_id}", status = "BluRay"|"Streaming"|"Cinema"|"Production"
    conn.execute("""
        CREATE TABLE IF NOT EXISTS release_status_cache (
            cache_key TEXT PRIMARY KEY,
            status    TEXT NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Movie release-info cache - richer sibling of release_status_cache for
    # TMDB /release_dates data. Stores JSON with status plus theatrical,
    # digital/TV, and physical dates so multiple sash slots can share one TMDB
    # lookup. cache_key = "{media_type}_{tmdb_id}".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS movie_release_info_cache (
            cache_key TEXT PRIMARY KEY,
            info_json TEXT NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Burned-in-text detection results, keyed by source asset + detection params.
    # The PP-OCR scan depends only on the image bytes and confidence, never
    # on the user's URL config — so memoising it here stops the most expensive
    # feature from re-running on every config change (composite-cache miss).
    # TMDB image paths are content-addressed (immutable), so results never go
    # stale; cached_at exists only for housekeeping/pruning.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS text_detection_cache (
            cache_key TEXT PRIMARY KEY,
            has_text  INTEGER NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS artwork_candidate_cache (
            cache_key    TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            result_class TEXT NOT NULL,
            cached_at    INTEGER NOT NULL
        )
    """)

    # Small generic key/value store for app-level bookkeeping (e.g. the last
    # cache-warm cycle's timestamp) that doesn't warrant its own table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Generic JSON cache for TVDB bookkeeping: resolved TVDB ids (incl. negative
    # "no match" results), per-title artwork indexes, the artwork-type catalogue,
    # and the auth token.  Each row carries its own TTL so different record kinds
    # (long-lived artwork vs. short negative cache) coexist in one table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tvdb_cache (
            cache_key   TEXT PRIMARY KEY,
            value_json  TEXT NOT NULL,
            cached_at   INTEGER NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )
    """)

    # Migrate existing tmdb_metadata_cache rows.
    for col, definition in (
        ("credits_json",        "TEXT"),
        ("production_cos_json", "TEXT"),
        ("runtime",             "INTEGER"),
        ("number_of_seasons",   "INTEGER"),
        ("number_of_episodes",  "INTEGER"),
        ("original_language",   "TEXT"),
        ("original_title",      "TEXT"),
        ("backdrop_path",       "TEXT"),
        ("tmdb_status",         "TEXT"),
        ("vote_count",          "INTEGER"),
        ("text_backdrop_path",  "TEXT"),
        ("original_poster_path","TEXT"),
        ("poster_langs_json",   "TEXT"),
        ("imdb_id",             "TEXT"),
        ("tmdb_release_date",   "TEXT"),
        ("last_air_date",       "TEXT"),
        ("next_episode_json",   "TEXT"),
        ("last_episode_json",   "TEXT"),
        ("seasons_json",        "TEXT"),
        ("metadata_version",    "INTEGER"),
    ):
        _add_column_if_missing(conn, "tmdb_metadata_cache", col, definition)

    conn.commit()


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------

def _rating_ttl(release_date: str | None) -> int:
    if not release_date:
        return OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else OLD_CACHE_DURATION
    except ValueError:
        return OLD_CACHE_DURATION


def _quality_ttl(release_date: str | None) -> int:
    """Quality data is far more stable than ratings for older titles."""
    if not release_date:
        return QUALITY_OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else QUALITY_OLD_CACHE_DURATION
    except ValueError:
        return QUALITY_OLD_CACHE_DURATION


# ---------------------------------------------------------------------------
# Final poster cache  (L1 in-memory LRU + L2 SQLite)
# ---------------------------------------------------------------------------

# L1: bounded in-memory LRU — most-recently-used composites served without
# any SQLite read, keeping the hot set off the OS page cache.
_composite_l1: OrderedDict[str, bytes] = OrderedDict()
_composite_l1_lock = threading.Lock()


def composite_l1_stats() -> dict:
    with _composite_l1_lock:
        count = len(_composite_l1)
        total_bytes = sum(len(v) for v in _composite_l1.values())
    return {"entries": count, "bytes": total_bytes}


def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Return cached JPEG bytes for a fully composited poster, or None on miss/expiry.

    Checks the in-memory LRU (L1) first; falls through to SQLite (L2) on miss
    and promotes the result to L1 so the next hit is served entirely from RAM.
    """
    # L1: in-memory LRU — no disk I/O, no OS page-cache pressure
    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            if cache_key in _composite_l1:
                _composite_l1.move_to_end(cache_key)
                return _composite_l1[cache_key]

    # L2: SQLite with TTL check
    try:
        row = get_db().execute(
            "SELECT jpeg_bytes, cached_at FROM final_poster_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        jpeg_bytes, cached_at = row
        age_secs = time.time() - cached_at
        effective_ttl = COMPOSITE_CACHE_TTL + _ttl_jitter(cache_key, COMPOSITE_CACHE_TTL_JITTER)
        if age_secs > effective_ttl:
            logger.info(f"Final poster cache expired for {cache_key} ({age_secs/86400:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
            return None
        data = bytes(jpeg_bytes)
        # Promote to L1
        if COMPOSITE_MEM_ENTRIES > 0:
            with _composite_l1_lock:
                _composite_l1[cache_key] = data
                _composite_l1.move_to_end(cache_key)
                while len(_composite_l1) > COMPOSITE_MEM_ENTRIES:
                    _composite_l1.popitem(last=False)
        return data
    except Exception as exc:
        logger.error(f"Final poster cache read error: {exc}")
        return None


def set_cached_final_poster(cache_key: str, jpeg_bytes: bytes, request_params: str = None, ttl_override: int = None) -> None:
    """Store a fully composited JPEG poster into L1 (RAM) and L2 (SQLite)."""
    # L1: always store the freshly-rendered composite so the next hit skips SQLite
    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            _composite_l1[cache_key] = jpeg_bytes
            _composite_l1.move_to_end(cache_key)
            while len(_composite_l1) > COMPOSITE_MEM_ENTRIES:
                _composite_l1.popitem(last=False)

    # L2: persist to SQLite for warm restarts
    try:
        with _db_lock:
            now = int(time.time())
            cached_at = now
            if ttl_override is not None and COMPOSITE_CACHE_TTL > ttl_override:
                cached_at = now - (COMPOSITE_CACHE_TTL - ttl_override)
                
            get_db().execute(
                """
                INSERT OR REPLACE INTO final_poster_cache (cache_key, jpeg_bytes, cached_at, request_params)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, jpeg_bytes, cached_at, request_params),
            )
            if COMPOSITE_MAX_ENTRIES > 0:
                (count,) = get_db().execute(
                    "SELECT COUNT(*) FROM final_poster_cache"
                ).fetchone()
                overflow = count - COMPOSITE_MAX_ENTRIES
                if overflow > 0:
                    get_db().execute(
                        "DELETE FROM final_poster_cache WHERE cache_key IN "
                        "(SELECT cache_key FROM final_poster_cache "
                        " ORDER BY cached_at ASC LIMIT ?)",
                        (overflow,),
                    )
                    logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
            get_db().commit()
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")

def delete_cached_final_poster(cache_key: str) -> None:
    """Remove a composited poster from both L1 (RAM) and L2 (SQLite) caches."""
    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            _composite_l1.pop(cache_key, None)
    try:
        with _db_lock:
            get_db().execute("DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,))
            get_db().commit()
    except Exception as exc:
        logger.error(f"Final poster cache delete error: {exc}")

def invalidate_final_posters(tmdb_id: str, media_type: str | None = None) -> None:
    """Invalidate all composited posters for a specific TMDB ID.
    Used when underlying dynamic data (like trending rank or release status)
    changes so the next request renders a fresh poster with updated badges.
    """
    # TV posters are cached under either "tv" or "series" (Stremio requests use
    # "series"), so treat the two as equivalent when filtering by media type —
    # otherwise a trending/status change leaves half the cache stale.
    if media_type in ("tv", "series"):
        type_variants: tuple[str, ...] | None = ("tv", "series")
    elif media_type:
        type_variants = (media_type,)
    else:
        type_variants = None

    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            keys_to_delete = []
            for k in _composite_l1:
                parts = k.split(":")
                if len(parts) >= 3 and parts[1] == tmdb_id:
                    if type_variants is None or parts[2] in type_variants:
                        keys_to_delete.append(k)
            for k in keys_to_delete:
                _composite_l1.pop(k, None)

    try:
        with _db_lock:
            if type_variants is None:
                get_db().execute(
                    "DELETE FROM final_poster_cache WHERE cache_key LIKE ?",
                    (f"%:{tmdb_id}:%",),
                )
            else:
                for _tv in type_variants:
                    get_db().execute(
                        "DELETE FROM final_poster_cache WHERE cache_key LIKE ?",
                        (f"%:{tmdb_id}:{_tv}:%",),
                    )
            get_db().commit()
        logger.info(f"Invalidated final poster cache for tmdb_id={tmdb_id}")
    except Exception as exc:
        logger.error(f"Final poster cache invalidate error: {exc}")


def get_cache_stats() -> dict:
    """
    Return row counts for every cache table plus the composite cache's total
    byte size and the DB file size on disk.  Used by the /stats endpoint so
    operators can see cache health at a glance.  Never raises.
    """
    stats: dict = {}
    try:
        db = get_db()
        for table in (
            "rating_cache", "quality_cache", "trending_cache",
            "tmdb_metadata_cache", "final_poster_cache",
            "digital_release_cache", "release_status_cache",
            "movie_release_info_cache", "text_detection_cache",
            "artwork_candidate_cache",
        ):
            try:
                (n,) = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = n
            except Exception:
                stats[table] = None

        try:
            (total,) = db.execute(
                "SELECT COALESCE(SUM(LENGTH(jpeg_bytes)), 0) FROM final_poster_cache"
            ).fetchone()
            stats["composite_bytes"] = int(total)
        except Exception:
            stats["composite_bytes"] = None

        try:
            stats["db_file_bytes"] = os.path.getsize(DB_PATH)
        except OSError:
            stats["db_file_bytes"] = None

        l1 = composite_l1_stats()
        stats["composite_l1_entries"] = l1["entries"]
        stats["composite_l1_bytes"]   = l1["bytes"]
    except Exception as exc:
        logger.error(f"Cache stats error: {exc}")
    return stats


def prune_caches() -> None:
    """
    Delete expired rows from every SQLite cache table.

    Called periodically by a background task in main.py.  All tables use a
    simple age cutoff; the composite table is the only one large enough to
    matter for storage, but pruning everything keeps the DB tidy.

    For rating/quality we use the maximum possible TTL as the cutoff so we
    never delete an entry that might still be considered fresh for a new
    release.  Any surviving-but-expired rows will be evicted lazily on the
    next read as before.
    """
    now = int(time.time())
    try:
        with _db_lock:
            db = get_db()

            # Composites — fixed TTL in seconds
            r = db.execute(
                "DELETE FROM final_poster_cache WHERE cached_at < ?",
                (now - COMPOSITE_CACHE_TTL,),
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired composite cache entries")

            # Ratings / quality / metadata — use the most generous TTL so we
            # never evict something that could still be considered fresh.
            rating_cutoff   = now - OLD_CACHE_DURATION           * 86400
            quality_cutoff  = now - QUALITY_OLD_CACHE_DURATION   * 86400
            metadata_cutoff = now - TMDB_METADATA_CACHE_DURATION * 86400

            r = db.execute(
                "DELETE FROM rating_cache WHERE cached_at < ?", (rating_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired rating cache entries")

            r = db.execute(
                "DELETE FROM quality_cache WHERE cached_at < ?", (quality_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired quality cache entries")

            r = db.execute(
                "DELETE FROM tmdb_metadata_cache WHERE cached_at < ?", (metadata_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired TMDB metadata cache entries")

            digital_cutoff = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400
            r = db.execute(
                "DELETE FROM digital_release_cache WHERE posted_at < ?", (digital_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired digital release cache entries")

            release_status_cutoff = now - _RELEASE_STATUS_TTL_DAYS * 86400
            r = db.execute(
                "DELETE FROM release_status_cache WHERE cached_at < ?", (release_status_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired release status cache entries")

            r = db.execute(
                "DELETE FROM movie_release_info_cache WHERE cached_at < ?", (release_status_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired movie release info cache entries")

            detection_cutoff = now - 180 * 86400
            r = db.execute(
                "DELETE FROM text_detection_cache WHERE cached_at < ?", (detection_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} old text-detection cache entries")

            r = db.execute(
                "DELETE FROM artwork_candidate_cache WHERE cached_at < ?",
                (now - 24 * 60 * 60,),
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired artwork-candidate entries")

            # Each tvdb_cache row stores its own TTL, so expiry is per-row rather
            # than a single cutoff.
            r = db.execute(
                "DELETE FROM tvdb_cache WHERE (? - cached_at) > ttl_seconds", (now,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired TVDB cache entries")

            db.commit()

        # Use the high end of the per-key jitter range so prune never deletes
        # a file before get_cached_tmdb_poster/_logo would (which apply the
        # same jitter per cache_key).
        _prune_file_cache(TMDB_POSTER_CACHE_DIR, TMDB_POSTER_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2)
        _prune_file_cache(TMDB_LOGO_CACHE_DIR, TMDB_LOGO_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2)

        # Reclaim free pages left by the deletes.
        with _db_lock:
            db = get_db()
            auto_vac = db.execute("PRAGMA auto_vacuum").fetchone()[0]
            if auto_vac == 2:   # INCREMENTAL — cheap, moves a few pages, no long lock
                db.execute("PRAGMA incremental_vacuum(100)")
                db.commit()
            else:
                # Legacy DB created before incremental auto-vacuum (auto_vacuum=0):
                # the incremental pragma is a no-op there, so freed pages (e.g. from
                # evicted composite JPEGs) never return and the file bloats.  Do a
                # one-time conversion: enable INCREMENTAL then full VACUUM to rewrite
                # the DB compactly.  Gated on meaningful dead space so it only fires
                # when worthwhile, and it runs here in the background prune task
                # (off the event loop), so it never blocks request handling.
                page  = db.execute("PRAGMA page_size").fetchone()[0]
                free  = db.execute("PRAGMA freelist_count").fetchone()[0]
                total = db.execute("PRAGMA page_count").fetchone()[0]
                live_mb = page * (total - free) / 1e6
                if page * free > 20 * 1024 * 1024:   # >20 MB reclaimable
                    # VACUUM rewrites ALL live data while holding an exclusive lock.
                    # On a large live set that could exceed busy_timeout and lock out
                    # the other worker process, so cap it: skip (and tell the operator
                    # to VACUUM offline) when the live data is big.  Small DBs convert
                    # in well under a second.  (After the first worker converts,
                    # auto_vacuum becomes INCREMENTAL and every later prune takes the
                    # cheap incremental path above, so this runs at most once.)
                    if live_mb > 256:
                        logger.warning(
                            f"Cache DB has ~{page * free / 1e6:.0f} MB reclaimable but "
                            f"{live_mb:.0f} MB live — skipping automatic VACUUM to avoid "
                            f"a long exclusive lock. Reclaim offline with: "
                            f"sqlite3 {DB_PATH} 'PRAGMA auto_vacuum=INCREMENTAL; VACUUM;'"
                        )
                    else:
                        logger.info(
                            f"Cache DB: one-time conversion to incremental auto-vacuum, "
                            f"reclaiming ~{page * free / 1e6:.0f} MB of dead space "
                            f"({live_mb:.0f} MB live)…"
                        )
                        db.commit()                   # close any open transaction
                        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                        db.execute("VACUUM")
                        logger.info("Cache DB vacuum complete")

    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(
    imdb_id: str,
) -> tuple[
    dict[str, float], str, str | None,
    list[str], list[str], bool,
    str | None, int | None,
    bool, bool, bool,
] | None:
    """
    Returns an 11-tuple:
        (ratings_dict, genre, release_date, award_wins, award_noms,
         awards_fetched, festival_label, age_rating,
         is_cult, is_true_story, is_metacritic)
    Returns None if the row is absent or expired.
    """
    try:
        row = get_db().execute(
            """
            SELECT ratings_json, genre, cached_at, release_date,
                   award_wins, award_noms, awards_fetched, festival_label,
                   age_rating, is_cult, is_true_story, is_metacritic,
                   rating_min_votes
            FROM rating_cache
            WHERE imdb_id = ?
            """,
            (imdb_id,),
        ).fetchone()

        if not row:
            return None

        (ratings_json, genre, cached_at, release_date,
         wins_raw, noms_raw, awards_fetched_int, festival_label,
         age_rating, is_cult_int, is_true_story_int, is_metacritic_int,
         rating_min_votes) = row

        if rating_min_votes is not None and rating_min_votes != RATING_MIN_VOTES:
            logger.info(
                f"Rating cache policy changed for {imdb_id}: "
                f"stored={rating_min_votes!r}, current={RATING_MIN_VOTES}; refreshing"
            )
            with _db_lock:
                get_db().execute(
                    "DELETE FROM rating_cache WHERE imdb_id = ?",
                    (imdb_id,),
                )
                get_db().commit()
            return None

        age_days = (time.time() - cached_at) / 86400

        if age_days > _rating_ttl(release_date):
            logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM rating_cache WHERE imdb_id = ?",
                    (imdb_id,),
                )
                get_db().commit()
            return None

        if rating_min_votes is None:
            # Rows created before policy tracking are still valid until their
            # normal TTL expires. Backfill in place instead of consuming one
            # MDBList request per legacy cache entry after an upgrade.
            with _db_lock:
                get_db().execute(
                    "UPDATE rating_cache SET rating_min_votes = ? "
                    "WHERE imdb_id = ? AND rating_min_votes IS NULL",
                    (RATING_MIN_VOTES, imdb_id),
                )
                get_db().commit()
            logger.debug(f"Backfilled rating cache policy for {imdb_id}")

        ratings_dict = json.loads(ratings_json or "{}")
        wins = [w for w in (wins_raw or "").split("|") if w]
        noms = [n for n in (noms_raw or "").split("|") if n]
        awards_fetched = bool(awards_fetched_int)

        return (ratings_dict, genre, release_date, wins, noms,
                awards_fetched, festival_label, age_rating,
                bool(is_cult_int), bool(is_true_story_int), bool(is_metacritic_int))

    except Exception as exc:
        logger.error(f"Cache read error: {exc}")
        return None


def set_cached_rating(
    imdb_id: str,
    ratings_dict: dict,
    genre: str,
    rel: str | None,
    award_wins: list[str],
    award_noms: list[str],
    awards_fetched: bool = False,
    festival_label: str | None = None,
    age_rating: int | None = None,
    is_cult: bool = False,
    is_true_story: bool = False,
    is_metacritic: bool = False,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO rating_cache
                    (
                        imdb_id,
                        ratings_json,
                        genre,
                        cached_at,
                        release_date,
                        award_wins,
                        award_noms,
                        awards_fetched,
                        festival_label,
                        age_rating,
                        is_cult,
                        is_true_story,
                        is_metacritic,
                        rating_min_votes
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    imdb_id,
                    json.dumps(ratings_dict),
                    genre,
                    int(time.time()),
                    rel,
                    "|".join(award_wins or []),
                    "|".join(award_noms or []),
                    int(awards_fetched),
                    festival_label,
                    age_rating,
                    int(is_cult),
                    int(is_true_story),
                    int(is_metacritic),
                    RATING_MIN_VOTES,
                ),
            )
            get_db().commit()

    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


# ---------------------------------------------------------------------------
# Quality cache
# ---------------------------------------------------------------------------

def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        row = get_db().execute(
            "SELECT tokens, cached_at, release_date FROM quality_cache WHERE imdb_id = ?",
            (imdb_id,),
        ).fetchone()
        if row is None:
            return None

        tokens_raw, cached_at, stored_release = row
        ttl_release = release_date or stored_release
        age_days    = (time.time() - cached_at) / 86400
        if age_days > _quality_ttl(ttl_release):
            logger.info(f"Quality cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute("DELETE FROM quality_cache WHERE imdb_id = ?", (imdb_id,))
                get_db().commit()
            return None

        return [t for t in (tokens_raw or "").split("|") if t]

    except Exception as exc:
        logger.error(f"Quality cache read error: {exc}")
        return None


def set_cached_quality(
    imdb_id: str,
    tokens: list[str],
    release_date: str | None = None,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO quality_cache
                    (imdb_id, tokens, cached_at, release_date)
                VALUES (?, ?, ?, ?)
                """,
                (imdb_id, "|".join(tokens), int(time.time()), release_date),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending cache  (snapshot-based — one row per media type)
#
# NOTE: The old per-item get_cached_trending / set_cached_trending helpers
# referenced columns ("rank", "tmdb_id") that never existed in the actual
# schema and always raised OperationalError at runtime.  They are removed.
# All callers use get_cached_trending_snapshot / set_cached_trending_snapshot.
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(media_type: str) -> dict[str, int] | None:
    try:
        row = get_db().execute(
            """
            SELECT rankings_json, cached_at
            FROM trending_cache
            WHERE media_type = ?
            """,
            (media_type,),
        ).fetchone()

        if not row:
            return None

        rankings_json, cached_at = row
        age_days = (time.time() - cached_at) / 86400

        if age_days > TRENDING_CACHE_DURATION:
            return None

        return json.loads(rankings_json)
    except Exception as exc:
        logger.error(f"Trending snapshot cache read error: {exc}")
        return None


def set_cached_trending_snapshot(
    media_type: str,
    rankings: dict[str, int],
) -> None:
    old_rankings = get_cached_trending_snapshot(media_type) or {}
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO trending_cache
                (media_type, rankings_json, cached_at)
                VALUES (?, ?, ?)
                """,
                (
                    media_type,
                    json.dumps(rankings),
                    int(time.time()),
                ),
            )
            get_db().commit()
            
        # Invalidate final posters for items that changed trending rank or dropped out.
        changed_ids = set()
        for t_id, r in rankings.items():
            if old_rankings.get(t_id) != r:
                changed_ids.add(t_id)
        for t_id in old_rankings:
            if t_id not in rankings:
                changed_ids.add(t_id)
                
        for t_id in changed_ids:
            invalidate_final_posters(t_id, media_type)
            
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# Filesystem cache helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: bytes) -> None:
    """Atomically replace *path* so readers never observe partial image bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=os.path.dirname(path), prefix=".tmp-", delete=False
        ) as tmp:
            temp_path = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _prune_file_cache(base_dir: str, ttl_days: float) -> None:
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    try:
        for entry in os.scandir(base_dir):
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except FileNotFoundError:
                pass
        if removed:
            logger.info(f"Pruned {removed} expired files from {base_dir}")
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(f"File-cache prune failed for {base_dir}: {exc}")


# ---------------------------------------------------------------------------
# TMDB poster cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_poster(cache_key: str) -> bytes | None:
    # Extension is now .jpg — posters are stored as JPEG for faster decode.
    path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400
    effective_days = TMDB_POSTER_CACHE_DURATION + _ttl_jitter(cache_key, TMDB_IMAGE_CACHE_JITTER_DAYS)

    if age_days > effective_days:
        logger.info(f"TMDB poster cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB poster cache read error: {exc}")
        return None


def set_cached_tmdb_poster(cache_key: str, data: bytes) -> None:
    # Store as .jpg — written by tmdb.py as JPEG q=92 RGB, then converted
    # back to RGBA on load.  ~4x faster decode vs PNG, ~5x smaller on disk.
    try:
        path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)
        _atomic_write(path, data)
    except Exception as exc:
        logger.error(f"TMDB poster cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB logo cache
# ---------------------------------------------------------------------------

def _remove_if_dir(path: str) -> bool:
    """Remove *path* if it is a directory (stale artefact from a previous bug).
    Returns True if a directory was found and removed."""
    if os.path.isdir(path):
        try:
            os.rmdir(path)
            logger.info(f"Removed stale cache directory at {path}")
        except OSError:
            pass
        return True
    return False


def get_cached_tmdb_logo(cache_key: str) -> bytes | None:
    path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)

    if _remove_if_dir(path):
        return None

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400
    effective_days = TMDB_LOGO_CACHE_DURATION + _ttl_jitter(cache_key, TMDB_IMAGE_CACHE_JITTER_DAYS)

    if age_days > effective_days:
        logger.info(f"TMDB logo cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB logo cache read error: {exc}")
        return None


def set_cached_tmdb_logo(cache_key: str, data: bytes) -> None:
    try:
        path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)
        _remove_if_dir(path)
        _atomic_write(path, data)
    except Exception as exc:
        logger.error(f"TMDB logo cache write error: {exc}")

def _safe_cache_path(base_dir: str, filename: str) -> str:
    if os.path.isabs(filename):
        raise ValueError(f"Absolute cache path rejected: {filename!r}")
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, filename))
    if os.path.commonpath((base, path)) != base:
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path

# ---------------------------------------------------------------------------
# TMDB metadata cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        row = get_db().execute(
            """
            SELECT title, release_year, genre_ids, is_textless, poster_path,
                   logos_json, cached_at,
                   credits_json, production_cos_json,
                   runtime, number_of_seasons, number_of_episodes,
                   original_language, original_title, backdrop_path, tmdb_status, vote_count,
                   text_backdrop_path, original_poster_path,
                   poster_langs_json, imdb_id,
                   tmdb_release_date, last_air_date, next_episode_json,
                   last_episode_json, seasons_json, metadata_version
            FROM tmdb_metadata_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if not row:
            return None

        (
            title, release_year, genre_ids_raw, is_textless, poster_path,
            logos_json, cached_at,
            credits_json, production_cos_json,
            runtime, number_of_seasons, number_of_episodes,
            original_language, original_title, backdrop_path, tmdb_status, vote_count,
            text_backdrop_path, original_poster_path,
            poster_langs_json, imdb_id,
            tmdb_release_date, last_air_date, next_episode_json,
            last_episode_json, seasons_json, metadata_version,
        ) = row

        age_days = (time.time() - cached_at) / 86400

        if tmdb_release_date:
            try:
                from datetime import timezone
                rel_dt = datetime.strptime(tmdb_release_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                rel_ts = rel_dt.timestamp()
                now_ts = time.time()
                
                # If cached before the release date, and it is now strictly on or after the release date
                if cached_at < rel_ts and now_ts >= rel_ts:
                    age_days = 9999  # Force expiration
                # If it's unreleased or released within the last 14 days, use a 1-day TTL
                elif (now_ts < rel_ts or (now_ts - rel_ts) < 14 * 86400) and age_days > 1.0:
                    age_days = 9999
            except Exception:
                pass

        if age_days > TMDB_METADATA_CACHE_DURATION:
            logger.info(f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
                
            if age_days == 9999:
                parts = cache_key.split("_")
                if len(parts) >= 2:
                    m_type, t_id = parts[0], parts[1]
                    invalidate_final_posters(t_id, m_type)
                    
            return None

        # Rows created before newer metadata fields were added were migrated
        # with NULL. Refresh once so discovery sashes have complete title,
        # vote, and TV lifecycle fields.
        if vote_count is None or original_title is None or metadata_version != 3:
            logger.info(
                f"TMDB metadata cache missing current schema fields for {cache_key}; refreshing"
            )
            with _db_lock:
                get_db().execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
            return None

        return {
            "title":                title,
            "release_year":         release_year,
            "genre_ids":            json.loads(genre_ids_raw or "[]"),
            "is_textless":          bool(is_textless),
            "poster_path":          poster_path,
            "logos":                json.loads(logos_json or "[]"),
            "credits":              json.loads(credits_json or "{}"),
            "production_companies": json.loads(production_cos_json or "[]"),
            "runtime":              runtime,
            "number_of_seasons":    number_of_seasons,
            "number_of_episodes":   number_of_episodes,
            "original_language":    original_language,
            "original_title":       original_title,
            "backdrop_path":        backdrop_path,
            "tmdb_status":          tmdb_status,
            "vote_count":           vote_count,
            "text_backdrop_path":   text_backdrop_path,
            "original_poster_path": original_poster_path,
            "poster_langs":         json.loads(poster_langs_json or "{}"),
            "imdb_id":              imdb_id,
            "tmdb_release_date":    tmdb_release_date,
            "last_air_date":        last_air_date,
            "next_episode":         json.loads(next_episode_json or "null"),
            "last_episode":         json.loads(last_episode_json or "null"),
            "seasons":              json.loads(seasons_json or "[]"),
            "metadata_version":     metadata_version,
        }
    except Exception as exc:
        logger.error(f"TMDB metadata cache read error: {exc}")
        return None


def set_cached_tmdb_metadata(
    cache_key: str,
    title: str,
    release_year: str | None,
    genre_ids: list[int],
    is_textless: bool,
    poster_path: str,
    logos: list[dict],
    *,
    credits: dict | None = None,
    production_companies: list[dict] | None = None,
    original_language: str | None = None,
    original_title: str | None = None,
    runtime: int | None = None,
    number_of_seasons: int | None = None,
    number_of_episodes: int | None = None,
    backdrop_path: str | None = None,
    tmdb_status: str | None = None,
    vote_count: int | None = None,
    text_backdrop_path: str | None = None,
    original_poster_path: str | None = None,
    poster_langs: dict | None = None,
    imdb_id: str | None = None,
    tmdb_release_date: str | None = None,
    last_air_date: str | None = None,
    next_episode: dict | None = None,
    last_episode: dict | None = None,
    seasons: list[dict] | None = None,
    metadata_version: int = 3,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO tmdb_metadata_cache
                    (cache_key, title, release_year, genre_ids, is_textless,
                     poster_path, logos_json, cached_at,
                     credits_json, production_cos_json,
                     runtime, number_of_seasons, number_of_episodes,
                     original_language, original_title, backdrop_path, tmdb_status, vote_count,
                     text_backdrop_path, original_poster_path,
                     poster_langs_json, imdb_id,
                     tmdb_release_date, last_air_date, next_episode_json,
                     last_episode_json, seasons_json, metadata_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    title,
                    release_year,
                    json.dumps(genre_ids),
                    int(is_textless),
                    poster_path,
                    json.dumps(logos),
                    int(time.time()),
                    json.dumps(credits or {}),
                    json.dumps(production_companies or []),
                    runtime,
                    number_of_seasons,
                    number_of_episodes,
                    original_language,
                    original_title,
                    backdrop_path,
                    tmdb_status,
                    vote_count,
                    text_backdrop_path,
                    original_poster_path,
                    json.dumps(poster_langs or {}),
                    imdb_id,
                    tmdb_release_date,
                    last_air_date,
                    json.dumps(next_episode) if next_episode else None,
                    json.dumps(last_episode) if last_episode else None,
                    json.dumps(seasons or []),
                    metadata_version,
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    """Remove a single TMDB metadata entry so the next request re-fetches from TMDB."""
    try:
        with _db_lock:
            get_db().execute(
                "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


# ---------------------------------------------------------------------------
# TVDB generic JSON cache (resolved ids, artwork indexes, type catalogue, token)
# ---------------------------------------------------------------------------

def get_cached_tvdb_json(cache_key: str) -> dict | None:
    """Return the cached JSON object for *cache_key*, or None on miss/expiry.
    Expired rows are deleted on read so stale data never lingers."""
    try:
        row = get_db().execute(
            "SELECT value_json, cached_at, ttl_seconds FROM tvdb_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        value_json, cached_at, ttl_seconds = row
        if (time.time() - cached_at) > ttl_seconds:
            with _db_lock:
                get_db().execute("DELETE FROM tvdb_cache WHERE cache_key = ?", (cache_key,))
                get_db().commit()
            return None
        return json.loads(value_json)
    except Exception as exc:
        logger.error(f"TVDB cache read error: {exc}")
        return None


def set_cached_tvdb_json(cache_key: str, value: dict, ttl_seconds: int) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO tvdb_cache
                    (cache_key, value_json, cached_at, ttl_seconds)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, json.dumps(value), int(time.time()), int(ttl_seconds)),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TVDB cache write error: {exc}")


# ---------------------------------------------------------------------------
# Digital release cache
# ---------------------------------------------------------------------------

def is_digital_release(imdb_id: str) -> bool:
    """Return True if the IMDB ID has a matching entry in the digital release cache."""
    try:
        row = get_db().execute(
            "SELECT 1 FROM digital_release_cache WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
    """
    Insert (imdb_id, posted_at) pairs. Uses INSERT OR IGNORE so the
    original posted_at is never overwritten. Returns the number of new rows inserted.
    """
    if not entries:
        return 0
    inserted = 0
    try:
        with _db_lock:
            for imdb_id, posted_at in entries:
                r = get_db().execute(
                    "INSERT OR IGNORE INTO digital_release_cache (imdb_id, posted_at) VALUES (?, ?)",
                    (imdb_id, posted_at),
                )
                inserted += r.rowcount
            get_db().commit()
    except Exception as exc:
        logger.error(f"Digital release cache write error: {exc}")
    return inserted


# ---------------------------------------------------------------------------
# Release status cache
# ---------------------------------------------------------------------------
# Cached separately from main metadata so the extra TMDB /release_dates call
# only happens for users who have enabled the "release_status" sash slot.
# TTL: 7 days — status changes slowly (Cinema → Streaming → BluRay is one-way).

_RELEASE_STATUS_TTL_DAYS = 7


def get_cached_movie_release_info(cache_key: str) -> dict | None:
    """Return cached movie release info JSON, or None if absent / expired."""
    try:
        row = get_db().execute(
            "SELECT info_json, cached_at FROM movie_release_info_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        info_json, cached_at = row
        age_days = (time.time() - cached_at) / 86400
        if age_days > _RELEASE_STATUS_TTL_DAYS:
            logger.info(f"Movie release info cache expired for {cache_key} ({age_days:.1f}d old)")
            return None
        return json.loads(info_json or "{}")
    except Exception as exc:
        logger.error(f"Movie release info cache read error: {exc}")
        return None


def set_cached_movie_release_info(cache_key: str, info: dict) -> None:
    """Upsert richer TMDB movie release-date information."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO movie_release_info_cache (cache_key, info_json, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET info_json=excluded.info_json, cached_at=excluded.cached_at
                """,
                (cache_key, json.dumps(info), int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Movie release info cache write error: {exc}")


def get_cached_release_status(cache_key: str) -> str | None:
    """Return the cached release status string, or None if absent / expired."""
    try:
        row = get_db().execute(
            "SELECT status, cached_at FROM release_status_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        status, cached_at = row
        age_days = (time.time() - cached_at) / 86400
        if age_days > _RELEASE_STATUS_TTL_DAYS:
            logger.info(f"Release status cache expired for {cache_key} ({age_days:.1f}d old)")
            return None
        return status
    except Exception as exc:
        logger.error(f"Release status cache read error: {exc}")
        return None


def set_cached_release_status(cache_key: str, status: str) -> None:
    """Upsert a release status entry."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO release_status_cache (cache_key, status, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET status=excluded.status, cached_at=excluded.cached_at
                """,
                (cache_key, status, int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Release status cache write error: {exc}")


def get_cached_text_detection(cache_key: str) -> bool | None:
    """Return the cached burned-in-text result (True/False), or None if absent.

    Results never expire — they're keyed by an immutable TMDB image path plus the
    detection params, so the answer can't change for a given key.
    """
    try:
        row = get_db().execute(
            "SELECT has_text FROM text_detection_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return None if row is None else bool(row[0])
    except Exception as exc:
        logger.error(f"Text-detection cache read error: {exc}")
        return None


def set_cached_text_detection(cache_key: str, has_text: bool) -> None:
    """Upsert a burned-in-text detection result."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO text_detection_cache (cache_key, has_text, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET has_text=excluded.has_text, cached_at=excluded.cached_at
                """,
                (cache_key, int(has_text), int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Text-detection cache write error: {exc}")


def get_cached_artwork_candidates(
    cache_key: str,
    *,
    complete_ttl: int,
    partial_ttl: int,
) -> dict | None:
    """Return a fresh successful candidate response, or None."""
    try:
        with _db_lock:
            row = get_db().execute(
                """
                SELECT response_json, result_class, cached_at
                FROM artwork_candidate_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        ttl = partial_ttl if row[1] == "partial" else complete_ttl
        if row[1] not in ("complete", "empty", "partial"):
            return None
        if int(time.time()) - int(row[2]) >= max(0, ttl):
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except Exception as exc:
        logger.error(f"Artwork-candidate cache read error: {exc}")
        return None


def set_cached_artwork_candidates(
    cache_key: str,
    response: dict,
    result_class: str,
    *,
    deadline_monotonic: float | None = None,
) -> bool | None:
    """Cache only complete, empty, or partial successful searches."""
    if result_class not in ("complete", "empty", "partial"):
        raise ValueError("invalid artwork-candidate result class")
    payload = json.dumps(response, sort_keys=True, separators=(",", ":"))
    cached_at = int(time.time())
    try:
        with _db_lock:
            if (
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                return False
            db = get_db()
            db.execute(
                """
                INSERT INTO artwork_candidate_cache
                    (cache_key, response_json, result_class, cached_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_json=excluded.response_json,
                    result_class=excluded.result_class,
                    cached_at=excluded.cached_at
                WHERE excluded.result_class != 'partial'
                   OR artwork_candidate_cache.result_class = 'partial'
                   OR artwork_candidate_cache.cached_at < excluded.cached_at - 86400
                """,
                (cache_key, payload, result_class, cached_at),
            )
            if (
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                db.rollback()
                return False
            db.commit()
            if (
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                db.execute(
                    """
                    DELETE FROM artwork_candidate_cache
                    WHERE cache_key = ? AND response_json = ? AND result_class = ?
                    """,
                    (cache_key, payload, result_class),
                )
                db.commit()
                return False
            return True
    except Exception as exc:
        logger.error(f"Artwork-candidate cache write error: {exc}")
        return None


# ---------------------------------------------------------------------------
# App state — small key/value store for cross-restart bookkeeping
# ---------------------------------------------------------------------------

def get_app_state(key: str) -> str | None:
    """Return the stored string value for *key*, or None if unset/on error."""
    try:
        row = get_db().execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]
    except Exception as exc:
        logger.error(f"App state read error ({key}): {exc}")
        return None


def set_app_state(key: str, value: str) -> None:
    """Upsert a string value in the app-state key/value store."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"App state write error ({key}): {exc}")